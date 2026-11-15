"""按报表聚合最近一次生成的产物。

第二步的核心产物是**校验规则**，但之前的界面只让用户「选表」，生成完
看不到结果。这里按表聚合最近一次 run 的规则、表结构、码表迁移和验证结果，
让「这张表最终产出了什么」一眼可见。
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import Session

from ..asset_models import (
    CodeSetModel,
    CodeValueModel,
    FieldMetadataModel,
    TableMetadataModel,
)
from ..blobs import LocalBlobStore
from ..compiler import compile_where
from ..config import get_settings
from ..db import SessionLocal
from ..domain import RuleDefinition
from ..models import RuleModel, RunModel

router = APIRouter(prefix="/artifacts", tags=["artifacts"])

_OPERATOR_LABELS = {
    "REQUIRED": "必填",
    "ENUM": "枚举",
    "NOT_IN": "排除",
    "COND_REQUIRED": "条件必填",
    "LENGTH": "定长",
    "LENGTH_MAX": "最大长度",
    "REGEXP": "格式",
    "FOREIGN_KEY": "外键",
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


@router.get("")
def artifacts_status() -> Dict[str, Any]:
    """每张表最近一次生成的状态，用于列表显示（未生成/生成中/已验证/失败）。"""
    with SessionLocal() as session:
        latest: Dict[str, str] = {}
        for row in session.query(RuleModel).order_by(RuleModel.created_at.desc()).all():
            if row.table_code not in latest:
                latest[row.table_code] = row.run_id
        run_ids = set(latest.values())
        runs = {row.id: row for row in session.query(RunModel).filter(RunModel.id.in_(run_ids)).all()} if run_ids else {}
        tables = session.query(TableMetadataModel).all()
        result = []
        for table in tables:
            run_id = latest.get(table.table_code)
            run = runs.get(run_id)
            summary = (run.result_json or {}).get("audit_summary") if run else {}
            result.append(
                {
                    "table_id": table.id,
                    "table_code": table.table_code,
                    "table_name": table.table_name,
                    "generated": run_id is not None,
                    "run_id": run_id,
                    "run_status": run.status if run else None,
                    "all_passed": summary.get("all_passed") if summary else None,
                    "created_at": run.created_at.isoformat() if run and run.created_at else None,
                }
            )
        return {"tables": result}


@router.get("/{table_id}")
def table_artifacts(table_id: str) -> Dict[str, Any]:
    settings = get_settings()
    store = LocalBlobStore(settings.blob_dir)
    with SessionLocal() as session:
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
            "audit": None,
        }
        if not run:
            return payload

        payload["run"] = {
            "id": run.id,
            "status": run.status,
            "current_node": run.current_node,
            "created_at": run.created_at.isoformat() if run.created_at else None,
        }

        # 规则：核心产物，按算子分组，每条带原文依据和编译后的 SQL
        rows = (
            session.query(RuleModel)
            .filter(RuleModel.run_id == run.id, RuleModel.table_code == table.table_code)
            .order_by(RuleModel.created_at, RuleModel.rule_key)
            .all()
        )
        groups: Dict[str, int] = {}
        rules: List[Dict[str, Any]] = []
        physical = {table.table_code: table.table_code}
        for row in rows:
            definition = RuleDefinition.model_validate(row.definition_json)
            try:
                sql, _ = compile_where(definition, "oracle", physical)
            except Exception:
                sql = None
            groups[definition.operator] = groups.get(definition.operator, 0) + 1
            rules.append(
                {
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
        payload["rules"] = rules
        payload["rule_groups"] = groups

        # 表结构：建表 SQL（Oracle 为主，MySQL 备用）
        result = run.result_json or {}
        ddl_uri = result.get("ddl_blob") or result.get("oracle_ddl_blob")
        if ddl_uri:
            payload["ddl"] = _blob_text(store, ddl_uri)

        # 码表迁移：这几张码表灌了多少条进 Oracle 沙箱
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

        # 验证结果
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
