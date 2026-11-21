"""ProdAgent 微内核（机制层）：机制在内，策略在外。

本包只包含与监管报送业务完全解耦的九大内核构件：
1. EventLog (`event_log.py`)：带 SHA-256 哈希链的只追加事件账本与状态折叠引擎；
2. BlobStore (`blob_store.py`)：基于 SHA-256 内容寻址的不可变大对象存储；
3. Bus (`bus.py`)：事前门禁裁决（check/adjudicate）+ 全生命周期事后广播（fire/subscribe）；
4. Channel & RunContext (`channel.py`)：带 Reducer 合并语义的跨节点命名数据通道与运行时上下文；
5. Plan (`plan.py`)：静态只读工作流蓝图（PlanNode / PlanEdge / WorkflowPlan）；
6. Interrupt (`interrupt.py`)：一等人工中断指令与幂等回答协议；
7. Budget (`budget.py`)：挂载在 Bus 上的横切成本统计与超限熔断守卫（BudgetGuard）；
8. Run (`run.py`)：统一 Run 生命周期控制面（创世/提交/活跃注册表/取消/恢复/钩子删除/回放）；
9. Scheduler (`scheduler.py`)：真 BSP 波次调度引擎（`Send` 动态扇出 + `Goto` + `Interrupt` + `execute_wave`）。
"""

from .blob_store import LocalBlobStore
from .budget import BudgetExceededError, BudgetGuard, BudgetLimits
from .bus import LIFECYCLE_TOPICS, MAX_BUS_HISTORY, Adjudicator, Bus, Subscriber, global_bus
from .channel import (
    ChannelSpec,
    Identity,
    Reducer,
    RunContext,
    append_list,
    last_value,
    merge_dict,
    resolve_event_tables,
)
from .event_log import (
    GENESIS_HASH,
    RunState,
    StateReducer,
    append_event,
    canonical_event_json,
    compute_event_hash,
    fold_with,
    project_many_with,
    project_with,
    register_default_reducer,
    verify_chain,
)
from .interrupt import (
    Interrupt,
    answer_interrupt,
    latest_interrupt,
    make_interrupt_payload,
    matching_answer,
    pending_interrupt_with,
    record_interrupt_answer,
)
from .plan import PlanEdge, PlanNode, WorkflowPlan
from .run import (
    CancellationToken,
    RunCancelledError,
    RunRegistry,
    cancel_run,
    create_run,
    delete_run,
    fail_run,
    record_run_cancelled,
    recover_interrupted_runs,
    replay_run,
    run_registry,
    submit_run,
)
from .scheduler import NodeBody, Outcome, Scheduler, Send, StepResult, execute_wave

__all__ = [
    "Adjudicator",
    "BudgetExceededError",
    "BudgetGuard",
    "BudgetLimits",
    "Bus",
    "CancellationToken",
    "ChannelSpec",
    "GENESIS_HASH",
    "Identity",
    "Interrupt",
    "LIFECYCLE_TOPICS",
    "LocalBlobStore",
    "MAX_BUS_HISTORY",
    "NodeBody",
    "Outcome",
    "PlanEdge",
    "PlanNode",
    "Reducer",
    "RunCancelledError",
    "RunContext",
    "RunRegistry",
    "RunState",
    "Scheduler",
    "Send",
    "StateReducer",
    "StepResult",
    "Subscriber",
    "WorkflowPlan",
    "answer_interrupt",
    "append_event",
    "append_list",
    "cancel_run",
    "canonical_event_json",
    "compute_event_hash",
    "create_run",
    "delete_run",
    "execute_wave",
    "fail_run",
    "fold_with",
    "global_bus",
    "last_value",
    "latest_interrupt",
    "make_interrupt_payload",
    "matching_answer",
    "merge_dict",
    "pending_interrupt_with",
    "project_many_with",
    "project_with",
    "record_interrupt_answer",
    "record_run_cancelled",
    "recover_interrupted_runs",
    "register_default_reducer",
    "replay_run",
    "resolve_event_tables",
    "run_registry",
    "submit_run",
    "verify_chain",
]
