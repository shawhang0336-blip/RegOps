"""RegOps 数据通道（Channel）规格与断点折叠恢复策略（策略层）。

定义 16 个命名数据通道（含 6 个 merge_dict 多表汇聚通道与 10 个 last_value 标量/控制通道），
并提供从 EventLog + BlobStore 无损恢复全部 Channel 的 recover_channels 与 build_context。
"""

import json
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.infra.config import Settings
from app.infra.domain import AuditCaseBinding, RuleDefinition
from app.kernel.blob_store import LocalBlobStore
from app.kernel.budget import BudgetGuard, BudgetLimits
from app.kernel.channel import ChannelSpec, Identity, RunContext, last_value, merge_dict, resolve_event_tables
from app.infra.models import EventModel, RuleModel, RunModel
from .regops_bus import default_bus
from .regops_plan import project

DEFAULT_CHANNELS: Dict[str, ChannelSpec] = {
    "plans": ChannelSpec("plans", merge_dict),
    "schema_payload": ChannelSpec("schema_payload", merge_dict),
    "golden_overrides": ChannelSpec("golden_overrides", merge_dict),
    "generator_payload": ChannelSpec("generator_payload", merge_dict),
    "golden_records": ChannelSpec("golden_records", merge_dict),
    "audit_cases": ChannelSpec("audit_cases", merge_dict),
    "use_plan": ChannelSpec("use_plan", last_value),
    "pending_count": ChannelSpec("pending_count", last_value),
    "rules": ChannelSpec("rules", last_value),
    "prepared_assets": ChannelSpec("prepared_assets", last_value),
    "report": ChannelSpec("report", last_value),
    "result": ChannelSpec("result", last_value),
    "attempt": ChannelSpec("attempt", last_value),
    "first_attempt": ChannelSpec("first_attempt", last_value),
    "self_heal_report": ChannelSpec("self_heal_report", last_value),
    "heal_data_report": ChannelSpec("heal_data_report", last_value),
    "heal_cases_report": ChannelSpec("heal_cases_report", last_value),
    "approval_origin": ChannelSpec("approval_origin", last_value),
}


def recover_channels(session: Session, run: RunModel, settings: Any = None) -> Dict[str, Any]:
    """从 EventLog + BlobStore 折叠恢复跨节点 Channel 状态。

    任何时刻中断（人工门等待、进程重启、网络超时），新建 RunContext 都能
    无损还原当时已产出的 plans / schema_payload / prepared_assets / golden_records / audit_cases / self_heal_report。
    """
    blob_dir = getattr(settings, "blob_dir", None)
    blob_store = LocalBlobStore(blob_dir) if isinstance(blob_dir, str) and blob_dir else None

    shared: Dict[str, Any] = {
        "plans": {},
        "schema_payload": {},
        "golden_overrides": {},
    }
    events = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id)
        .order_by(EventModel.seq)
        .all()
    )
    for event in events:
        kind = event.kind
        payload = event.payload_json or {}
        if kind == "GENERATION_PLANNED":
            shared["plans"] = merge_dict(shared.get("plans"), resolve_event_tables(payload, blob_store))
            shared["use_plan"] = True
        elif kind == "SCHEMA_ASSIGNED":
            shared["schema_payload"] = merge_dict(shared.get("schema_payload"), resolve_event_tables(payload, blob_store))
        elif kind == "CROSS_RULES_ASSIGNED":
            shared["golden_overrides"] = merge_dict(
                shared.get("golden_overrides"), payload.get("golden_overrides") or {}
            )
        elif kind == "RULES_BOUND":
            shared["pending_count"] = int(payload.get("pending_count") or 0)
            shared["approval_origin"] = payload.get("origin") or "rule_binder"
            shared.pop("self_heal_report", None)
        elif kind == "ORACLE_ASSETS_PREPARED":
            if payload.get("assets") is not None:
                shared["prepared_assets"] = payload["assets"]
        elif kind == "GENERATORS_ASSIGNED":
            tables = resolve_event_tables(payload, blob_store)
            shared["generator_payload"] = merge_dict(shared.get("generator_payload"), tables)
            golden = {
                code: item.get("golden_record", {})
                for code, item in tables.items()
                if isinstance(item, dict) and "golden_record" in item
            }
            shared["golden_records"] = merge_dict(shared.get("golden_records"), golden)
            shared.pop("heal_data_report", None)
        elif kind == "AUDIT_CASES_ASSIGNED":
            tables = resolve_event_tables(payload, blob_store)
            typed_cases = {}
            for code, items in tables.items():
                if isinstance(items, list):
                    try:
                        typed_cases[code] = [AuditCaseBinding.model_validate(item) for item in items]
                    except Exception:
                        pass
            if typed_cases:
                shared["audit_cases"] = merge_dict(shared.get("audit_cases"), typed_cases)
            shared.pop("heal_cases_report", None)
        elif kind == "GO_REDIRECT":
            shared["approval_origin"] = payload.get("origin") or "self_heal"
            target_node = payload.get("target") or "rule_binder"
            report_uri = (payload.get("failed_result") or {}).get("audit_report_blob") or payload.get("audit_report_blob")
            if report_uri and blob_store is not None:
                try:
                    loaded_report = json.loads(blob_store.get(report_uri).decode("utf-8"))
                    if target_node == "data_generator_binder":
                        shared["heal_data_report"] = loaded_report
                    elif target_node == "audit_case_binder":
                        shared["heal_cases_report"] = loaded_report
                    else:
                        shared["self_heal_report"] = loaded_report
                except Exception:
                    pass

    approved_rows = (
        session.query(RuleModel)
        .filter(RuleModel.run_id == run.id, RuleModel.status == "APPROVED")
        .all()
    )
    if approved_rows:
        try:
            shared["rules"] = [RuleDefinition.model_validate(row.definition_json) for row in approved_rows]
        except Exception:
            pass
    return shared


def build_context(
    session: Session,
    run: RunModel,
    settings: Settings,
    llm=None,
    budget=None,
    concurrency: int = 4,
    operator: str = "UNSPECIFIED_OPERATOR",
    approver: Optional[str] = None,
    editor: Optional[str] = None,
    attempt_base: Optional[int] = None,
    cancellation: Any = None,
) -> RunContext:
    """从入口参数组装 RunContext，注入 RegOps 通道规格与门禁总线，并从事件账本自动折叠恢复 Channel 快照。"""
    state = project(session, run)
    shared = recover_channels(session, run, settings)
    bus = default_bus()
    budget_guard = BudgetGuard(BudgetLimits.from_dict(budget), run_id=run.id).attach(bus)
    return RunContext(
        session=session,
        run=run,
        settings=settings,
        state=state,
        identity=Identity(operator=operator, approver=approver, editor=editor),
        shared=shared,
        channels=dict(DEFAULT_CHANNELS),
        bus=bus,
        llm=llm,
        budget=budget,
        concurrency=concurrency,
        attempt_base=attempt_base,
        current_node=state.current_node,
        projector=project,
        cancellation=cancellation,
        budget_guard=budget_guard,
    )
