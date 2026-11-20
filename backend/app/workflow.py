"""WorkflowPlan：装配流水线的只读蓝图（ProdAgent 的 Plan）。

Plan 回答静态问题：有哪些节点、节点怎么连、哪条边在什么条件下走。
它不可变、可被多个 Run 复用、可在运行前静态校验、可渲染成图。
Run 只回答动态问题：这次跑到哪了——由事件账本折叠得到（见 events.py）。

节点命名与 events._apply 写入的 current_node 严格一致。
新增阶段必须先在这里加节点和边，再由执行器消费；不允许代码里硬编码跳转。
"""

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple


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
    PlanEdge("oracle_executor", "test_confirmation", "audit_passed"),
    PlanEdge("oracle_executor", "failed", "audit_rejected"),
    PlanEdge("test_confirmation", "export", "confirmed"),
)


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


DEFAULT_PLAN = WorkflowPlan(
    name="regops_generation",
    nodes=DEFAULT_NODES,
    edges=DEFAULT_EDGES,
    entry="created",
)


def is_gate(name: str) -> bool:
    """中断点：执行到此处必须停下等人工输入。"""
    return DEFAULT_PLAN.node(name).kind == "gate"


# 语义化的边查询入口。执行器只认这些语义，不再硬编码节点名。
def self_heal_target() -> str:
    """审计失败且还有自愈预算时回到的节点。"""
    target = DEFAULT_PLAN.next("oracle_executor", "audit_failed_self_heal")
    if not target:
        raise RuntimeError("Plan 缺少自愈回边")
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


def front_half_nodes() -> FrozenSet[str]:
    """Plan 前半段：从 entry 到 oracle_asset_builder（含），由首轮执行器负责。"""
    front = {"created", "generation_planner", "schema_mapper", "rule_binder", "oracle_asset_builder"}
    return frozenset(front & {node.name for node in DEFAULT_PLAN.nodes})
