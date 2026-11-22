"""RegOps 流程拓扑、人工中断协议与统一事件状态折叠策略（策略层 Layer 2）。

将监管报送两阶段 Run 的状态机策略统一插拔装配到 ProdAgent 微内核（`app.kernel`），且无任何 import 期全局副作用：
1. Step 2 规则生成与沙箱审计：18 节点拓扑（`DEFAULT_PLAN`）、人工中断门协议（`INTERRUPT_*`）与事件折叠（`_apply` / `fold` / `project` / `project_many`）；
2. Step 1 文档导入与解析（`run_kind == "INGESTION"`）：共享 `RunModel` + `EventModel` 账本的事件折叠（`fold_ingestion_event` / `project_ingestion` / `project_ingestion_many`）。
"""

from dataclasses import replace
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.infra.db_tables import EventModel, RunModel
from app.kernel.event_log import (
    RunState,
    fold_with,
    project_many_with,
    project_with,
)
from app.kernel.interrupt import make_interrupt_payload, pending_interrupt_with
from app.kernel.plan import PlanEdge, PlanNode, RetryPolicy, WorkflowPlan

# =============================================================================
# Step 2：规则生成与 Oracle 沙箱审计拓扑（18 节点）与人工门协议
# =============================================================================

# 条件标签语义（执行器按此查边）：
#   always                  无条件后继
#   fanout                  BSP 扇出至并行单表 Worker
#   skip_fanout             所有表已在断点缓存中完成 → 直达 Join 节点
#   direct                  单表/直通分支，跳过扇出与汇聚
#   pending_rules           规则绑定后有 PENDING → 人工确权门
#   no_pending_rules        规则绑定后无 PENDING → 直接进入造数
#   all_decided             人工全部决断 → 进入造数
#   partial_decided         部分决断 → 继续等（自环）
#   audit_passed            Oracle 审计全通过 → 测试确认门
#   audit_failed_self_heal  失败且需修正规则 → 回到规则绑定重绑
#   audit_failed_heal_data  黄金样本误报 → 回到造数节点修正数据
#   audit_failed_heal_cases 确定性规则漏报 → 回到审计用例节点重构反例
#   audit_rejected          失败且预算耗尽 → 终态
#   confirmed               测试确认 → 导出终态
DEFAULT_NODES: Tuple[PlanNode, ...] = (
    PlanNode("created", "已创建", "stage"),
    PlanNode("generation_planner", "生成规划", "stage"),
    PlanNode("schema_mapper", "表结构映射", "stage"),
    PlanNode("rule_binder", "校验规则派发", "stage"),
    PlanNode("rule_table_worker", "单表规则并行绑定", "stage"),
    PlanNode("rule_join", "规则波次汇聚", "stage"),
    PlanNode("oracle_asset_builder", "Oracle 资产准备", "stage"),
    PlanNode("human_gate", "规则人工确权", "gate"),
    PlanNode("data_generator_binder", "黄金造数派发", "stage"),
    PlanNode("data_table_worker", "单表黄金造数并行", "stage"),
    PlanNode("data_join", "造数波次汇聚", "stage"),
    PlanNode("audit_case_binder", "审计用例派发", "stage"),
    PlanNode("audit_table_worker", "单表变异用例并行", "stage"),
    PlanNode("audit_join", "用例波次汇聚", "stage"),
    PlanNode(
        "oracle_executor",
        "Oracle 沙箱审计",
        "stage",
        retry=RetryPolicy(max_attempts=2, base_delay=0.3, retry_on=(ConnectionError, TimeoutError)),
    ),
    PlanNode("test_confirmation", "测试结果确认", "gate"),
    PlanNode("export", "产物导出", "terminal"),
    PlanNode("failed", "失败", "terminal"),
)

DEFAULT_EDGES: Tuple[PlanEdge, ...] = (
    PlanEdge("created", "generation_planner", "always"),
    PlanEdge("generation_planner", "schema_mapper", "always"),
    PlanEdge("schema_mapper", "rule_binder", "always"),
    PlanEdge("rule_binder", "rule_table_worker", "fanout"),
    PlanEdge("rule_binder", "rule_join", "skip_fanout"),
    PlanEdge("rule_binder", "oracle_asset_builder", "direct"),
    PlanEdge("rule_table_worker", "rule_join", "always"),
    PlanEdge("rule_join", "oracle_asset_builder", "always"),
    PlanEdge("oracle_asset_builder", "human_gate", "pending_rules"),
    PlanEdge("oracle_asset_builder", "data_generator_binder", "no_pending_rules"),
    PlanEdge("human_gate", "data_generator_binder", "all_decided"),
    PlanEdge("human_gate", "human_gate", "partial_decided"),
    PlanEdge("data_generator_binder", "data_table_worker", "fanout"),
    PlanEdge("data_generator_binder", "data_join", "skip_fanout"),
    PlanEdge("data_generator_binder", "audit_case_binder", "direct"),
    PlanEdge("data_table_worker", "data_join", "always"),
    PlanEdge("data_join", "audit_case_binder", "always"),
    PlanEdge("audit_case_binder", "audit_table_worker", "fanout"),
    PlanEdge("audit_case_binder", "audit_join", "skip_fanout"),
    PlanEdge("audit_case_binder", "oracle_executor", "direct"),
    PlanEdge("audit_table_worker", "audit_join", "always"),
    PlanEdge("audit_join", "oracle_executor", "always"),
    PlanEdge("oracle_executor", "rule_binder", "audit_failed_self_heal"),
    PlanEdge("oracle_executor", "data_generator_binder", "audit_failed_heal_data"),
    PlanEdge("oracle_executor", "audit_case_binder", "audit_failed_heal_cases"),
    PlanEdge("oracle_executor", "test_confirmation", "audit_passed"),
    PlanEdge("oracle_executor", "failed", "audit_rejected"),
    PlanEdge("test_confirmation", "export", "confirmed"),
)

DEFAULT_PLAN = WorkflowPlan(
    name="regops_generation",
    nodes=DEFAULT_NODES,
    edges=DEFAULT_EDGES,
    entry="created",
)

# 中断类型 → 恢复节点（与 Plan 节点名严格一致）
INTERRUPT_RESUME_NODES: Dict[str, str] = {
    "APPROVAL_REQUIRED": "human_gate",
    "TEST_CONFIRMATION_REQUIRED": "test_confirmation",
}

INTERRUPT_ANSWER_KINDS: Dict[str, str] = {
    "APPROVAL_REQUIRED": "HUMAN_DECISIONS_RECORDED",
    "TEST_CONFIRMATION_REQUIRED": "TEST_RESULTS_CONFIRMED",
}

ACTIVE_RUN_STATUSES = frozenset({"RUNNING", "AUDITING", "READY_TO_AUDIT"})


def _require_next(from_node: str, when: str, label: str) -> str:
    target = DEFAULT_PLAN.next(from_node, when)
    if not target:
        raise RuntimeError(f"Plan 缺少{label}（{from_node} --{when}--> ?）")
    return target


def is_gate(name: str) -> bool:
    """中断点：执行到此处必须停下等人工输入。"""
    return DEFAULT_PLAN.node(name).kind == "gate"


def self_heal_target() -> str:
    """审计失败且需修正规则时回到的节点。"""
    return _require_next("oracle_executor", "audit_failed_self_heal", "自愈回边")


def self_heal_data_target() -> str:
    """黄金样本误报时回到造数节点修正测试数据，绝不削弱规则。"""
    return _require_next("oracle_executor", "audit_failed_heal_data", "黄金造数自愈回边")


def self_heal_cases_target() -> str:
    """确定性/显性规则漏报且需重构反例时回到审计用例节点。"""
    return _require_next("oracle_executor", "audit_failed_heal_cases", "变异用例自愈回边")


def approval_target(pending: bool) -> str:
    """规则确权后前进到的节点；pending=True 停人工门，否则直接进造数。"""
    return _require_next(
        "oracle_asset_builder", "pending_rules" if pending else "no_pending_rules", "确权后继边"
    )


def decided_target(all_decided: bool) -> str:
    """人工确权后的后继节点。"""
    return _require_next("human_gate", "all_decided" if all_decided else "partial_decided", "确权后继边")


def audit_passed_target() -> str:
    """Oracle 审计全通过后的测试确认门节点。"""
    return _require_next("oracle_executor", "audit_passed", "审计通过边")


def interrupt_payload(
    kind: str,
    question: str,
    pending_input: Dict[str, Any],
    identity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构造一条 RegOps Interrupt 事件的 payload。"""
    return make_interrupt_payload(
        kind=kind,
        question=question,
        pending_input=pending_input,
        resume_nodes=INTERRUPT_RESUME_NODES,
        identity=identity,
    )


def pending_interrupt(session: Session, run: RunModel, kind: str) -> Optional[EventModel]:
    """找最近一条尚未被回答的指定类型中断事件。"""
    return pending_interrupt_with(session, run, kind, INTERRUPT_ANSWER_KINDS)


def _apply(state: RunState, kind: str, payload: Dict[str, Any]) -> RunState:
    """把一条 Step 2 RegOps 业务事件折进 RunState（纯函数）。"""
    payload = payload or {}
    if kind == "RUN_CREATED":
        return replace(
            state,
            status="RUNNING",
            current_node="created",
            when="always",
            pending_target=None,
            resume_from_node="created",
            resume_when="always",
        )
    if kind == "GENERATION_PLANNED":
        return replace(
            state,
            current_node="generation_planner",
            when="always",
            pending_target=None,
            resume_from_node="generation_planner",
            resume_when="always",
        )
    if kind == "SCHEMA_ASSIGNED":
        return replace(
            state,
            current_node="schema_mapper",
            when="always",
            pending_target=None,
            resume_from_node="schema_mapper",
            resume_when="always",
        )
    if kind == "RULES_BOUND":
        pending = bool(payload.get("pending_count"))
        return replace(
            state,
            status="WAITING_APPROVAL" if pending else "READY_TO_AUDIT",
            current_node="rule_binder",
            when="direct",
            pending_target=None,
            resume_from_node="rule_binder",
            resume_when="direct",
        )
    if kind == "ORACLE_ASSETS_PREPARED":
        has_pending = (
            bool(payload["pending_count"]) if "pending_count" in payload else (state.status == "WAITING_APPROVAL")
        )
        when = "pending_rules" if has_pending else "no_pending_rules"
        return replace(
            state,
            current_node="oracle_asset_builder",
            result_json=payload.get("result"),
            when=when,
            pending_target=None,
            resume_from_node="oracle_asset_builder",
            resume_when=when,
        )
    if kind == "APPROVAL_REQUIRED":
        return replace(
            state,
            status="WAITING_APPROVAL",
            current_node="human_gate",
            when="partial_decided",
            pending_target=None,
            resume_from_node="human_gate",
            resume_when="partial_decided",
        )
    if kind == "HUMAN_DECISIONS_RECORDED":
        decided = bool(payload.get("all_decided"))
        when = "all_decided" if decided else "partial_decided"
        return replace(
            state,
            status="READY_TO_AUDIT" if decided else "WAITING_APPROVAL",
            current_node="human_gate",
            when=when,
            pending_target=None,
            resume_from_node="human_gate",
            resume_when=when,
        )
    if kind == "REAUDIT_REQUESTED":
        return replace(
            state,
            status="READY_TO_AUDIT",
            current_node="human_gate",
            when="all_decided",
            pending_target=None,
            resume_from_node="human_gate",
            resume_when="all_decided",
        )
    if kind == "GENERATORS_ASSIGNED":
        return replace(
            state,
            status="RUNNING",
            current_node="data_generator_binder",
            when="direct",
            pending_target=None,
            resume_from_node="data_generator_binder",
            resume_when="direct",
        )
    if kind == "AUDIT_CASES_ASSIGNED":
        return replace(
            state,
            status="RUNNING",
            current_node="audit_case_binder",
            when="direct",
            pending_target=None,
            resume_from_node="audit_case_binder",
            resume_when="direct",
        )
    if kind == "TEST_CONFIRMATION_REQUIRED":
        return replace(
            state,
            status="WAITING_TEST_APPROVAL",
            current_node="test_confirmation",
            when="confirmed",
            pending_target=None,
            resume_from_node="test_confirmation",
            resume_when="confirmed",
        )
    if kind == "TEST_RESULTS_CONFIRMED":
        return replace(
            state,
            status="COMPLETED",
            current_node="export",
            when="confirmed",
            pending_target=None,
            resume_from_node="export",
            resume_when="confirmed",
        )
    if kind == "AUDIT_STARTED":
        return replace(
            state,
            status="AUDITING",
            current_node="oracle_executor",
            attempt=payload.get("attempt", state.attempt),
            when="always",
            pending_target=None,
        )
    if kind == "GO_REDIRECT":
        target = payload.get("target")
        if not target or not any(node.name == target for node in DEFAULT_PLAN.nodes):
            raise ValueError(f"GO_REDIRECT 目标 {target} 不在 Plan 中")
        return replace(
            state,
            status="RUNNING",
            current_node=target,
            when="always",
            pending_target=target,
            resume_from_node=target,
            resume_when="always",
        )
    if kind == "AUDIT_COMPLETED":
        return replace(
            state,
            status="COMPLETED",
            current_node="oracle_executor",
            result_json=payload.get("result"),
            when="audit_passed",
            pending_target=None,
            resume_from_node="oracle_executor",
            resume_when="audit_passed",
        )
    if kind == "AUDIT_REJECTED":
        return replace(
            state,
            status="FAILED",
            current_node="failed",
            result_json=payload.get("result"),
            when="audit_rejected",
            pending_target=None,
        )
    if kind == "RUN_FAILED":
        return replace(
            state,
            status="FAILED",
            current_node=payload.get("node", state.current_node),
            result_json=payload.get("result"),
            resume_from_node=payload.get("resume_from_node") or state.resume_from_node,
            resume_when=payload.get("resume_when") or state.resume_when,
        )
    if kind == "RUN_CANCELLED":
        return replace(
            state,
            status="FAILED",
            current_node=payload.get("node", state.current_node),
            result_json=payload.get("result") or {"error": payload.get("reason", "Run cancelled"), "cancelled": True},
            resume_from_node=payload.get("resume_from_node") or state.resume_from_node,
            resume_when=payload.get("resume_when") or state.resume_when,
        )
    if kind == "RUN_RESUMED":
        resume_node = payload.get("resume_from_node") or state.resume_from_node
        resume_when = payload.get("resume_when") or state.resume_when
        pending_target = payload.get("pending_target") if "pending_target" in payload else state.pending_target
        restored_status = "READY_TO_AUDIT" if resume_node == "human_gate" and not pending_target else "RUNNING"
        return replace(
            state,
            status=restored_status,
            current_node=resume_node,
            when=resume_when,
            pending_target=pending_target,
        )
    return state


fold_regops_event = _apply


def fold(events: Iterable[EventModel]) -> RunState:
    """将 Step 2 规则生成事件流纯函数折叠为 RunState。"""
    return fold_with(events, reducer=_apply)


def project(session: Session, run: RunModel) -> RunState:
    """显式按 Step 2 RegOps Reducer 折叠单条 Run 的最新状态（无全局注册表依赖）。"""
    return project_with(session, run, reducer=_apply)


def project_many(session: Session, runs: Iterable[RunModel]) -> Dict[str, RunState]:
    """显式按 Step 2 RegOps Reducer 批量折叠多条 Run 状态。"""
    return project_many_with(session, runs, reducer=_apply)


# =============================================================================
# Step 1：文档导入与解析（Ingestion Run）事件折叠
# =============================================================================

ACTIVE_INGESTION_STATUSES = frozenset({"NEW", "RUNNING", "WORKING"})
_MAX_FOLDED_LOGS = 40


def _merge_ingestion_result(base: Any, patch: Dict[str, Any]) -> Dict[str, Any]:
    current = dict(base) if isinstance(base, dict) else {}
    counters = dict(current.get("counters") or {})
    if isinstance(patch.get("counters"), dict):
        counters.update(patch["counters"])
    logs: List[Dict[str, Any]] = list(current.get("logs") or [])
    if patch.get("log"):
        entry = patch["log"]
        if isinstance(entry, str):
            logs.append({"at": patch.get("at") or 0, "text": entry, "stage": patch.get("stage", current.get("stage", 0))})
        elif isinstance(entry, dict):
            logs.append(entry)
        if len(logs) > _MAX_FOLDED_LOGS:
            logs = logs[-_MAX_FOLDED_LOGS:]
    merged = {**current, **{k: v for k, v in patch.items() if k not in {"counters", "log"}}}
    merged["counters"] = counters
    merged["logs"] = logs
    return merged


def fold_ingestion_event(state: RunState, kind: str, payload: Dict[str, Any]) -> RunState:
    """将单条 Step 1 文档解析事件折叠为新的 RunState（纯函数）。"""
    payload = payload or {}
    if kind == "RUN_CREATED":
        base = {
            "run_kind": payload.get("run_kind") or "INGESTION",
            "document_id": payload.get("document_id"),
            "file_name": payload.get("file_name") or "",
            "phase": payload.get("phase") or "prepare",
            "stage": int(payload.get("stage") or 1),
            "progress_status": "WORKING",
            "detail": payload.get("detail") or "任务已创建，准备执行",
            "counters": dict(payload.get("counters") or {}),
            "logs": [],
        }
        return replace(
            state,
            status="RUNNING",
            current_node=payload.get("phase") or "created",
            result_json=base,
            when="always",
        )
    if kind == "INGESTION_STAGE":
        patch: Dict[str, Any] = {}
        for key in ("stage", "detail", "counters", "log", "at", "file_name", "phase", "budget_usage"):
            if key in payload and payload[key] is not None:
                patch[key] = payload[key]
        if payload.get("progress_status"):
            patch["progress_status"] = payload["progress_status"]
        merged = _merge_ingestion_result(state.result_json, patch)
        return replace(
            state,
            status="RUNNING",
            current_node=payload.get("node") or state.current_node,
            result_json=merged,
            when="always",
        )
    if kind == "INGESTION_COMPLETED":
        patch = {
            "progress_status": payload.get("progress_status") or "DONE",
            "detail": payload.get("detail") or "解析完成",
            "stage": int(payload.get("stage") or 4),
        }
        for key in ("counters", "log", "at", "budget_usage", "result", "document_status"):
            if key in payload and payload[key] is not None:
                patch[key] = payload[key]
        merged = _merge_ingestion_result(state.result_json, patch)
        return replace(
            state,
            status="COMPLETED",
            current_node=payload.get("node") or "completed",
            result_json=merged,
            when="always",
        )
    if kind == "RUN_CANCELLED":
        raw_res = payload.get("result")
        res_patch = dict(raw_res) if isinstance(raw_res, dict) else ({"error": str(raw_res)} if raw_res else {})
        reason = payload.get("reason") or res_patch.get("error") or "Run cancelled by operator"
        res_patch.setdefault("error", reason)
        res_patch["cancelled"] = True
        res_patch["progress_status"] = "FAILED"
        res_patch["detail"] = f"已人工中止：{reason}"
        merged = _merge_ingestion_result(state.result_json, res_patch)
        return replace(
            state,
            status="FAILED",
            current_node=payload.get("node") or state.current_node,
            result_json=merged,
        )
    if kind == "RUN_FAILED":
        raw_res = payload.get("result")
        res_patch = dict(raw_res) if isinstance(raw_res, dict) else ({"error": str(raw_res)} if raw_res else {})
        res_patch["progress_status"] = "FAILED"
        if res_patch.get("error") and not res_patch.get("detail"):
            res_patch["detail"] = str(res_patch["error"])
        merged = _merge_ingestion_result(state.result_json, res_patch)
        return replace(
            state,
            status="FAILED",
            current_node=payload.get("node") or state.current_node,
            result_json=merged,
        )
    return state


def project_ingestion(session: Session, run: RunModel) -> RunState:
    """显式按 Step 1 文档导入 Reducer 折叠单条 Run 的最新状态。"""
    return project_with(session, run, reducer=fold_ingestion_event)


def project_ingestion_many(session: Session, runs: Iterable[RunModel]) -> Dict[str, RunState]:
    """显式按 Step 1 文档导入 Reducer 批量折叠多条 Run 的状态。"""
    return project_many_with(session, runs, reducer=fold_ingestion_event)
