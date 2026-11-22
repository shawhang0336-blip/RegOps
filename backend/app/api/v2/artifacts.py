"""按报表聚合最近一次生成的产物。

第二步的核心产物是**校验规则**，但之前的界面只让用户「选表」，生成完
看不到结果。这里按表聚合最近一次 run 的规则、表结构、码表迁移和验证结果，
让「这张表最终产出了什么」一眼可见。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import db_session
from app.catalog.code_sets.bindings import code_set_compile_bindings, load_published_code_sets
from app.generation.sql.compiler import compile_where
from app.generation.core.rules import RuleValidationError, validate_rule
from app.infra.db_tables import (
    CodeSetModel,
    CodeValueModel,
    FieldMetadataModel,
    SourceDocumentModel,
    TableMetadataModel,
)
from app.infra.blob_store import LocalBlobStore
from app.infra.config import get_settings
from app.infra.contracts import RuleDefinition, SpecDocument, calculate_rule_hash
from app.infra.config import OPERATOR_PLACEHOLDER
from app.infra.db_tables import RuleModel, RunModel, SpecificationModel
from app.policy.regops_plan import project

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


def _rules_payload(session: Session, table: TableMetadataModel, run: RunModel) -> List[Dict[str, Any]]:
    """一张表在某次 run 里的规则列表，带编译后的 SQL；明细和总览共用它。"""
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
        spec_model = session.get(SpecificationModel, run.spec_id)
        specs = [
            SpecDocument.model_validate(item)
            for item in (spec_model.content_json or {}).get("tables") or []
            if item.get("table_code") == table.table_code
        ]
        code_set_tables = ((project(session, run).result_json or {}).get("oracle_code_set_tables")) or {}
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


@router.get("/rules")
def all_table_rules(session: Session = Depends(db_session)) -> Dict[str, Any]:
    """所有已生成表的规则总览：按表分组，每组带最近一次 run 的状态。"""
    tables = session.query(TableMetadataModel).order_by(TableMetadataModel.table_code).all()
    items: List[Dict[str, Any]] = []
    for table in tables:
        run = _latest_run_for_table(session, table.table_code)
        if not run:
            continue
        rules = _rules_payload(session, table, run)
        state = project(session, run)
        result = state.result_json or {}
        items.append(
            {
                "table": {
                    "id": table.id,
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
def artifacts_status(session: Session = Depends(db_session)) -> Dict[str, Any]:
    """每张表最近一次生成的状态，用于列表显示（未生成/生成中/已验证/失败）。"""
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
    count_rows = (
        session.query(RuleModel.run_id, RuleModel.table_code, func.count())
        .filter(RuleModel.status != "SUPERSEDED")
        .group_by(RuleModel.run_id, RuleModel.table_code)
        .all()
    )
    rule_counts: Dict[str, Dict[str, int]] = {}
    for run_id, table_code, count in count_rows:
        rule_counts.setdefault(run_id, {})[table_code] = count
    run_ids = set(latest.values())
    runs = {row.id: row for row in session.query(RunModel).filter(RunModel.id.in_(run_ids)).all()} if run_ids else {}
    tables = session.query(TableMetadataModel).all()
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
        run_state = project(session, run) if run else None
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


def _spec_for_rule(session: Session, rule: RuleModel) -> SpecDocument:
    """规则改 params 前要拿它所属表的 spec 做合法性校验（长度>0、枚举非空等）。"""
    spec_model = session.get(SpecificationModel, rule.spec_id)
    if not spec_model:
        raise HTTPException(status_code=404, detail="Rule spec not found")
    content = spec_model.content_json or {}
    for table in content.get("tables") or []:
        if table.get("table_code") == rule.table_code:
            return SpecDocument.model_validate(table)
    raise HTTPException(status_code=404, detail=f"Spec table {rule.table_code} not found")


@router.patch("/rules/{rule_row_id}")
def edit_rule(rule_row_id: str, request: RuleEditRequest, session: Session = Depends(db_session)) -> Dict[str, Any]:
    """改一条规则的参数，保存后由前端决定要不要重新生成验证。"""
    row = session.get(RuleModel, rule_row_id)
    if not row:
        raise HTTPException(status_code=404, detail="Rule not found")
    definition = RuleDefinition.model_validate(row.definition_json)
    spec = _spec_for_rule(session, row)
    previous = definition.model_dump()
    definition.params = dict(request.params)
    try:
        validate_rule(definition, spec)
    except RuleValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    definition.rule_hash = calculate_rule_hash(definition, spec.source_sha256)
    row.definition_json = definition.model_dump()
    row.rule_hash = definition.rule_hash
    session.commit()
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
