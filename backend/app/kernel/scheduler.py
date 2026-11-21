"""Scheduler：ProdAgent 真 BSP 波次执行引擎微内核（Wave + Send + Goto + Interrupt）。

职责边界：
- 不认识任何具体业务阶段——只读 Plan、算就绪波次（Ready Set）、并发跑 Wave、在屏障统一归并落账；
- 完整支持 ProdAgent 三大控制原语：
  1. `Send(node, input)`：运行时动态扇出（同波次并发执行，屏障按确定性顺序通过 Channel Reducer 合并产物）；
  2. `goto`：运行时跳转/自愈回边（落 `GO_REDIRECT` 后重入目标节点）；
  3. `Interrupt`：一等人工中断指令（屏障落账中断事件、广播 `run.interrupted` 并立刻挂起）；
- 同步提供可复用的内核级波次执行原语 `execute_wave`，统一托管 `wave.fanout` / `wave.barrier`、
  并发池、取消检查（`CancellationToken`）与预算熔断检查（`BudgetGuard`）。
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar

from .bus import global_bus
from .channel import RunContext
from .event_log import append_event
from .interrupt import Interrupt
from .plan import WorkflowPlan

T = TypeVar("T")
R = TypeVar("R")

# Node body 的统一契约：body(input, ctx) -> Outcome
NodeBody = Callable[[Any, RunContext], "Outcome"]


@dataclass(frozen=True)
class Send:
    """动态扇出指令：向目标节点派发一条带独立 input 的并发任务，在下一 BSP 波次并行执行。"""

    node: str
    input: Any = None
    label: Optional[str] = None


@dataclass
class Outcome:
    """节点执行结果。只声明事实与控制指令，不直接改 Run 或调下游。"""

    output: Any = None
    shared: Dict[str, Any] = field(default_factory=dict)  # 写入 Channel 的产物
    events: List[tuple] = field(default_factory=list)  # [(kind, node, payload), ...]
    goto: Optional[str] = None  # 运行时跳转目标（必须为 Plan 中的合法节点）
    sends: List[Send] = field(default_factory=list)  # 动态扇出任务列表（下一 BSP 波次并发执行）
    control: Dict[str, Any] = field(default_factory=dict)  # goto 附带的控制信息
    when: str = "always"  # Plan 条件出边标签
    interrupt: Optional[Interrupt] = None  # 一等人工中断指令（存在时屏障落账后立即挂起）

    def add_event(self, kind: str, node: str, payload: Dict[str, Any]) -> "Outcome":
        self.events.append((kind, node, payload))
        return self


@dataclass
class StepResult:
    node: str
    outcome: Outcome
    input: Any = None
    label: Optional[str] = None


def execute_wave(
    items: Sequence[T],
    worker: Callable[[T], R],
    *,
    bus: Any = None,
    run_id: Optional[str] = None,
    stage: Optional[str] = None,
    labels: Optional[Sequence[str]] = None,
    max_workers: int = 4,
    cancellation: Any = None,
    budget_guard: Any = None,
) -> List[R]:
    """内核级 BSP 波次执行原语：波前门禁 → `wave.fanout` → 并发执行 → `wave.barrier`（保序返回）。"""
    if not items:
        return []
    if cancellation is not None and hasattr(cancellation, "raise_if_cancelled"):
        cancellation.raise_if_cancelled()
    active_bus = bus if bus is not None else global_bus()
    if budget_guard is not None and hasattr(budget_guard, "check"):
        budget_guard.check(active_bus)
    elif hasattr(active_bus, "check_budget"):
        active_bus.check_budget()

    item_labels = list(labels) if labels is not None else [str(i) for i in range(len(items))]
    fanout_meta: Dict[str, Any] = {"wave_size": len(items), "labels": item_labels}
    if run_id:
        fanout_meta["run_id"] = run_id
    if stage:
        fanout_meta["stage"] = stage
        fanout_meta["table_codes"] = item_labels
    if hasattr(active_bus, "fire"):
        active_bus.fire("wave.fanout", fanout_meta)

    def _guarded_worker(item: T) -> R:
        if cancellation is not None and hasattr(cancellation, "raise_if_cancelled"):
            cancellation.raise_if_cancelled()
        return worker(item)

    worker_count = max(1, min(int(max_workers or 1), len(items)))
    if worker_count == 1 or len(items) == 1:
        results = [_guarded_worker(item) for item in items]
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(_guarded_worker, item) for item in items]
            results = [future.result() for future in futures]

    barrier_meta: Dict[str, Any] = {"merged_count": len(results), "labels": item_labels}
    if run_id:
        barrier_meta["run_id"] = run_id
    if stage:
        barrier_meta["stage"] = stage
        barrier_meta["table_codes"] = item_labels
    if hasattr(active_bus, "fire"):
        active_bus.fire("wave.barrier", barrier_meta)
    return results


class Scheduler:
    """按 Plan 与 BSP 超步模型驱动一次执行，直到人工中断门或终态。"""

    def __init__(self, plan: WorkflowPlan, bodies: Dict[str, NodeBody]) -> None:
        self.plan = plan
        self.bodies = bodies
        self._node_names = {node.name for node in plan.nodes}
        unknown = set(bodies) - self._node_names
        if unknown:
            raise ValueError(f"Node body 不在 Plan 中：{sorted(unknown)}")

    def _next_ready(self, node: str, ctx: RunContext, when: Optional[str] = None) -> List[str]:
        """从当前游标 + 条件标签计算下一波次的所有就绪节点（支持单后继与静态多边扇出）。"""
        if node not in self._node_names:
            return []
        plan_node = self.plan.node(node)
        if plan_node.kind == "terminal":
            return []
        if plan_node.kind == "gate" and ctx.state.status.startswith("WAITING_"):
            return []

        edges = self.plan.successors(node)
        for tag in (when, ctx.state.when, "always"):
            if not tag:
                continue
            if tag == "always":
                matched = [e.dst for e in edges if e.when == "always"]
            else:
                matched = [e.dst for e in edges if e.when == tag]
            if matched:
                # 保持声明顺序去重
                return list(dict.fromkeys(matched))
        return []

    def _next(self, node: str, ctx: RunContext, when: Optional[str] = None) -> Optional[str]:
        """兼容单后继查询。"""
        ready = self._next_ready(node, ctx, when=when)
        return ready[0] if ready else None

    def _run_task(self, task: Send, ctx: RunContext) -> Tuple[Send, Outcome]:
        body = self.bodies[task.node]
        if hasattr(ctx.bus, "fire"):
            enter_meta: Dict[str, Any] = {
                "run_id": ctx.run.id,
                "node": task.node,
                "attempt": ctx.state.attempt,
            }
            if task.label:
                enter_meta["label"] = task.label
            ctx.bus.fire("node.enter", enter_meta)
        outcome = body(task.input, ctx)
        return task, outcome

    def drive(self, ctx: RunContext, max_steps: int = 64, max_workers: Optional[int] = None) -> List[StepResult]:
        """按 BSP 超步波次执行到人工中断门或终态，返回全部执行轨迹。"""
        executed: List[StepResult] = []
        cursor = ctx.state.current_node
        pending_wave: Optional[List[Send]] = (
            [Send(ctx.state.pending_target, None)] if ctx.state.pending_target else None
        )
        last_when: Optional[str] = None
        wave_width = max(1, int(max_workers or ctx.concurrency or 4))

        for _ in range(max_steps):
            ctx.check_cancelled()
            ctx.check_budget()

            if pending_wave is None:
                ready_nodes = self._next_ready(cursor, ctx, when=last_when)
                pending_wave = [Send(name, None) for name in ready_nodes]
            wave = [task for task in pending_wave if task.node in self.bodies]
            pending_wave = None
            last_when = None
            if not wave:
                break

            # 波次并发执行（单任务直跑零线程开销；多任务通过 execute_wave 真 BSP 并发）
            if len(wave) == 1:
                ctx.current_node = wave[0].node
                wave_outcomes = [self._run_task(wave[0], ctx)]
            else:
                ctx.current_node = wave[0].node
                wave_outcomes = execute_wave(
                    wave,
                    lambda task: self._run_task(task, ctx),
                    bus=ctx.bus,
                    run_id=ctx.run.id,
                    stage=wave[0].node,
                    labels=[task.label or task.node for task in wave],
                    max_workers=wave_width,
                    cancellation=ctx.cancellation,
                    budget_guard=ctx.budget_guard,
                )

            # ── BSP 屏障：主线程按确定性顺序统一落账、归并 Channel、折叠状态 ──
            bus_rejections = ctx.bus.drain_rejections() if hasattr(ctx.bus, "drain_rejections") else []
            barrier_node = wave[-1].node
            for rej in bus_rejections:
                append_event(ctx.session, ctx.run, "BUS_REJECTED", barrier_node, rej, bus=ctx.bus)

            interrupt_hit: Optional[Interrupt] = None
            goto_outcome: Optional[Tuple[str, Outcome]] = None
            next_sends: List[Send] = []

            for task, outcome in wave_outcomes:
                ctx.current_node = task.node
                for kind, ev_node, payload in outcome.events:
                    append_event(ctx.session, ctx.run, kind, ev_node, payload, bus=ctx.bus)
                if outcome.interrupt is not None and interrupt_hit is None:
                    interrupt_hit = outcome.interrupt
                    append_event(
                        ctx.session,
                        ctx.run,
                        outcome.interrupt.kind,
                        outcome.interrupt.node,
                        outcome.interrupt.to_payload(),
                        bus=ctx.bus,
                    )
                for key, value in outcome.shared.items():
                    ctx.put(key, value)
                if outcome.sends:
                    for send_cmd in outcome.sends:
                        if send_cmd.node not in self._node_names:
                            raise ValueError(f"Send 目标不在 Plan 中：{send_cmd.node}")
                        next_sends.append(send_cmd)
                if outcome.goto and goto_outcome is None:
                    goto_outcome = (task.node, outcome)

            ctx.session.flush()
            ctx.refresh_state()

            for task, outcome in wave_outcomes:
                if hasattr(ctx.bus, "fire"):
                    exit_meta: Dict[str, Any] = {
                        "run_id": ctx.run.id,
                        "node": task.node,
                        "when": outcome.when,
                        "goto": outcome.goto,
                        "sends": len(outcome.sends),
                        "interrupted": outcome.interrupt is not None,
                        "status": ctx.state.status,
                    }
                    if task.label:
                        exit_meta["label"] = task.label
                    ctx.bus.fire("node.exit", exit_meta)
                executed.append(StepResult(task.node, outcome, input=task.input, label=task.label))

            # 1) 一等 Interrupt：立即挂起并提交
            if interrupt_hit is not None:
                if hasattr(ctx.bus, "fire"):
                    ctx.bus.fire(
                        "run.interrupted",
                        {
                            "run_id": ctx.run.id,
                            "node": interrupt_hit.node,
                            "interrupt_kind": interrupt_hit.kind,
                            "status": ctx.state.status,
                        },
                    )
                ctx.session.commit()
                return executed

            # 2) 动态扇出 Send：进入下一 BSP 波次
            if next_sends:
                pending_wave = next_sends
                cursor = wave[-1].node
            # 3) 运行时跳转 Goto：落 GO_REDIRECT 并重入目标节点
            elif goto_outcome is not None:
                from_node, out = goto_outcome
                if out.goto not in self._node_names:
                    raise ValueError(f"goto 目标不在 Plan 中：{out.goto}")
                goto_payload = {"target": out.goto, "reason": "scheduler goto", **out.control}
                append_event(ctx.session, ctx.run, "GO_REDIRECT", from_node, goto_payload, bus=ctx.bus)
                ctx.session.flush()
                ctx.refresh_state()
                if hasattr(ctx.bus, "fire"):
                    ctx.bus.fire("node.redirect", {"run_id": ctx.run.id, "from": from_node, **goto_payload})
                pending_wave = [Send(out.goto, None)]
                cursor = out.goto
            else:
                cursor = wave[-1].node
                last_when = wave_outcomes[-1][1].when

            # 终态停止
            if ctx.state.status in {"COMPLETED", "FAILED"}:
                if hasattr(ctx.bus, "fire"):
                    topic = "run.completed" if ctx.state.status == "COMPLETED" else "run.failed"
                    ctx.bus.fire(topic, {"run_id": ctx.run.id, "node": ctx.state.current_node, "status": ctx.state.status})
                ctx.session.commit()
                return executed

            # 波后取消 / 预算熔断：先提交已完成波次的检查点再抛出异常
            if ctx.is_cancelled:
                ctx.session.commit()
                ctx.check_cancelled()
            ctx.check_budget()

        if hasattr(ctx.bus, "fire") and ctx.state.status.startswith("WAITING_"):
            ctx.bus.fire("run.interrupted", {"run_id": ctx.run.id, "node": ctx.state.current_node, "status": ctx.state.status})
        ctx.session.commit()
        return executed
