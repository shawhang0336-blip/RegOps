"""Scheduler：唯一的执行引擎（ProdAgent 的 Scheduler）。

职责边界：
  - 不认识任何具体业务阶段——只读 Plan、算就绪、调 body、收 Outcome；
  - 节点之间不直接调用：body 返回 Outcome（delta + 事件 + goto），
    由 Scheduler 在屏障处统一落 EventLog → 折叠 state → 应用控制指令；
  - "先落账、再投影"：任何时刻崩溃，重放 EventLog 都能精确回到当时。

这条流水线是线性 + 人工门，没有真正的多节点同波并发（并发在 _fanout 内部），
所以 Scheduler 每波只有一个就绪节点。但调度权收在这里，
body 不再自己决定下一个调谁——goto 由 Outcome 声明、由 Scheduler 应用。

中断（Interrupt）：遇到 gate 节点或 status 进入 WAITING_* 时停下返回，
等外部事件（审批/确认）写进账本后，入口再次 drive 即从新状态继续。
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .context import RunContext
from .events import append_event
from .workflow import WorkflowPlan, is_gate

# Node body 的统一契约：body(input, ctx) -> Outcome
NodeBody = Callable[[Any, RunContext], "Outcome"]


@dataclass
class Outcome:
    """节点执行结果。只声明事实与控制指令，不直接改 Run 或调下游。"""

    output: Any = None
    shared: Dict[str, Any] = field(default_factory=dict)  # 写入 Channel 的产物
    events: List[tuple] = field(default_factory=list)  # [(kind, node, payload), ...]
    goto: Optional[str] = None  # 运行时跳转目标（必须合法节点）

    def add_event(self, kind: str, node: str, payload: Dict[str, Any]) -> "Outcome":
        self.events.append((kind, node, payload))
        return self


@dataclass
class StepResult:
    node: str
    outcome: Outcome


class Scheduler:
    """按 Plan 驱动一次执行，直到人工门或终态。"""

    def __init__(self, plan: WorkflowPlan, bodies: Dict[str, NodeBody]) -> None:
        self.plan = plan
        self.bodies = bodies
        unknown = set(bodies) - {node.name for node in plan.nodes}
        if unknown:
            raise ValueError(f"Node body 不在 Plan 中：{sorted(unknown)}")

    def _next(self, node: str, ctx: RunContext) -> Optional[str]:
        """从当前游标算下一节点。调度推进不依赖事件——
        某些节点（如可选规划）不写事件，若靠 folded current_node 推进会原地打转。
        事件只负责把状态落账供审计与恢复，游标由 Scheduler 自己持有。"""
        if node not in {item.name for item in self.plan.nodes}:
            return None
        plan_node = self.plan.node(node)
        if plan_node.kind == "terminal" or is_gate(node):
            return None
        if node == "oracle_asset_builder":
            # 有 pending → 停在人工门；无 pending → 沿 no_pending_rules 进后半段入口
            when = "pending_rules" if ctx.get("pending_count", 0) else "no_pending_rules"
            return self.plan.next(node, when)
        nxt = self.plan.next(node, "always")
        return nxt

    def drive(self, ctx: RunContext, max_steps: int = 64) -> List[StepResult]:
        """执行到人工门或终态，返回本轮执行的节点轨迹。"""
        executed: List[StepResult] = []
        cursor = ctx.state.current_node
        # 从 human_gate 恢复且已放行 → 沿 all_decided 进下一节点
        if cursor == "human_gate" and ctx.state.status == "READY_TO_AUDIT":
            cursor = self.plan.next("human_gate", "all_decided") or cursor
        for _ in range(max_steps):
            nxt = self._next(cursor, ctx)
            if not nxt:
                break
            body = self.bodies.get(nxt)
            if body is None:
                # 该节点没有 body（如由外部事件推进的节点）→ 停下
                break
            outcome = body(None, ctx)
            # 屏障：先落账，再折叠，再应用 goto
            for kind, node, payload in outcome.events:
                append_event(ctx.session, ctx.run, kind, node, payload)
            ctx.session.flush()
            for key, value in outcome.shared.items():
                ctx.put(key, value)
            ctx.refresh_state()
            executed.append(StepResult(nxt, outcome))
            if outcome.goto:
                if outcome.goto not in {item.name for item in self.plan.nodes}:
                    raise ValueError(f"goto 目标不在 Plan 中：{outcome.goto}")
                append_event(
                    ctx.session, ctx.run, "GO_REDIRECT", nxt,
                    {"target": outcome.goto, "reason": "scheduler goto"},
                )
                ctx.session.flush()
                ctx.refresh_state()
                cursor = outcome.goto
            else:
                cursor = nxt
            # 只在终态停；WAITING_* 由 _next 在 gate 处返回 None 实现中断
            if ctx.state.status in {"COMPLETED", "FAILED"}:
                ctx.session.commit()
                return executed
        ctx.session.commit()
        return executed
