"""API 公共依赖、请求契约与视图序列化器（v1 / v2 共享）。"""

from typing import Dict, Iterable, List, Optional

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.generation.workflow import submit_generic_run
from app.infra.config import get_settings
from app.infra.db import SessionLocal
from app.infra.config import OPERATOR_PLACEHOLDER
from app.infra.db_tables import EventModel, RuleModel, RunModel, SpecificationModel
from app.policy.regops_plan import ACTIVE_RUN_STATUSES, project


def spawn_run_driver(
    run_id: str,
    settings=None,
    generation_budget=None,
    allowed_statuses: Iterable[str] = ("RUNNING", "READY_TO_AUDIT"),
) -> bool:
    """兼容薄壳：统一委托至 `engine.workflow.submit_generic_run`（底层基于 `kernel.run.submit_run`）。"""
    return submit_generic_run(
        run_id,
        settings=settings if settings is not None else get_settings(),
        generation_budget=generation_budget,
        allowed_statuses=allowed_statuses,
    )


def db_session():
    """FastAPI 依赖：提供按请求隔离的 SQLAlchemy Session。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


# ── 请求体契约 ────────────────────────────────────────────────────────────────


class RunRequest(BaseModel):
    spec_id: str
    provider: str = "live"


class TestConfirmationRequest(BaseModel):
    approver: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)


class Decision(BaseModel):
    rule_hash: str
    decision: str


class ApprovalRequest(BaseModel):
    approver: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)
    decisions: List[Decision]
    provider: str = "live"


# ── 视图序列化 ────────────────────────────────────────────────────────────────


def rule_view(row: RuleModel) -> Dict:
    return {
        "rule_hash": row.rule_hash,
        "table_code": row.table_code,
        "rule_key": row.rule_key,
        "source": row.source,
        "status": row.status,
        "definition": row.definition_json,
    }


def run_tables_from_content(content: Dict) -> List[Dict]:
    tables = (content or {}).get("tables") or ([content] if content else [])
    return [
        {"table_code": item.get("table_code"), "table_name": item.get("table_name")}
        for item in tables
        if item.get("table_code")
    ]


def run_tables(session: Session, run: RunModel) -> List[Dict]:
    spec = session.get(SpecificationModel, run.spec_id)
    return run_tables_from_content((spec.content_json or {}) if spec else {})


def run_view(
    session: Session,
    run: RunModel,
    include_events: bool = True,
    run_state=None,
    prefetched_rules: Optional[List[RuleModel]] = None,
    prefetched_tables: Optional[List[Dict]] = None,
) -> Dict:
    """将 RunModel + EventLog 折叠状态序列化为前端消费视图。"""
    state = run_state if run_state is not None else project(session, run)
    rules = (
        prefetched_rules
        if prefetched_rules is not None
        else session.query(RuleModel).filter(RuleModel.run_id == run.id).all()
    )
    tables = prefetched_tables if prefetched_tables is not None else run_tables(session, run)
    result = {
        "id": run.id,
        "spec_id": run.spec_id,
        "status": state.status,
        "current_node": state.current_node,
        "attempt": state.attempt,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "tables": tables,
        "table_count": len(tables),
        "result": state.result_json,
        "rules": [rule_view(rule) for rule in sorted(rules, key=lambda item: item.rule_key)],
    }
    if include_events:
        events = session.query(EventModel).filter(EventModel.run_id == run.id).order_by(EventModel.seq).all()
        result["events"] = [
            {
                "seq": event.seq,
                "kind": event.kind,
                "node": event.node,
                "payload": event.payload_json,
                "event_hash": event.event_hash,
                "created_at": event.created_at.isoformat(),
            }
            for event in events
        ]
    return result
