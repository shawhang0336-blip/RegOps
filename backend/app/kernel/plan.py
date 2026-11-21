"""WorkflowPlan：ProdAgent 静态工作流蓝图微内核（Plan）。

职责边界：
- Plan 回答静态问题：有哪些节点、节点怎么连、哪条边在什么条件下走、节点的超时与退避重试策略是什么；
- 不可变、可被多个 Run 复用、可在运行前做悬空边/孤岛节点/终态出边静态校验、可渲染成 Mermaid 图；
- 机制层不定义任何业务节点名（业务拓扑位于 app/policy/regops_plan.py）。
"""

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple, Type, Union


@dataclass(frozen=True)
class RetryPolicy:
    """节点级声明式重试与指数退避策略（机制在内执行，重试条件与退避参数由 Plan 声明）。

    - `max_attempts`：含首次执行在内的最大尝试次数；
    - `base_delay` / `factor` / `max_delay`：第 n 次失败（从 0 起）等待 `base_delay * factor**n` 秒；
    - `retry_on`：允许重试的异常类型元组或谓词函数；取消（`RunCancelledError`）、预算熔断（`BudgetExceededError`）
      与通道并发写冲突（`AmbiguousWriteError`）永不重试。
    """

    max_attempts: int = 3
    base_delay: float = 0.05
    factor: float = 2.0
    max_delay: Optional[float] = None
    retry_on: Union[Tuple[Type[BaseException], ...], Callable[[BaseException], bool]] = (Exception,)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("RetryPolicy.max_attempts 必须 >= 1")
        if self.base_delay < 0:
            raise ValueError("RetryPolicy.base_delay 不能为负数")
        if self.factor < 1:
            raise ValueError("RetryPolicy.factor 必须 >= 1")
        if self.max_delay is not None and self.max_delay < 0:
            raise ValueError("RetryPolicy.max_delay 不能为负数")

    def delay_for(self, failed_attempt: int) -> float:
        delay = self.base_delay * (self.factor ** max(0, failed_attempt))
        if self.max_delay is not None:
            return min(delay, self.max_delay)
        return delay

    def matches(self, exc: BaseException) -> bool:
        if isinstance(self.retry_on, tuple):
            return isinstance(exc, self.retry_on)
        return bool(self.retry_on(exc))


@dataclass(frozen=True)
class PlanNode:
    name: str
    label: str
    kind: str  # stage | gate | terminal
    timeout: Optional[float] = None
    retry: Optional[RetryPolicy] = None

    def __post_init__(self) -> None:
        if self.kind not in {"stage", "gate", "terminal"}:
            raise ValueError(f"未知节点类型：{self.kind}")
        if self.timeout is not None and self.timeout <= 0:
            raise ValueError(f"节点 {self.name} 的 timeout 必须大于 0")


@dataclass(frozen=True)
class PlanEdge:
    src: str
    dst: str
    when: str  # 条件标签，由执行器判定；"always" 为无条件边

    def __post_init__(self) -> None:
        if not self.src or not self.dst:
            raise ValueError("边不能有空端点")


@dataclass(frozen=True)
class WorkflowPlan:
    name: str
    nodes: Tuple[PlanNode, ...]
    edges: Tuple[PlanEdge, ...]
    entry: str

    def __post_init__(self) -> None:
        errors = self.validate()
        if errors:
            raise ValueError("非法 Plan：" + "；".join(errors))

    def validate(self) -> List[str]:
        """静态校验：悬空边、重复节点、入口缺失、终态出边、不可达节点。"""
        errors: List[str] = []
        names = [node.name for node in self.nodes]
        if len(names) != len(set(names)):
            errors.append("存在重复节点")
        name_set = set(names)
        if self.entry not in name_set:
            errors.append(f"入口节点 {self.entry} 不存在")
        for edge in self.edges:
            if edge.src not in name_set:
                errors.append(f"悬空边：{edge.src} -[{edge.when}]-> {edge.dst} 的起点不存在")
            if edge.dst not in name_set:
                errors.append(f"悬空边：{edge.src} -[{edge.when}]-> {edge.dst} 的终点不存在")
        terminals = {node.name for node in self.nodes if node.kind == "terminal"}
        for edge in self.edges:
            if edge.src in terminals:
                errors.append(f"终态节点 {edge.src} 不应有出边")

        # 从入口做可达性分析
        outgoing: Dict[str, List[str]] = {}
        for edge in self.edges:
            outgoing.setdefault(edge.src, []).append(edge.dst)
        seen = {self.entry}
        queue = [self.entry]
        while queue:
            current = queue.pop(0)
            for dst in outgoing.get(current, []):
                if dst not in seen:
                    seen.add(dst)
                    queue.append(dst)
        unreachable = name_set - seen
        if unreachable:
            errors.append(f"不可达节点：{sorted(unreachable)}")
        return errors

    def node(self, name: str) -> PlanNode:
        for item in self.nodes:
            if item.name == name:
                return item
        raise KeyError(f"Plan 中没有节点 {name}")

    def next(self, name: str, when: str = "always") -> Optional[str]:
        """按节点名 + 条件标签查后继；找不到返回 None（中断点或终态）。"""
        for edge in self.edges:
            if edge.src == name and edge.when == when:
                return edge.dst
        return None

    def successors(self, name: str) -> Tuple[PlanEdge, ...]:
        return tuple(edge for edge in self.edges if edge.src == name)

    def render_mermaid(self) -> str:
        """渲染成 Mermaid flowchart，便于可视化与人工核对拓扑。"""
        lines = ["flowchart TD"]
        for node in self.nodes:
            if node.kind == "gate":
                lines.append(f'  {node.name}{{{{"{node.name} {node.label}"}}}}')
            elif node.kind == "terminal":
                lines.append(f'  {node.name}(( "{node.name} {node.label}" ))')
            else:
                lines.append(f'  {node.name}["{node.name} {node.label}"]')
        for edge in self.edges:
            label = "" if edge.when == "always" else edge.when
            lines.append(f"  {edge.src} -->|{label}| {edge.dst}")
        return "\n".join(lines)
