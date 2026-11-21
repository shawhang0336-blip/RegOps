"""Scheduler 与 RunContext 测试。

核心性质：
1. 节点 body 契约是 body(input, ctx) -> Outcome；
2. 节点之间不直接调用，推进由 Scheduler 按 Plan 游标决定；
3. 屏障处先落 EventLog 再折叠 state；
4. 中间产物写 Channel（ctx.shared），靠它传下游；
5. 身份信息随事件落账，可追溯。
"""

from app.kernel.run import Identity, RunContext
from app.policy.regops_channels import build_context
from app.kernel.event_log import append_event
from app.policy.regops_plan import project
from app.infra.models import EventModel, RunModel
from app.kernel.scheduler import Outcome, Scheduler
from app.policy.regops_plan import DEFAULT_PLAN
from app.kernel.plan import PlanEdge, PlanNode, WorkflowPlan
from .helpers import make_session
import uuid


def _run(session):
    run = RunModel(id=str(uuid.uuid4()), spec_id=str(uuid.uuid4()))
    session.add(run)
    session.flush()
    append_event(session, run, "RUN_CREATED", "created", {})
    session.commit()
    return run


def test_run_context_carries_identity_and_channels():
    session = make_session()
    run = _run(session)
    ctx = build_context(
        session, run, object(),
        operator="zhang", approver="li", editor="wang",
    )
    assert ctx.identity.operator == "zhang"
    assert ctx.identity.approver == "li"
    payload = ctx.identity.as_payload()
    assert payload["operator"] == "zhang"
    assert payload["approver"] == "li"
    ctx.put("schemas", {"T1": [1, 2]})
    assert ctx.get("schemas") == {"T1": [1, 2]}


def test_scheduler_drives_plan_without_nodes_calling_each_other():
    """两个节点各自只产出 Outcome；Scheduler 按游标推进，节点里没有任何下游调用。"""
    session = make_session()
    run = _run(session)
    ctx = build_context(session, run, object())

    calls = []

    def body_a(_input, c):
        calls.append("a")
        return Outcome(shared={"step_a": "done"}, events=[("SCHEMA_ASSIGNED", "schema_mapper", {"tables": {}})])

    def body_b(_input, c):
        calls.append("b")
        assert c.get("step_a") == "done"  # 通过 Channel 读到上一节点产物
        return Outcome(shared={"step_b": "done"})

    plan = WorkflowPlan(
        name="test",
        nodes=(
            PlanNode("created", "起点", "stage"),
            PlanNode("generation_planner", "A", "stage"),
            PlanNode("schema_mapper", "B", "stage"),
            PlanNode("oracle_asset_builder", "终点", "terminal"),
        ),
        edges=(
            PlanEdge("created", "generation_planner", "always"),
            PlanEdge("generation_planner", "schema_mapper", "always"),
            PlanEdge("schema_mapper", "oracle_asset_builder", "always"),
        ),
        entry="created",
    )
    scheduler = Scheduler(plan, {
        "generation_planner": body_a,
        "schema_mapper": body_b,
    })
    steps = scheduler.drive(ctx)

    assert calls == ["a", "b"]
    assert [s.node for s in steps] == ["generation_planner", "schema_mapper"]
    assert ctx.get("step_b") == "done"
    # 屏障后 state 已折叠：SCHEMA_ASSIGNED 落账 → current_node=schema_mapper
    assert ctx.state.current_node == "schema_mapper"


def test_scheduler_logs_before_folding():
    """先落账、再折叠：执行后事件表里有记录，且状态与事件一致。"""
    session = make_session()
    run = _run(session)
    ctx = build_context(session, run, object())

    def body(_input, c):
        return Outcome(events=[("GENERATION_PLANNED", "plan_barrier", {"tables": {}})])

    plan = WorkflowPlan(
        name="test",
        nodes=(
            PlanNode("created", "起点", "stage"),
            PlanNode("generation_planner", "规划", "stage"),
            PlanNode("schema_mapper", "终点", "terminal"),
        ),
        edges=(
            PlanEdge("created", "generation_planner", "always"),
            PlanEdge("generation_planner", "schema_mapper", "always"),
        ),
        entry="created",
    )
    Scheduler(plan, {"generation_planner": body}).drive(ctx)

    kinds = [e.kind for e in session.query(EventModel).filter(EventModel.run_id == run.id).order_by(EventModel.seq).all()]
    assert "GENERATION_PLANNED" in kinds
    assert project(session, run).current_node == "generation_planner"


def test_identity_lands_in_event_payload():
    session = make_session()
    run = _run(session)
    ctx = build_context(session, run, object(), operator="audit_zhangsan")

    def body(_input, c):
        return Outcome(events=[("APPROVAL_REQUIRED", "human_gate", {"pending_count": 1, **c.identity.as_payload()})])

    plan = WorkflowPlan(
        name="test",
        nodes=(
            PlanNode("created", "起点", "stage"),
            PlanNode("generation_planner", "门", "gate"),
        ),
        edges=(PlanEdge("created", "generation_planner", "always"),),
        entry="created",
    )
    Scheduler(plan, {"generation_planner": body}).drive(ctx)

    event = session.query(EventModel).filter(EventModel.kind == "APPROVAL_REQUIRED").first()
    assert event.payload_json["operator"] == "audit_zhangsan"


def test_scheduler_rejects_body_outside_plan():
    with __import__("pytest").raises(ValueError, match="不在 Plan 中"):
        Scheduler(DEFAULT_PLAN, {"ghost_node": lambda i, c: Outcome()})


def test_goto_is_applied_by_scheduler_not_by_node():
    """自愈回边由 Outcome.goto 声明，Scheduler 统一落账与改游标；
    节点 body 自己不调用下游、也不写 GO_REDIRECT。
    """
    session = make_session()
    run = _run(session)
    ctx = build_context(session, run, object())

    executed = []

    def body_a(_input, c):
        executed.append("a")
        # 第一次执行声明 goto 回自身，第二次正常完成
        if "visited" not in c.shared:
            return Outcome(
                shared={"visited": True},
                events=[("AUDIT_STARTED", "oracle_executor", {"attempt": 1})],
                goto="generation_planner",
                control={"reason": "self-heal"},
            )
        return Outcome(events=[("SCHEMA_ASSIGNED", "schema_mapper", {"tables": {}})])

    plan = WorkflowPlan(
        name="goto-test",
        nodes=(
            PlanNode("created", "起点", "stage"),
            PlanNode("generation_planner", "A", "stage"),
            PlanNode("schema_mapper", "终点", "terminal"),
        ),
        edges=(
            PlanEdge("created", "generation_planner", "always"),
            PlanEdge("generation_planner", "schema_mapper", "always"),
            PlanEdge("generation_planner", "generation_planner", "self_heal"),
        ),
        entry="created",
    )
    scheduler = Scheduler(plan, {"generation_planner": body_a})
    steps = scheduler.drive(ctx, max_steps=10)

    assert executed == ["a", "a"]  # body 被调用两次，都是 Scheduler 调的
    assert len(steps) == 2
    events = session.query(EventModel).filter(EventModel.run_id == run.id).order_by(EventModel.seq).all()
    redirects = [e for e in events if e.kind == "GO_REDIRECT"]
    assert len(redirects) == 1
    assert redirects[0].payload_json["target"] == "generation_planner"
    assert redirects[0].payload_json["reason"] == "self-heal"


def test_scheduler_routes_custom_conditional_edges_and_drains_bus_rejections():
    """Scheduler 完全依据 Outcome.when 查 Plan 条件边，不依赖任何硬编码节点名；
    同时在屏障处自动把 ctx.bus 拦截记录落为 BUS_REJECTED 事件。
    """
    session = make_session()
    run = _run(session)
    ctx = build_context(session, run, object())

    visited = []

    def body_router(_input, c):
        visited.append("router")
        # 触发一次 Bus 门禁拦截，验证屏障处自动落 BUS_REJECTED
        c.bus.adjudicate("assign_rules", {"rules": [{"field": "FAKE_COL"}]}, {"field_names": ["REAL_COL"]})
        return Outcome(shared={"plans": {"T1": {"k": 1}}}, when="custom_branch_b")

    def body_branch_b(_input, c):
        visited.append("branch_b")
        # Channel merge_dict reducer：二次写入自动合并字典，不丢 T1
        return Outcome(shared={"plans": {"T2": {"k": 2}}})

    plan = WorkflowPlan(
        name="custom-when",
        nodes=(
            PlanNode("created", "起点", "stage"),
            PlanNode("router", "路由", "stage"),
            PlanNode("branch_a", "分支A", "terminal"),
            PlanNode("branch_b", "分支B", "stage"),
            PlanNode("done", "完成", "terminal"),
        ),
        edges=(
            PlanEdge("created", "router", "always"),
            PlanEdge("router", "branch_a", "custom_branch_a"),
            PlanEdge("router", "branch_b", "custom_branch_b"),
            PlanEdge("branch_b", "done", "always"),
        ),
        entry="created",
    )
    Scheduler(plan, {"router": body_router, "branch_b": body_branch_b}).drive(ctx)

    assert visited == ["router", "branch_b"]
    assert ctx.get("plans") == {"T1": {"k": 1}, "T2": {"k": 2}}
    bus_events = session.query(EventModel).filter(EventModel.run_id == run.id, EventModel.kind == "BUS_REJECTED").all()
    assert len(bus_events) == 1
    assert "FAKE_COL" in bus_events[0].payload_json["reason"]


def test_build_context_recovers_channels_from_event_log():
    session = make_session()
    run = _run(session)
    append_event(session, run, "GENERATION_PLANNED", "plan_barrier", {"tables": {"T1": {"golden_count": 3}}})
    append_event(session, run, "SCHEMA_ASSIGNED", "schema_barrier", {"tables": {"T1": [{"field": "A"}]}})
    append_event(session, run, "CROSS_RULES_ASSIGNED", "cross_rule_binder", {"golden_overrides": {"T1": {"A": "X"}}})
    session.commit()

    recovered_ctx = build_context(session, run, object())
    assert recovered_ctx.get("use_plan") is True
    assert recovered_ctx.get("plans") == {"T1": {"golden_count": 3}}
    assert recovered_ctx.get("schema_payload") == {"T1": [{"field": "A"}]}
    assert recovered_ctx.get("golden_overrides") == {"T1": {"A": "X"}}
