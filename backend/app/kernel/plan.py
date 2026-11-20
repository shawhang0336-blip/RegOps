"""WorkflowPlan：ProdAgent 静态工作流蓝图微内核（Plan）。

职责边界：
- Plan 回答静态问题：有哪些节点、节点怎么连、哪条边在什么条件下走；
- 不可变、可被多个 Run 复用、可在运行前做悬空边/孤岛节点/终态出边静态校验、可渲染成 Mermaid 图；
- 机制层不定义任何业务节点名（业务拓扑位于 app/policy/regops_plan.py）。
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class PlanNode:
    name: str
    label: str
    kind: str  # stage | gate | terminal

    def __post_init__(self) -> None:
        if self.kind not in {"stage", "gate", "terminal"}:
            raise ValueError(f"未知节点类型：{self.kind}")


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
