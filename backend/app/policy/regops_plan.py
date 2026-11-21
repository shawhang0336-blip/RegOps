"""RegOps 监管数据质量装配拓扑与事件折叠策略（策略层）。

将 RegOps 专属的 12 节点流水线拓扑（DEFAULT_PLAN）、人工中断门协议（INTERRUPT_RESUME_NODES）
与业务事件状态折叠函数（_apply）插拔注册到 ProdAgent 微内核（app.kernel）。
"""

from dataclasses import replace
from typing import Any, Dict, Iterable, Optional, Tuple

from sqlalchemy.orm import Session

from app.kernel.event_log import (
    RunState,
    fold_with,
    project_many_with,
    project_with,
    register_default_reducer,
)
from app.kernel.interrupt import make_interrupt_payload, pending_interrupt_with
from app.kernel.plan import PlanEdge, PlanNode, WorkflowPlan
from app.infra.models import EventModel, RunModel

# 条件标签语义（执行器按此查边）：
#   always                 无条件后继
#   pending_rules          规则绑定后有 PENDING → 人工确权门
#   no_pending_rules       规则绑定后无 PENDING → 直接进入造数
#   all_decided            人工全部决断 → 进入造数
#   partial_decided        部分决断 → 继续等（自环）
#   audit_passed           Oracle 审计全通过 → 测试确认门
#   audit_failed_self_heal 失败且还有自愈预算 → 回到规则绑定重绑
#   audit_rejected         失败且预算耗尽 → 终态
#   confirmed              测试确认 → 导出终态
DEFAULT_NODES: Tuple[PlanNode, ...] = (
    PlanNode("created", "已创建", "stage"),
    PlanNode("generation_planner", "生成规划", "stage"),
    PlanNode("schema_mapper", "表结构映射", "stage"),
    PlanNode("rule_binder", "校验规则绑定", "stage"),
    PlanNode("oracle_asset_builder", "Oracle 资产准备", "stage"),
    PlanNode("human_gate", "规则人工确权", "gate"),
    PlanNode("data_generator_binder", "黄金数据生成", "stage"),
    PlanNode("audit_case_binder", "审计用例绑定", "stage"),
    PlanNode("oracle_executor", "Oracle 沙箱审计", "stage"),
    PlanNode("test_confirmation", "测试结果确认", "gate"),
    PlanNode("export", "产物导出", "terminal"),
    PlanNode("failed", "失败", "terminal"),
)

DEFAULT_EDGES: Tuple[PlanEdge, ...] = (
    PlanEdge("created", "generation_planner", "always"),
    PlanEdge("generation_planner", "schema_mapper", "always"),
    PlanEdge("schema_mapper", "rule_binder", "always"),
    PlanEdge("rule_binder", "oracle_asset_builder", "always"),
    PlanEdge("oracle_asset_builder", "human_gate", "pending_rules"),
    PlanEdge("oracle_asset_builder", "data_generator_binder", "no_pending_rules"),
    PlanEdge("human_gate", "data_generator_binder", "all_decided"),
    PlanEdge("human_gate", "human_gate", "partial_decided"),
    PlanEdge("data_generator_binder", "audit_case_binder", "always"),
    PlanEdge("audit_case_binder", "oracle_executor", "always"),
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


def is_gate(name: str) -> bool:
    """中断点：执行到此处必须停下等人工输入。"""
    return DEFAULT_PLAN.node(name).kind == "gate"


def self_heal_target() -> str:
    """审计失败且需修正规则时回到的节点。"""
    target = DEFAULT_PLAN.next("oracle_executor", "audit_failed_self_heal")
    if not target:
        raise RuntimeError("Plan 缺少自愈回边")
    return target


def self_heal_data_target() -> str:
    """黄金样本误报时回到造数节点修正测试数据，绝不削弱规则。"""
    target = DEFAULT_PLAN.next("oracle_executor", "audit_failed_heal_data")
    if not target:
        raise RuntimeError("Plan 缺少黄金造数自愈回边")
    return target


def self_heal_cases_target() -> str:
    """确定性/显性规则漏报且需重构反例时回到审计用例节点。"""
    target = DEFAULT_PLAN.next("oracle_executor", "audit_failed_heal_cases")
    if not target:
        raise RuntimeError("Plan 缺少变异用例自愈回边")
    return target


def approval_target(pending: bool) -> str:
    """规则确权后前进到的节点；pending=True 停人工门，否则直接进造数。"""
    target = DEFAULT_PLAN.next(
        "oracle_asset_builder", "pending_rules" if pending else "no_pending_rules"
    )
    if not target:
        raise RuntimeError("Plan 缺少确权后继边")
    return target


def decided_target(all_decided: bool) -> str:
    """人工确权后的后继节点。"""
    target = DEFAULT_PLAN.next("human_gate", "all_decided" if all_decided else "partial_decided")
    if not target:
        raise RuntimeError("Plan 缺少确权后继边")
    return target


def audit_passed_target() -> str:
    target = DEFAULT_PLAN.next("oracle_executor", "audit_passed")
    if not target:
        raise RuntimeError("Plan 缺少审计通过边")
    return target


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
    """把一条 RegOps 业务事件折进 RunState。"""
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
            when="always",
            pending_target=None,
            resume_from_node="rule_binder",
            resume_when="always",
        )
    if kind == "ORACLE_ASSETS_PREPARED":
        when = "pending_rules" if state.status == "WAITING_APPROVAL" else "no_pending_rules"
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
            when="always",
            pending_target=None,
            resume_from_node="data_generator_binder",
            resume_when="always",
        )
    if kind == "AUDIT_CASES_ASSIGNED":
        return replace(
            state,
            status="RUNNING",
            current_node="audit_case_binder",
            when="always",
            pending_target=None,
            resume_from_node="audit_case_binder",
            resume_when="always",
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
        target = payload["target"]
        if not any(node.name == target for node in DEFAULT_PLAN.nodes):
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
        )
    if kind == "RUN_CANCELLED":
        return replace(
            state,
            status="FAILED",
            current_node=payload.get("node", state.current_node),
            result_json=payload.get("result") or {"error": payload.get("reason", "Run cancelled"), "cancelled": True},
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


register_default_reducer(_apply)


def fold(events: Iterable[EventModel]) -> RunState:
    return fold_with(events, reducer=_apply)


def project(session: Session, run: RunModel) -> RunState:
    return project_with(session, run)


def project_many(session: Session, runs: Iterable[RunModel]) -> Dict[str, RunState]:
    return project_many_with(session, runs)
