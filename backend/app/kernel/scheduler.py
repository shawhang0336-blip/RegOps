"""Scheduler：ProdAgent 唯一执行引擎微内核。

职责边界：
- 不认识任何具体业务阶段——只读 Plan、算就绪、调 body、收 Outcome；
- 节点之间不直接调用：body 返回 Outcome（shared 增量 + events 事件 + when 出边标签 + goto 跳转指令），
  由 Scheduler 在屏障处统一排空 Bus 拦截记录（BUS_REJECTED）→ 落 EventLog → 经 Channel Reducer 合并产物 → 折叠 state → 应用 goto；
- "先落账、再投影"：任何时刻崩溃，重放 EventLog 都能精确回到当时；
- 遇到 gate 节点且 status 处于 WAITING_* 时自动挂起中断（Interrupt），遇到 terminal 节点或 COMPLETED/FAILED 时终止。
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .channel import RunContext
from .event_log import append_event
from .plan import WorkflowPlan

# Node body 的统一契约：body(input, ctx) -> Outcome
NodeBody = Callable[[Any, RunContext], "Outcome"]


@dataclass
class Outcome:
    """节点执行结果。只声明事实与控制指令，不直接改 Run 或调下游。"""

    output: Any = None
    shared: Dict[str, Any] = field(default_factory=dict)  # 写入 Channel 的产物
    events: List[tuple] = field(default_factory=list)  # [(kind, node, payload), ...]
    goto: Optional[str] = None  # 运行时跳转目标（必须为 Plan 中的合法节点）
    control: Dict[str, Any] = field(default_factory=dict)  # goto 附带的控制信息
    when: str = "always"  # Plan 条件出边标签

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

    def _next(self, node: str, ctx: RunContext, when: Optional[str] = None) -> Optional[str]:
        """从当前游标 + 条件标签查 Plan 下一节点。

        Scheduler 不认识任何具体业务节点名：
          - terminal 节点一律停止；
          - gate 节点在 status 处于 WAITING_* 时挂起中断，已放行时按条件标签前进；
          - 普通 stage 节点按 outcome.when / state.when / 'always' 查 Plan 出边。
        """
        if node not in {item.name for item in self.plan.nodes}:
            return None
        plan_node = self.plan.node(node)
        if plan_node.kind == "terminal":
            return None
        if plan_node.kind == "gate" and ctx.state.status.startswith("WAITING_"):
            return None
        if when and when != "always":
            nxt = self.plan.next(node, when)
            if nxt:
                return nxt
        if ctx.state.when and ctx.state.when != "always":
            nxt = self.plan.next(node, ctx.state.when)
            if nxt:
                return nxt
        return self.plan.next(node, "always")

    def drive(self, ctx: RunContext, max_steps: int = 64) -> List[StepResult]:
        """执行到人工门或终态，返回本轮执行的节点轨迹。"""
        executed: List[StepResult] = []
        cursor = ctx.state.current_node
        target: Optional[str] = ctx.state.pending_target  # 若停在 GO_REDIRECT，直接重入目标节点本身
        last_when: Optional[str] = None
        for _ in range(max_steps):
            if target is None:
                target = self._next(cursor, ctx, when=last_when)
            node = target
            target = None
            last_when = None
            if not node:
                break
            body = self.bodies.get(node)
            if body is None:
                # 该节点没有 body（如由外部事件推进的 gate / terminal 节点）→ 停下
                break
            ctx.current_node = node
            if hasattr(ctx.bus, "fire"):
                ctx.bus.fire("node.enter", {"run_id": ctx.run.id, "node": node, "attempt": ctx.state.attempt})
            outcome = body(None, ctx)
            # 屏障：先落 Bus 拦截事件与节点事件，再写 Channel，再折叠 state，最后应用 goto
            bus_rejections = ctx.bus.drain_rejections() if hasattr(ctx.bus, "drain_rejections") else []
            for rej in bus_rejections:
                append_event(ctx.session, ctx.run, "BUS_REJECTED", node, rej, bus=ctx.bus)
            for kind, ev_node, payload in outcome.events:
                append_event(ctx.session, ctx.run, kind, ev_node, payload, bus=ctx.bus)
            ctx.session.flush()
            for key, value in outcome.shared.items():
                ctx.put(key, value)
            ctx.refresh_state()
            if hasattr(ctx.bus, "fire"):
                ctx.bus.fire(
                    "node.exit",
                    {
                        "run_id": ctx.run.id,
                        "node": node,
                        "when": outcome.when,
                        "goto": outcome.goto,
                        "status": ctx.state.status,
                    },
                )
            executed.append(StepResult(node, outcome))
            if outcome.goto:
                if outcome.goto not in {item.name for item in self.plan.nodes}:
                    raise ValueError(f"goto 目标不在 Plan 中：{outcome.goto}")
                goto_payload = {"target": outcome.goto, "reason": "scheduler goto", **outcome.control}
                append_event(ctx.session, ctx.run, "GO_REDIRECT", node, goto_payload, bus=ctx.bus)
                ctx.session.flush()
                ctx.refresh_state()
                if hasattr(ctx.bus, "fire"):
                    ctx.bus.fire("node.redirect", {"run_id": ctx.run.id, "from": node, **goto_payload})
                # goto 是"重新执行目标节点"，不是"执行目标的后继"
                target = outcome.goto
                cursor = outcome.goto
            else:
                cursor = node
                last_when = outcome.when
            # 只在终态停；WAITING_* 由 _next 在 gate 处返回 None 实现中断
            if ctx.state.status in {"COMPLETED", "FAILED"}:
                if hasattr(ctx.bus, "fire"):
                    topic = "run.completed" if ctx.state.status == "COMPLETED" else "run.failed"
                    ctx.bus.fire(topic, {"run_id": ctx.run.id, "node": ctx.state.current_node, "status": ctx.state.status})
                ctx.session.commit()
                return executed
        if hasattr(ctx.bus, "fire") and ctx.state.status.startswith("WAITING_"):
            ctx.bus.fire("run.interrupted", {"run_id": ctx.run.id, "node": ctx.state.current_node, "status": ctx.state.status})
        ctx.session.commit()
        return executed
