"""WorkflowPlan（ProdAgent 的 Plan）测试。

Plan 是只读蓝图：可复用、可静态校验、可渲染、可在运行前发现悬空边。
Run 的 current_node 必须落在 Plan 节点集里；跳转目标同样受 Plan 约束。
"""

import pytest

from app.workflow import (
    DEFAULT_PLAN,
    WorkflowPlan,
    PlanEdge,
    PlanNode,
    approval_target,
    audit_passed_target,
    decided_target,
    front_half_nodes,
    is_gate,
    self_heal_target,
)
from app.events import append_event
from app.models import RunModel
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
    assert "rule_binder" in front_half_nodes()
    assert "oracle_executor" not in front_half_nodes()


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
    from app.events import _apply, RunState

    with pytest.raises(ValueError, match="不在 Plan 中"):
        _apply(RunState(), "GO_REDIRECT", {"target": "ghost_node"})
