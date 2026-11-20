"""WorkflowPlan 兼容门面：
- 静态蓝图微内核（PlanNode / PlanEdge / WorkflowPlan）位于 `app.kernel.plan`；
- RegOps 12 节点装配拓扑（DEFAULT_PLAN）与语义边查询位于 `app.policy.regops_plan`。
"""

from .kernel.plan import PlanEdge, PlanNode, WorkflowPlan
from .policy.regops_plan import (
    DEFAULT_EDGES,
    DEFAULT_NODES,
    DEFAULT_PLAN,
    approval_target,
    audit_passed_target,
    decided_target,
    is_gate,
    self_heal_target,
)

__all__ = [
    "DEFAULT_EDGES",
    "DEFAULT_NODES",
    "DEFAULT_PLAN",
    "PlanEdge",
    "PlanNode",
    "WorkflowPlan",
    "approval_target",
    "audit_passed_target",
    "decided_target",
    "is_gate",
    "self_heal_target",
]
