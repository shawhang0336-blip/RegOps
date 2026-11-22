"""按报表聚合最近一次生成的产物。

第二步的核心产物是**校验规则**，但之前的界面只让用户「选表」，生成完
看不到结果。这里按表聚合最近一次 run 的规则、表结构、码表迁移和验证结果，
让「这张表最终产出了什么」一眼可见。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import db_session
from app.catalog.code_sets.bindings import code_set_compile_bindings, load_published_code_sets
from app.catalog.tables.readiness import latest_table_rows
from app.generation.sql.compiler import compile_where
from app.generation.core.rules import RuleValidationError, normalize_oracle_regexp, validate_rule
from app.infra.db_tables import (
    CodeSetModel,
    CodeValueModel,
    FieldMetadataModel,
    RuleModel,
    RunModel,
    SourceDocumentModel,
    SpecificationModel,
    TableMetadataModel,
)
from app.infra.blob_store import LocalBlobStore
from app.infra.config import OPERATOR_PLACEHOLDER, get_settings
from app.infra.contracts import RuleDefinition, SpecDocument, calculate_rule_hash
from app.kernel.event_log import append_event
from app.kernel.run import run_registry
from app.policy.regops_bus import default_bus
from app.policy.regops_plan import ACTIVE_RUN_STATUSES, project, project_many

router = APIRouter(prefix="/artifacts", tags=["artifacts"])

_OPERATOR_LABELS = {
    "REQUIRED": "必填",
    "ENUM": "枚举",
    "NOT_IN": "排除",
    "COND_REQUIRED": "条件必填",
    "COND_VALUE": "条件取值/互斥",
    "LENGTH": "定长",
    "LENGTH_MAX": "最大长度",
    "REGEXP": "格式",
    "FOREIGN_KEY": "外键",
    "NUM_RANGE": "数值区间",
    "FIELD_COMPARE": "跨字段比较",
    "ARITHMETIC_BALANCE": "算术平衡/勾稽",
    "UNIQUE_KEY": "唯一键查重",
}


def _blob_text(store: LocalBlobStore, uri: Optional[str]) -> Optional[str]:
    if not uri:
        return None
    try:
        return store.get(uri).decode("utf-8")
    except (ValueError, FileNotFoundError):
        return None


def _latest_run_for_table(session: Session, table_code: str) -> Optional[RunModel]:
    """找这张表参与过、且确实产出了规则的最近一次 run。"""
    row = (
        session.query(RuleModel.run_id)
        .filter(RuleModel.table_code == table_code)
        .order_by(RuleModel.created_at.desc())
        .first()
    )
    if not row:
        return None
    return session.get(RunModel, row[0])


def _rules_payload(
    session: Session,
    table: TableMetadataModel,
    run: RunModel,
    *,
    rows: Optional[List[RuleModel]] = None,
    run_state: Any = None,
    spec_model: Optional[SpecificationModel] = None,
) -> List[Dict[str, Any]]:
    """一张表在某次 run 里的规则列表，带编译后的 SQL；明细和总览共用它。"""
    if rows is None:
        rows = (
            session.query(RuleModel)
            .filter(RuleModel.run_id == run.id, RuleModel.table_code == table.table_code)
            .order_by(RuleModel.created_at, RuleModel.rule_key)
            .all()
        )
    physical = {table.table_code: table.table_code}
    # 尽力还原执行时的码表子查询形态：码表版本漂移或老批次缺上下文时回退为内联列表
    code_set_bindings = None
    try:
        resolved_spec = spec_model if spec_model is not None else session.get(SpecificationModel, run.spec_id)
        specs = [
            SpecDocument.model_validate(item)
            for item in ((resolved_spec.content_json if resolved_spec else None) or {}).get("tables") or []
            if item.get("table_code") == table.table_code
        ]
        state = run_state if run_state is not None else project(session, run)
        code_set_tables = ((state.result_json or {}).get("oracle_code_set_tables")) or {}
        if specs and code_set_tables:
            _, field_bindings = load_published_code_sets(session, specs)
            code_set_bindings = code_set_compile_bindings(field_bindings, code_set_tables)
    except Exception:
        code_set_bindings = None
    rules: List[Dict[str, Any]] = []
    for row in rows:
        definition = RuleDefinition.model_validate(row.definition_json)
        try:
            sql, _ = compile_where(definition, "oracle", physical, code_set_bindings)
        except Exception:
            sql = None
        rules.append(
            {
                "row_id": row.row_id,
                "rule_hash": row.rule_hash,
                "rule_key": definition.rule_key,
                "field": definition.field,
                "operator": definition.operator,
                "operator_label": _OPERATOR_LABELS.get(definition.operator, definition.operator),
                "params": definition.params,
                "source": definition.source,
                "status": row.status,
                "rationale": definition.rationale,
                "evidence": definition.evidence.model_dump() if definition.evidence else None,
                "sql": sql,
            }
        )
    return rules


def _latest_run_ids_by_table(session: Session) -> Dict[str, str]:
    candidates = (
        session.query(RuleModel.table_code, RuleModel.run_id, func.max(RuleModel.created_at))
        .group_by(RuleModel.table_code, RuleModel.run_id)
        .all()
    )
    latest: Dict[str, str] = {}
    newest_at: Dict[str, Any] = {}
    for table_code, run_id, newest in candidates:
        if table_code not in newest_at or (newest or datetime.min) > newest_at[table_code]:
            newest_at[table_code] = newest or datetime.min
            latest[table_code] = run_id
    return latest


@router.get("/rules")
def all_table_rules(
    document_id: Optional[str] = None,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    """所有已生成表的规则总览：按表分组（排除 DELETED 文档、MERGED 与历史旧版本），批量查询避免 N+1。"""
    tables = latest_table_rows(session, document_id=document_id)
    latest = _latest_run_ids_by_table(session)
    table_codes_with_runs = {table.table_code for table in tables if table.table_code in latest}
    run_ids = {latest[code] for code in table_codes_with_runs}
    if not run_ids:
        return {"tables": []}
    runs = {row.id: row for row in session.query(RunModel).filter(RunModel.id.in_(run_ids)).all()}
    run_states = project_many(session, runs.values()) if runs else {}
    spec_ids = {run.spec_id for run in runs.values() if run.spec_id}
    specs_by_id = (
        {row.id: row for row in session.query(SpecificationModel).filter(SpecificationModel.id.in_(spec_ids)).all()}
        if spec_ids
        else {}
    )
    all_rule_rows = (
        session.query(RuleModel)
        .filter(RuleModel.run_id.in_(list(runs.keys())), RuleModel.table_code.in_(list(table_codes_with_runs)))
        .order_by(RuleModel.created_at, RuleModel.rule_key)
        .all()
    )
    rules_by_pair: Dict[Tuple[str, str], List[RuleModel]] = {}
    for rule_row in all_rule_rows:
        rules_by_pair.setdefault((rule_row.run_id, rule_row.table_code), []).append(rule_row)

    items: List[Dict[str, Any]] = []
    for table in tables:
        run_id = latest.get(table.table_code)
        run = runs.get(run_id) if run_id else None
        if not run:
            continue
        state = run_states.get(run.id) or project(session, run)
        rules = _rules_payload(
            session,
            table,
            run,
            rows=rules_by_pair.get((run.id, table.table_code), []),
            run_state=state,
            spec_model=specs_by_id.get(run.spec_id),
        )
        result = state.result_json or {}
        items.append(
            {
                "table": {
                    "id": table.id,
                    "document_id": table.document_id,
                    "table_code": table.table_code,
                    "table_name": table.table_name,
                    "status": table.status,
                },
                "run": {
                    "id": run.id,
                    "status": state.status,
                    "created_at": run.created_at.isoformat() if run.created_at else None,
                },
                "rules": rules,
                "active_count": sum(1 for rule in rules if rule["status"] != "SUPERSEDED"),
                "pending_count": sum(1 for rule in rules if rule["status"] == "PENDING"),
                "superseded_count": sum(1 for rule in rules if rule["status"] == "SUPERSEDED"),
                "precheck": result.get("oracle_precheck"),
            }
        )
    items.sort(key=lambda item: (item["run"]["created_at"] or "", item["table"]["table_code"]), reverse=True)
    return {"tables": items}


@router.get("")
def artifacts_status(
    document_id: Optional[str] = None,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    """每张表最近一次生成的状态（排除 DELETED 文档、MERGED 碎片表与旧版本，批量折叠状态防 N+1）。"""
    latest = _latest_run_ids_by_table(session)
    count_rows = (
        session.query(RuleModel.run_id, RuleModel.table_code, func.count())
        .filter(RuleModel.status != "SUPERSEDED")
        .group_by(RuleModel.run_id, RuleModel.table_code)
        .all()
    )
    rule_counts: Dict[str, Dict[str, int]] = {}
    for run_id, table_code, count in count_rows:
        rule_counts.setdefault(run_id, {})[table_code] = count
    tables = latest_table_rows(session, document_id=document_id)
    run_ids = {latest[table.table_code] for table in tables if table.table_code in latest}
    runs = {row.id: row for row in session.query(RunModel).filter(RunModel.id.in_(run_ids)).all()} if run_ids else {}
    run_states = project_many(session, runs.values()) if runs else {}
    doc_ids = {table.document_id for table in tables if table.document_id}
    doc_names = (
        {
            row.id: row.file_name
            for row in session.query(SourceDocumentModel).filter(SourceDocumentModel.id.in_(doc_ids)).all()
        }
        if doc_ids
        else {}
    )
    result = []
    for table in tables:
        run_id = latest.get(table.table_code)
        run = runs.get(run_id)
        run_state = run_states.get(run.id) if run else None
        run_result = (run_state.result_json or {}) if run_state else {}
        summary = run_result.get("audit_summary") or {}
        result.append(
            {
                "id": table.id,
                "table_id": table.id,
                "table_code": table.table_code,
                "table_name": table.table_name,
                "document_id": table.document_id,
                "document_name": doc_names.get(table.document_id, ""),
                "status": table.status,
                "generated": run_id is not None,
                "run_id": run_id,
                "run_status": run_state.status if run_state else None,
                "all_passed": summary.get("all_passed") if summary else None,
                "created_at": run.created_at.isoformat() if run and run.created_at else None,
                "ddl_generated": bool(run_result.get("ddl_blob") or run_result.get("oracle_ddl_blob")),
                "rule_count": rule_counts.get(run_id, {}).get(table.table_code, 0) if run_id else 0,
                "code_set_count": len(summary.get("code_set_record_counts") or {}),
                "audit_case_count": summary.get("case_count"),
            }
        )
    return {"tables": result}


class RuleEditRequest(BaseModel):
    params: Dict[str, Any] = Field(default_factory=dict)
    editor: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)
    reason: str = ""


def _spec_bundle_for_rule(session: Session, rule: RuleModel) -> Tuple[SpecDocument, Dict[str, SpecDocument]]:
    """规则改 params 前拿所属表及同快照全部表的 spec 做合法性校验（含 FOREIGN_KEY 跨表校验）。"""
    spec_model = session.get(SpecificationModel, rule.spec_id)
    if not spec_model:
        raise HTTPException(status_code=404, detail="Rule spec not found")
    content = spec_model.content_json or {}
    all_specs: Dict[str, SpecDocument] = {}
    for table in content.get("tables") or []:
        if isinstance(table, dict) and table.get("table_code"):
            doc = SpecDocument.model_validate(table)
            all_specs[doc.table_code] = doc
    if rule.table_code not in all_specs:
        raise HTTPException(status_code=404, detail=f"Spec table {rule.table_code} not found")
    return all_specs[rule.table_code], all_specs


def _validate_edited_rule_references(
    session: Session,
    definition: RuleDefinition,
    all_specs: Dict[str, SpecDocument],
) -> None:
    if definition.operator == "ENUM":
        code_set_code = definition.params.get("code_set")
        if code_set_code:
            cs = (
                session.query(CodeSetModel)
                .filter(CodeSetModel.code == str(code_set_code).strip().upper(), CodeSetModel.status == "PUBLISHED")
                .order_by(CodeSetModel.current_version.desc())
                .first()
            )
            if not cs:
                raise HTTPException(status_code=422, detail=f"引用的码表 {code_set_code} 未发布或不存在")
            enabled_count = (
                session.query(CodeValueModel)
                .filter(CodeValueModel.code_set_id == cs.id, CodeValueModel.enabled.is_(True))
                .count()
            )
            if enabled_count == 0:
                raise HTTPException(status_code=422, detail=f"引用的码表 {code_set_code} 无可用码值")
    if definition.operator == "FOREIGN_KEY":
        ref_table = str(definition.params.get("references_table") or "").strip()
        ref_field = str(definition.params.get("references_field") or "").strip()
        if not ref_table or not ref_field:
            raise HTTPException(status_code=422, detail="FOREIGN_KEY 需要非空的 references_table 和 references_field")
        target_spec = all_specs.get(ref_table)
        if target_spec is not None:
            target_fields = {f.name for f in target_spec.fields}
            if ref_field not in target_fields:
                raise HTTPException(status_code=422, detail=f"外键目标字段不存在：{ref_table}.{ref_field}")
        else:
            meta_table = (
                session.query(TableMetadataModel)
                .filter(TableMetadataModel.table_code == ref_table, TableMetadataModel.status != "MERGED")
                .order_by(TableMetadataModel.current_version.desc())
                .first()
            )
            if not meta_table:
                raise HTTPException(status_code=422, detail=f"外键目标表不存在：{ref_table}")
            has_field = (
                session.query(FieldMetadataModel.id)
                .filter(FieldMetadataModel.table_id == meta_table.id, FieldMetadataModel.field_en == ref_field)
                .first()
            )
            if not has_field:
                raise HTTPException(status_code=422, detail=f"外键目标字段不存在：{ref_table}.{ref_field}")


@router.patch("/rules/{rule_row_id}")
def edit_rule(rule_row_id: str, request: RuleEditRequest, session: Session = Depends(db_session)) -> Dict[str, Any]:
    """改一条规则的参数（校验活跃运行锁、Oracle 正则、外键/码表、哈希冲突并落 EventLog 留痕）。"""
    row = session.get(RuleModel, rule_row_id)
    if not row:
        raise HTTPException(status_code=404, detail="Rule not found")
    if row.status == "SUPERSEDED":
        raise HTTPException(status_code=409, detail="已被替代（SUPERSEDED）的历史规则不可修改")
    run = session.get(RunModel, row.run_id)
    if run and (run_registry().is_active(run.id) or project(session, run).status in ACTIVE_RUN_STATUSES):
        raise HTTPException(status_code=409, detail="当前运行批次仍在执行中，不能修改规则参数")
    definition = RuleDefinition.model_validate(row.definition_json)
    spec, all_specs = _spec_bundle_for_rule(session, row)
    previous = definition.model_dump()
    new_params = dict(request.params)
    if definition.operator == "REGEXP" and isinstance(new_params.get("pattern"), str):
        new_params["pattern"] = normalize_oracle_regexp(new_params["pattern"])
    definition.params = new_params
    try:
        validate_rule(definition, spec)
    except RuleValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _validate_edited_rule_references(session, definition, all_specs)
    new_hash = calculate_rule_hash(definition, spec.source_sha256)
    duplicate = (
        session.query(RuleModel.row_id)
        .filter(
            RuleModel.run_id == row.run_id,
            RuleModel.rule_hash == new_hash,
            RuleModel.row_id != row.row_id,
        )
        .first()
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="修改后的规则与当前批次已有规则重复")
    old_hash = row.rule_hash
    definition.rule_hash = new_hash
    row.definition_json = definition.model_dump()
    row.rule_hash = new_hash
    if run:
        append_event(
            session,
            run,
            "RULE_EDITED",
            "human_edit",
            {
                "row_id": row.row_id,
                "table_code": row.table_code,
                "rule_key": definition.rule_key,
                "old_rule_hash": old_hash,
                "new_rule_hash": new_hash,
                "previous_params": previous.get("params"),
                "params": definition.params,
                "editor": request.editor,
                "reason": request.reason,
            },
            bus=default_bus(),
        )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="修改后的规则与当前批次已有规则重复") from exc
    return {
        "rule": {
            "rule_key": definition.rule_key,
            "field": definition.field,
            "operator": definition.operator,
            "params": definition.params,
            "rule_hash": definition.rule_hash,
        },
        "previous_params": previous.get("params"),
        "editor": request.editor,
        "reason": request.reason,
    }


@router.get("/{table_id}")
def table_artifacts(table_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    settings = get_settings()
    store = LocalBlobStore(settings.blob_dir)
    table = session.get(TableMetadataModel, table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Metadata table not found")
    fields = (
        session.query(FieldMetadataModel)
        .filter(FieldMetadataModel.table_id == table_id)
        .order_by(FieldMetadataModel.sort_order)
        .all()
    )
    run = _latest_run_for_table(session, table.table_code)

    payload: Dict[str, Any] = {
        "table": {
            "id": table.id,
            "table_code": table.table_code,
            "table_name": table.table_name,
            "field_count": len(fields),
            "status": table.status,
        },
        "run": None,
        "rules": [],
        "rule_groups": {},
        "ddl": None,
        "code_sets": [],
        "precheck": None,
        "audit": None,
    }
    if not run:
        return payload

    state = project(session, run)
    payload["run"] = {
        "id": run.id,
        "status": state.status,
        "current_node": state.current_node,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }

    rules = _rules_payload(session, table, run)
    groups: Dict[str, int] = {}
    for rule in rules:
        groups[rule["operator"]] = groups.get(rule["operator"], 0) + 1
    payload["rules"] = rules
    payload["rule_groups"] = groups

    result = state.result_json or {}
    ddl_uri = result.get("ddl_blob") or result.get("oracle_ddl_blob")
    if ddl_uri:
        payload["ddl"] = _blob_text(store, ddl_uri)

    summary = result.get("audit_summary") or {}
    record_counts = summary.get("code_set_record_counts") or {}
    for code, count in record_counts.items():
        row = session.query(CodeSetModel).filter(CodeSetModel.code == code).first()
        value_count = (
            session.query(CodeValueModel)
            .filter(CodeValueModel.code_set_id == row.id, CodeValueModel.enabled.is_(True))
            .count()
            if row
            else 0
        )
        payload["code_sets"].append(
            {
                "code": code,
                "name": row.name if row else code,
                "value_count": value_count,
                "oracle_records": count,
                "oracle_table": (summary.get("code_set_tables") or {}).get(code),
            }
        )

    payload["precheck"] = result.get("oracle_precheck")
    if summary:
        payload["audit"] = {
            "all_passed": summary.get("all_passed"),
            "case_count": summary.get("case_count"),
            "active_rule_count": summary.get("active_rule_count"),
            "coverage_rate": summary.get("coverage_rate"),
            "coverage_complete": summary.get("coverage_complete"),
            "false_negative_count": summary.get("false_negative_count"),
            "false_positive_count": summary.get("false_positive_count"),
            "co_triggered_count": summary.get("co_triggered_count"),
            "record_counts": summary.get("record_counts"),
            "audit_report_blob": result.get("audit_report_blob"),
        }
    return payload
