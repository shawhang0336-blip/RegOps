"""WorkflowPlan（ProdAgent 的 Plan）测试。

Plan 是只读蓝图：可复用、可静态校验、可渲染、可在运行前发现悬空边。
Run 的 current_node 必须落在 Plan 节点集里；跳转目标同样受 Plan 约束。
"""

import pytest

from app.policy.regops_plan import (
    DEFAULT_PLAN,
    approval_target,
    audit_passed_target,
    decided_target,
    is_gate,
    self_heal_target,
)
from app.kernel.plan import PlanEdge, PlanNode, RetryPolicy, WorkflowPlan
from app.kernel.event_log import append_event
from app.infra.models import RunModel
from .helpers import make_session


def test_default_plan_is_valid():
    assert DEFAULT_PLAN.validate() == []
    assert DEFAULT_PLAN.entry == "created"
    assert is_gate("human_gate")
    assert is_gate("test_confirmation")
    assert not is_gate("rule_binder")


def test_plan_semantic_edges():
    assert approval_target(pending=True) == "human_gate"
    assert approval_target(pending=False) == "data_generator_binder"
    assert decided_target(True) == "data_generator_binder"
    assert decided_target(False) == "human_gate"
    assert self_heal_target() == "rule_binder"
    assert audit_passed_target() == "test_confirmation"


def test_plan_rejects_dangling_edge():
    nodes = (
        PlanNode("start", "开始", "stage"),
        PlanNode("end", "结束", "terminal"),
    )
    # 绕过构造校验拿到实例，直接检查 validate() 能报出悬空边
    bad = object.__new__(WorkflowPlan)
    object.__setattr__(bad, "name", "bad")
    object.__setattr__(bad, "nodes", nodes)
    object.__setattr__(bad, "edges", (PlanEdge("start", "ghost", "always"),))
    object.__setattr__(bad, "entry", "start")
    errors = bad.validate()
    assert any("悬空边" in err and "ghost" in err for err in errors)


def test_plan_rejects_unreachable_node():
    bad = object.__new__(WorkflowPlan)
    object.__setattr__(bad, "name", "bad")
    object.__setattr__(bad, "nodes", (
        PlanNode("start", "开始", "stage"),
        PlanNode("lonely", "孤儿", "stage"),
    ))
    object.__setattr__(bad, "edges", ())
    object.__setattr__(bad, "entry", "start")
    assert any("不可达" in err for err in bad.validate())


def test_plan_constructor_blocks_invalid_blueprint():
    with pytest.raises(ValueError, match="非法 Plan"):
        WorkflowPlan(
            name="bad",
            nodes=(PlanNode("start", "开始", "stage"),),
            edges=(PlanEdge("start", "nowhere", "always"),),
            entry="start",
        )


def test_render_mermaid_contains_all_nodes_and_edges():
    text = DEFAULT_PLAN.render_mermaid()
    assert "flowchart TD" in text
    for node in DEFAULT_PLAN.nodes:
        assert node.name in text
    for edge in DEFAULT_PLAN.edges:
        assert f"{edge.src}" in text and f"{edge.dst}" in text


def test_go_redirect_rejects_node_outside_plan():
    """GO_REDIRECT 的目标必须是 Plan 里的合法节点。"""
    from app.policy.regops_plan import _apply
    from app.kernel.event_log import RunState

    with pytest.raises(ValueError, match="不在 Plan 中"):
        _apply(RunState(), "GO_REDIRECT", {"target": "ghost_node"})


def test_plan_audit_guards_retry_on_empty_fields_duplicates_and_dead_ends():
    """回归：plan.py 自审发现的 4 项静态校验缺口。"""
    # 1) RetryPolicy.retry_on 误传单个异常类（漏逗号）须在构造期立即拦截
    with pytest.raises(ValueError, match="单元素元组"):
        RetryPolicy(retry_on=ConnectionError)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="元组必须非空"):
        RetryPolicy(retry_on=())

    # 2) 空节点名与空条件标签 when 须立即拦截
    with pytest.raises(ValueError, match="节点名称不能为空"):
        PlanNode("   ", "空节点", "stage")
    with pytest.raises(ValueError, match="when 不能为空"):
        PlanEdge("a", "b", "  ")

    # 3) 重复边与无出边 stage 节点须在建图期拦截
    with pytest.raises(ValueError, match="重复边"):
        WorkflowPlan(
            name="dup_edge",
            nodes=(PlanNode("a", "起点", "stage"), PlanNode("b", "终点", "terminal")),
            edges=(PlanEdge("a", "b", "always"), PlanEdge("a", "b", "always")),
            entry="a",
        )
    with pytest.raises(ValueError, match="非终态阶段节点缺少出边"):
        WorkflowPlan(
            name="dead_stage",
            nodes=(PlanNode("a", "起点", "stage"), PlanNode("b", "死胡同", "stage")),
            edges=(PlanEdge("a", "b", "always"),),
            entry="a",
        )

    # 4) 入口节点不存在时不应级联误报全部正常节点“不可达”
    bad_entry = object.__new__(WorkflowPlan)
    object.__setattr__(bad_entry, "name", "bad_entry")
    object.__setattr__(bad_entry, "nodes", (PlanNode("a", "A", "stage"), PlanNode("b", "B", "terminal")))
    object.__setattr__(bad_entry, "edges", (PlanEdge("a", "b", "always"),))
    object.__setattr__(bad_entry, "entry", "missing_entry")
    errs = bad_entry.validate()
    assert any("入口节点" in e for e in errs)
    assert not any("不可达" in e for e in errs)

