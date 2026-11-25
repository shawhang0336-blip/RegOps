"""RegOps 数据通道（Channel）规格、大对象表载荷解析与断点折叠恢复策略（策略层 Layer 2）。

定义 24 个命名数据通道（含多表汇聚与单表增量检查点通道），
并提供从 EventLog + LocalBlobStore 单次查询无损恢复状态与全部 Channel 的
`resolve_event_tables` / `recover_channels` / `build_context`。
"""

import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.infra.blob_store import LocalBlobStore
from app.infra.config import Settings
from app.infra.contracts import AuditCaseBinding, RuleDefinition
from app.infra.db_tables import EventModel, RuleModel, RunModel
from app.kernel.event_log import fold_with
from app.kernel.run import (
    BudgetGuard,
    BudgetLimits,
    ChannelSpec,
    Identity,
    RunContext,
    append_list,
    last_value,
    merge_dict,
)
from .regops_bus import default_bus
from .regops_plan import fold, fold_ingestion_event, project, project_ingestion

logger = logging.getLogger(__name__)


def resolve_event_tables(
    payload: Optional[Dict[str, Any]],
    blob_store: Optional[LocalBlobStore] = None,
) -> Dict[str, Any]:
    """从 RegOps 事件 payload 取出 tables 字典；若已卸载到 BlobStore（tables_blob），支持按指针透明读取。"""
    if not isinstance(payload, dict):
        return {}
    tables = payload.get("tables")
    if isinstance(tables, dict) and tables:
        return tables
    blob_uri = payload.get("tables_blob")
    if blob_uri and blob_store is not None:
        try:
            loaded = json.loads(blob_store.get(blob_uri).decode("utf-8"))
            if isinstance(loaded, dict):
                return loaded
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取事件 tables_blob 失败（uri=%s）: %s", blob_uri, exc)
    return tables if isinstance(tables, dict) else {}


DEFAULT_CHANNELS: Dict[str, ChannelSpec] = {
    "plans": ChannelSpec("plans", merge_dict),
    "schema_payload": ChannelSpec("schema_payload", merge_dict),
    "golden_overrides": ChannelSpec("golden_overrides", merge_dict),
    "generator_payload": ChannelSpec("generator_payload", merge_dict),
    "golden_records": ChannelSpec("golden_records", merge_dict),
    "audit_cases": ChannelSpec("audit_cases", merge_dict),
    # 单表级 BSP Send 增量检查点通道（merge_dict 屏障归约，失败续跑只重跑未完成表）
    "table_rules_done": ChannelSpec("table_rules_done", merge_dict),
    "table_generators_done": ChannelSpec("table_generators_done", merge_dict),
    "table_audit_cases_done": ChannelSpec("table_audit_cases_done", merge_dict),
    "rule_wave_ctx": ChannelSpec("rule_wave_ctx", last_value),
    "data_wave_ctx": ChannelSpec("data_wave_ctx", last_value),
    "audit_wave_ctx": ChannelSpec("audit_wave_ctx", last_value),
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


def recover_channels(
    session: Session,
    run: RunModel,
    settings: Any = None,
    events: Optional[List[EventModel]] = None,
) -> Dict[str, Any]:
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
        "table_rules_done": {},
        "table_generators_done": {},
        "table_audit_cases_done": {},
    }
    if events is None:
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
        elif kind == "TABLE_RULES_BOUND":
            tcode = payload.get("table_code")
            if tcode:
                shared["table_rules_done"] = merge_dict(
                    shared.get("table_rules_done"),
                    {tcode: {"rules": payload.get("rules") or [], "gaps": payload.get("gaps") or []}},
                )
        elif kind == "CROSS_RULES_ASSIGNED":
            shared["golden_overrides"] = merge_dict(
                shared.get("golden_overrides"), payload.get("golden_overrides") or {}
            )
        elif kind == "RULES_BOUND":
            shared["pending_count"] = int(payload.get("pending_count") or 0)
            shared["approval_origin"] = payload.get("origin") or "rule_binder"
            shared.pop("self_heal_report", None)
        elif kind == "REAUDIT_REQUESTED":
            shared["table_generators_done"] = {}
            shared["table_audit_cases_done"] = {}
        elif kind == "ORACLE_ASSETS_PREPARED":
            if payload.get("assets") is not None:
                shared["prepared_assets"] = payload["assets"]
        elif kind == "TABLE_GENERATORS_ASSIGNED":
            tcode = payload.get("table_code")
            if tcode:
                shared["table_generators_done"] = merge_dict(
                    shared.get("table_generators_done"),
                    {tcode: payload.get("generators") or []},
                )
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
        elif kind == "TABLE_AUDIT_CASES_ASSIGNED":
            tcode = payload.get("table_code")
            if tcode:
                shared["table_audit_cases_done"] = merge_dict(
                    shared.get("table_audit_cases_done"),
                    {tcode: {"cases": payload.get("cases") or [], "gaps": payload.get("gaps") or []}},
                )
        elif kind == "AUDIT_CASES_ASSIGNED":
            tables = resolve_event_tables(payload, blob_store)
            typed_cases = {}
            for code, items in tables.items():
                if isinstance(items, list):
                    try:
                        typed_cases[code] = [AuditCaseBinding.model_validate(item) for item in items]
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("断点恢复反序列化审计用例失败（run=%s, table=%s）: %s", run.id, code, exc)
            if typed_cases:
                shared["audit_cases"] = merge_dict(shared.get("audit_cases"), typed_cases)
            shared.pop("heal_cases_report", None)
        elif kind == "GO_REDIRECT":
            shared["approval_origin"] = payload.get("origin") or "self_heal"
            target_node = payload.get("target") or "rule_binder"
            # 清理之前方向遗留的自愈报告，避免连续不同方向跳转串味
            shared.pop("self_heal_report", None)
            shared.pop("heal_data_report", None)
            shared.pop("heal_cases_report", None)
            report_uri = (payload.get("failed_result") or {}).get("audit_report_blob") or payload.get("audit_report_blob")
            loaded_report = None
            if report_uri and blob_store is not None:
                try:
                    loaded_report = json.loads(blob_store.get(report_uri).decode("utf-8"))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "断点恢复读取自愈报告失败（run=%s, target=%s, uri=%s）: %s",
                        run.id,
                        target_node,
                        report_uri,
                        exc,
                    )
            if target_node == "data_generator_binder":
                if isinstance(loaded_report, dict):
                    shared["heal_data_report"] = loaded_report
                    affected = {
                        c.get("table_code")
                        for c in loaded_report.get("cases", [])
                        if c.get("case_kind") == "GOLDEN" and c.get("unexpected_rules") and c.get("table_code")
                    }
                    if affected:
                        for tcode in affected:
                            shared["table_generators_done"].pop(tcode, None)
                            shared["table_audit_cases_done"].pop(tcode, None)
                    else:
                        shared["table_generators_done"] = {}
                        shared["table_audit_cases_done"] = {}
                else:
                    # 报告缺失或读取失败时保守清空下游单表缓存，绝不误走 skip_fanout 跳过重造
                    shared["table_generators_done"] = {}
                    shared["table_audit_cases_done"] = {}
            elif target_node == "audit_case_binder":
                if isinstance(loaded_report, dict):
                    shared["heal_cases_report"] = loaded_report
                    affected = {
                        c.get("table_code")
                        for c in loaded_report.get("cases", [])
                        if c.get("missing_rules") and c.get("table_code")
                    }
                    if affected:
                        for tcode in affected:
                            shared["table_audit_cases_done"].pop(tcode, None)
                    else:
                        shared["table_audit_cases_done"] = {}
                else:
                    shared["table_audit_cases_done"] = {}
            else:
                if isinstance(loaded_report, dict):
                    shared["self_heal_report"] = loaded_report
                shared["table_generators_done"] = {}
                shared["table_audit_cases_done"] = {}

    approved_rows = (
        session.query(RuleModel)
        .filter(RuleModel.run_id == run.id, RuleModel.status == "APPROVED")
        .all()
    )
    if approved_rows:
        try:
            shared["rules"] = [RuleDefinition.model_validate(row.definition_json) for row in approved_rows]
        except Exception as exc:  # noqa: BLE001
            logger.warning("断点恢复反序列化已批准规则失败（run=%s）: %s", run.id, exc)
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
    """从入口参数组装 RunContext：单次查询 EventModel 同时折叠 RunState 与全部 Channel 快照。"""
    events = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id)
        .order_by(EventModel.seq)
        .all()
    )
    state = fold(events)
    shared = recover_channels(session, run, settings, events=events)
    bus = default_bus()
    prior_usage = (state.result_json or {}).get("budget_usage") if isinstance(state.result_json, dict) else None
    budget_guard = BudgetGuard(
        BudgetLimits.from_dict(budget),
        run_id=run.id,
        seed=prior_usage if isinstance(prior_usage, dict) else None,
    ).attach(bus)
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


INGESTION_CHANNELS: Dict[str, ChannelSpec] = {
    "document_id": ChannelSpec("document_id", last_value),
    "phase": ChannelSpec("phase", last_value),
    "blocks_completed": ChannelSpec("blocks_completed", merge_dict),
    "block_meta_outputs": ChannelSpec("block_meta_outputs", merge_dict),
    "block_codeset_outputs": ChannelSpec("block_codeset_outputs", merge_dict),
    "block_errors": ChannelSpec("block_errors", append_list),
    "extract_result": ChannelSpec("extract_result", last_value),
}


def recover_ingestion_checkpoint(
    session: Session,
    document_id: str,
    *,
    exclude_run_id: Optional[str] = None,
) -> Dict[str, str]:
    """从最近一次失败/中断的 Step 1 提取 Run 恢复已落库的内容块检查点（若上次已完成则全新重跑）。"""
    rows = (
        session.query(RunModel)
        .filter(RunModel.spec_id == document_id)
        .order_by(RunModel.created_at.desc())
        .all()
    )
    completed: Dict[str, str] = {}
    found_extract = False
    for prev_run in rows:
        if exclude_run_id and prev_run.id == exclude_run_id:
            continue
        first = (
            session.query(EventModel)
            .filter(EventModel.run_id == prev_run.id)
            .order_by(EventModel.seq.asc())
            .first()
        )
        if not first or not isinstance(first.payload_json, dict):
            continue
        if first.payload_json.get("run_kind") != "INGESTION" or first.payload_json.get("phase") != "extract":
            continue
        found_extract = True
        state = project_ingestion(session, prev_run)
        if state.status == "COMPLETED":
            break
        if state.status == "FAILED":
            prev_completed = (state.result_json or {}).get("completed_blocks") or {}
            if isinstance(prev_completed, dict):
                for key, val in prev_completed.items():
                    completed.setdefault(str(key), str(val or ""))
            continue
        break
    return completed if found_extract else {}


def build_ingestion_context(
    session: Session,
    run: RunModel,
    settings: Settings,
    *,
    budget: Optional[Dict[str, Any]] = None,
    concurrency: int = 4,
    cancellation: Any = None,
    shared: Optional[Dict[str, Any]] = None,
    bus: Optional[Any] = None,
) -> RunContext:
    """组装 Step 0/1 文档导入/解析专用 `RunContext`，挂载 `INGESTION_CHANNELS` 与双级预算守卫。"""
    events = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id)
        .order_by(EventModel.seq)
        .all()
    )
    state = fold_with(events, reducer=fold_ingestion_event)
    active_bus = bus if bus is not None else default_bus()
    prior_usage = (state.result_json or {}).get("budget_usage") if isinstance(state.result_json, dict) else None
    budget_guard = BudgetGuard(
        BudgetLimits.from_dict(budget),
        run_id=run.id,
        seed=prior_usage if isinstance(prior_usage, dict) else None,
    ).attach(active_bus)
    return RunContext(
        session=session,
        run=run,
        settings=settings,
        state=state,
        shared=dict(shared or {}),
        channels=dict(INGESTION_CHANNELS),
        bus=active_bus,
        budget=budget,
        concurrency=max(1, int(concurrency or 1)),
        current_node=state.current_node,
        projector=project_ingestion,
        reducer=fold_ingestion_event,
        cancellation=cancellation,
        budget_guard=budget_guard,
    )
