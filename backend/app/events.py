"""EventLog 兼容门面：
- 事件账本微内核（GENESIS_HASH / RunState / append_event / verify_chain / 通用 Interrupt 与折叠引擎）位于 `app.kernel.event_log`；
- RegOps 业务事件状态折叠策略（_apply / fold / project / project_many / interrupt_payload / pending_interrupt）位于 `app.policy.regops_plan`。
"""

from .kernel.event_log import (
    GENESIS_HASH,
    RunState,
    StateReducer,
    append_event,
    canonical_event_json,
    compute_event_hash,
    fold_with,
    latest_interrupt,
    make_interrupt_payload,
    matching_answer,
    pending_interrupt_with,
    project_many_with,
    project_with,
    register_default_reducer,
    verify_chain,
)
from .policy.regops_plan import (
    INTERRUPT_ANSWER_KINDS,
    INTERRUPT_RESUME_NODES,
    _apply,
    fold,
    interrupt_payload,
    pending_interrupt,
    project,
    project_many,
)

__all__ = [
    "GENESIS_HASH",
    "INTERRUPT_ANSWER_KINDS",
    "INTERRUPT_RESUME_NODES",
    "RunState",
    "StateReducer",
    "_apply",
    "append_event",
    "canonical_event_json",
    "compute_event_hash",
    "fold",
    "fold_with",
    "interrupt_payload",
    "latest_interrupt",
    "make_interrupt_payload",
    "matching_answer",
    "pending_interrupt",
    "pending_interrupt_with",
    "project",
    "project_many",
    "project_many_with",
    "project_with",
    "register_default_reducer",
    "verify_chain",
]
