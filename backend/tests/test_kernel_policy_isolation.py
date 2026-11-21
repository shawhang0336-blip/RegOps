"""ProdAgent 架构分层边界测试：验证「机制在内（app.kernel），策略在外（app.policy）」与整洁目录边界。

核心断言：
1. `app/kernel/*.py` 通过 AST 静态依赖检查，零引用任何业务域（ingestion / assets / engine / delivery / api）或策略层（policy）；
2. 仅依赖 `app.kernel` 即可独立定义并驱动一个与监管业务完全无关的通用工作流；
3. `backend/app/` 根目录保持整洁，只允许入口 `main.py` 与八大分层包（kernel / policy / infra / ingestion / assets / engine / delivery / api）。
"""

import ast
import uuid
from dataclasses import replace
from pathlib import Path

import app.kernel as kernel
import app.policy as policy
from app.infra.models import RunModel

from .helpers import make_session

FORBIDDEN_LAYERS_FOR_KERNEL = {
    "policy",
    "ingestion",
    "assets",
    "engine",
    "delivery",
    "api",
}

ALLOWED_ROOT_ENTRIES = {
    "__init__.py",
    "main.py",
    "kernel",
    "policy",
    "infra",
    "ingestion",
    "assets",
    "engine",
    "delivery",
    "api",
}

# 严格单向八层拓扑序：任意跨层 import 必须满足 LAYER_RANK[target] < LAYER_RANK[source]，
# 彻底杜绝反向依赖（如 engine -> api）与横向循环依赖（如 policy <-> engine、assets <-> engine）。
LAYER_RANK = {
    "infra": 0,
    "kernel": 1,
    "policy": 2,
    "ingestion": 3,
    "assets": 4,
    "engine": 5,
    "delivery": 6,
    "api": 7,
    "main": 8,
}


def test_kernel_has_zero_domain_or_policy_imports():
    """AST 静态检查：app/kernel/*.py 严禁 import 任何业务层或策略层模块。"""
    kernel_dir = Path(kernel.__file__).resolve().parent
    py_files = sorted(kernel_dir.glob("*.py"))
    assert len(py_files) >= 9

    violations = []
    for file_path in py_files:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = set(alias.name.split("."))
                    hit = parts & FORBIDDEN_LAYERS_FOR_KERNEL
                    if hit:
                        violations.append(f"{file_path.name}: import {alias.name} (命中禁用层 {sorted(hit)})")
            elif isinstance(node, ast.ImportFrom) and node.module:
                parts = set(node.module.split("."))
                hit = parts & FORBIDDEN_LAYERS_FOR_KERNEL
                if hit:
                    violations.append(f"{file_path.name}: from {node.module} import ... (命中禁用层 {sorted(hit)})")

    assert violations == [], "ProdAgent 微内核存在越界依赖：\n" + "\n".join(violations)


def test_kernel_can_drive_non_regops_workflow_independently():
    """证明 app.kernel 不依赖任何监管业务代码即可独立编排、门禁拦截、合并 Channel 并完成状态折叠。"""
    session = make_session()
    run = RunModel(id=str(uuid.uuid4()), spec_id="generic-app")
    session.add(run)
    session.flush()

    def custom_reducer(state: kernel.RunState, kind: str, payload: dict) -> kernel.RunState:
        if kind == "TASK_STARTED":
            return replace(state, status="RUNNING", current_node="start", when="always")
        if kind == "DRAFT_BUILT":
            return replace(state, status="RUNNING", current_node="draft", when="needs_review")
        if kind == "DONE":
            return replace(state, status="COMPLETED", current_node="finish", result_json=payload)
        return state

    kernel.append_event(session, run, "TASK_STARTED", "start", {})
    session.commit()

    custom_bus = kernel.Bus().add_adjudicator(
        lambda tool, args, _payload: "禁止空标题" if tool == "write_doc" and not args.get("title") else None
    )
    ctx = kernel.RunContext(
        session=session,
        run=run,
        settings=object(),
        state=kernel.project_with(session, run, reducer=custom_reducer),
        channels={"tags": kernel.ChannelSpec("tags", kernel.append_list)},
        bus=custom_bus,
        projector=lambda s, r: kernel.project_with(s, r, reducer=custom_reducer),
    )

    plan = kernel.WorkflowPlan(
        name="pure_kernel_demo",
        nodes=(
            kernel.PlanNode("start", "开始", "stage"),
            kernel.PlanNode("draft", "草稿", "stage"),
            kernel.PlanNode("finish", "完成", "terminal"),
        ),
        edges=(
            kernel.PlanEdge("start", "draft", "always"),
            kernel.PlanEdge("draft", "finish", "needs_review"),
        ),
        entry="start",
    )

    def body_draft(_input, c: kernel.RunContext) -> kernel.Outcome:
        reason = c.bus.check("write_doc", {"title": ""}, {})
        assert reason == "禁止空标题"
        c.put("tags", "v1")
        return kernel.Outcome(
            shared={"tags": "v2"},
            events=[
                ("DRAFT_BUILT", "draft", {}),
                ("DONE", "finish", {"ok": True}),
            ],
            when="needs_review",
        )

    steps = kernel.Scheduler(plan, {"draft": body_draft}).drive(ctx)
    assert [s.node for s in steps] == ["draft"]
    assert ctx.get("tags") == ["v1", "v2"]
    assert ctx.state.status == "COMPLETED"
    assert ctx.state.current_node == "finish"
    assert kernel.verify_chain(session, run) is True


def test_app_root_directory_stays_clean_and_layered():
    """防止根目录重新退化成大杂烩：app/ 下只允许 main.py 与八大架构包。"""
    app_dir = Path(kernel.__file__).resolve().parent.parent
    actual = {p.name for p in app_dir.iterdir() if p.name != "__pycache__"}
    assert actual == ALLOWED_ROOT_ENTRIES
    assert policy.DEFAULT_PLAN.validate() == []


def test_all_backend_layers_form_strict_dag():
    """AST 全局架构红线：所有跨层依赖必须严格从高层指向低层，零倒挂、零循环依赖。"""
    app_dir = Path(kernel.__file__).resolve().parent.parent
    violations = []

    for py_file in sorted(app_dir.rglob("*.py")):
        rel = py_file.relative_to(app_dir)
        src_layer = rel.parts[0] if len(rel.parts) > 1 else "main"
        if src_layer not in LAYER_RANK:
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    # 解析相对导入的目标包，防止用 `from ..xxx` 绕过架构红线
                    pkg_parts = list(("app",) + rel.parts[:-1])
                    if node.level <= len(pkg_parts):
                        base = pkg_parts[: len(pkg_parts) - node.level + 1]
                        if node.module:
                            base = base + node.module.split(".")
                        modules = [".".join(base)]
                elif node.module:
                    modules = [node.module]
            for mod in modules:
                if not mod.startswith("app."):
                    continue
                parts = mod.split(".")
                dst_layer = parts[1] if len(parts) > 1 else "main"
                if dst_layer == src_layer or dst_layer not in LAYER_RANK:
                    continue
                if LAYER_RANK[dst_layer] >= LAYER_RANK[src_layer]:
                    violations.append(
                        f"{rel} ({src_layer}={LAYER_RANK[src_layer]}) -> "
                        f"{mod} ({dst_layer}={LAYER_RANK[dst_layer]})"
                    )

    assert violations == [], "检测到违反严格单向分层 DAG 的越界依赖：\n" + "\n".join(violations)


def test_kernel_run_lifecycle_interrupt_cancel_recover_and_delete():
    """验证 app.kernel.run 第 7 内核原语：零业务依赖即可完成创世、一等 Interrupt、幂等回答、取消、恢复与钩子删除。"""
    import pytest

    session = make_session()
    bus = kernel.Bus()
    observed: list = []
    bus.subscribe("run.interrupted", lambda e: observed.append(("interrupted", e["interrupt_kind"])))
    bus.subscribe("run.failed", lambda e: observed.append(("failed", e["node"])))

    def reducer(state: kernel.RunState, kind: str, payload: dict) -> kernel.RunState:
        payload = payload or {}
        if kind == "RUN_CREATED":
            return replace(state, status="RUNNING", current_node="start", when="always", resume_from_node="start", resume_when="always")
        if kind == "STEP1_DONE":
            return replace(state, status="RUNNING", current_node="step1", when="always", resume_from_node="step1", resume_when="always")
        if kind == "NEED_HUMAN":
            return replace(state, status="WAITING_HUMAN", current_node="gate", when="approved", resume_from_node="gate", resume_when="approved")
        if kind == "HUMAN_ANSWERED":
            return replace(state, status="RUNNING", current_node="gate", when="approved", resume_from_node="gate", resume_when="approved")
        if kind == "DONE":
            return replace(state, status="COMPLETED", current_node="end", result_json=payload)
        if kind in {"RUN_FAILED", "RUN_CANCELLED"}:
            return replace(state, status="FAILED", current_node=payload.get("node", state.current_node), result_json=payload.get("result"))
        return state

    proj = lambda s, r: kernel.project_with(s, r, reducer=reducer)
    proj_many = lambda s, rs: kernel.project_many_with(s, rs, reducer=reducer)

    plan = kernel.WorkflowPlan(
        name="p0_kernel_test",
        nodes=(
            kernel.PlanNode("start", "开始", "stage"),
            kernel.PlanNode("step1", "步骤一", "stage"),
            kernel.PlanNode("gate", "人工门", "gate"),
            kernel.PlanNode("step2", "步骤二", "stage"),
            kernel.PlanNode("end", "结束", "terminal"),
        ),
        edges=(
            kernel.PlanEdge("start", "step1", "always"),
            kernel.PlanEdge("step1", "gate", "always"),
            kernel.PlanEdge("gate", "step2", "approved"),
            kernel.PlanEdge("step2", "end", "always"),
        ),
        entry="start",
    )

    # 1) create_run + 一等 Interrupt 挂起
    run = kernel.create_run(session, "demo-spec", initial_node="start", bus=bus)
    ctx = kernel.RunContext(
        session=session,
        run=run,
        settings=object(),
        state=proj(session, run),
        bus=bus,
        projector=proj,
    )

    def body_step1(_inp, _c):
        return kernel.Outcome(
            events=[("STEP1_DONE", "step1", {})],
            interrupt=kernel.Interrupt(
                kind="NEED_HUMAN",
                node="gate",
                question="请审批",
                pending_input={"k": 1},
                resume_node="gate",
            ),
        )

    def body_step2(_inp, _c):
        return kernel.Outcome(events=[("DONE", "end", {"ok": True})])

    steps = kernel.Scheduler(plan, {"step1": body_step1, "step2": body_step2}).drive(ctx)
    assert [s.node for s in steps] == ["step1"]
    assert proj(session, run).status == "WAITING_HUMAN"
    assert ("interrupted", "NEED_HUMAN") in observed

    # 2) answer_interrupt 幂等重放 + 冲突拦截
    st, intr = kernel.answer_interrupt(session, run, "NEED_HUMAN", "HUMAN_ANSWERED", "fp_ok")
    assert st == "PENDING" and intr is not None
    kernel.append_event(
        session,
        run,
        "HUMAN_ANSWERED",
        "gate",
        {
            "interrupt_id": intr.payload_json["interrupt_id"],
            "answer_fingerprint": "fp_ok",
        },
    )
    session.commit()
    st_replay, _ = kernel.answer_interrupt(session, run, "NEED_HUMAN", "HUMAN_ANSWERED", "fp_ok")
    assert st_replay == "REPLAY"
    with pytest.raises(ValueError, match="different decisions"):
        kernel.answer_interrupt(session, run, "NEED_HUMAN", "HUMAN_ANSWERED", "fp_conflict")

    # 3) CancellationToken 在节点边界生效
    token = kernel.CancellationToken()
    token.cancel("operator stop")
    ctx_cancel = kernel.RunContext(
        session=session,
        run=run,
        settings=object(),
        state=proj(session, run),
        bus=bus,
        projector=proj,
        cancellation=token,
    )
    with pytest.raises(kernel.RunCancelledError, match="operator stop"):
        kernel.Scheduler(plan, {"step1": body_step1, "step2": body_step2}).drive(ctx_cancel)

    # 4) recover_interrupted_runs 恢复卡在运行态的批次
    recovered = kernel.recover_interrupted_runs(session, ("RUNNING",), projector_many=proj_many, bus=bus)
    assert run.id in recovered
    assert proj(session, run).status == "FAILED"
    assert ("failed", "startup") in observed
    rep = kernel.replay_run(session, run, projector=proj)
    assert rep["chain_valid"] is True and rep["active"] is False

    # 5) delete_run 触发前置与提交后钩子并彻底清除
    hook_calls = []
    kernel.delete_run(
        session,
        run,
        active_statuses=("RUNNING",),
        projector=proj,
        pre_delete_hooks=(lambda _s, _r, c: hook_calls.append(("pre", c["run_id"])),),
        post_commit_hooks=(lambda _s, c: hook_calls.append(("post", c["run_id"])),),
    )
    assert hook_calls == [("pre", run.id), ("post", run.id)]
    assert session.get(RunModel, run.id) is None


def test_kernel_true_bsp_send_fanout_and_budget_guard_circuit_breaker():
    """验证 P1 内核能力：真 BSP `Send` 动态扇出波次并发归并 + `BudgetGuard` 横切成本统计与熔断（零业务依赖）。"""
    import pytest

    session = make_session()
    bus = kernel.Bus()
    waves_seen: list = []
    budget_breaches: list = []
    bus.subscribe("wave.fanout", lambda e: waves_seen.append(("fanout", e["wave_size"], e["labels"])))
    bus.subscribe("wave.barrier", lambda e: waves_seen.append(("barrier", e["merged_count"], e["labels"])))
    bus.subscribe("budget.exceeded", lambda e: budget_breaches.append((e["metric"], e["used"], e["limit"])))

    guard = kernel.BudgetGuard(kernel.BudgetLimits(max_llm_calls=10, max_total_tokens=10000), run_id="bsp-run").attach(bus)

    def reducer(state: kernel.RunState, kind: str, payload: dict) -> kernel.RunState:
        payload = payload or {}
        if kind == "RUN_CREATED":
            return replace(state, status="RUNNING", current_node="start", when="always")
        if kind == "DONE":
            return replace(state, status="COMPLETED", current_node="join", result_json=payload)
        return state

    proj = lambda s, r: kernel.project_with(s, r, reducer=reducer)
    plan = kernel.WorkflowPlan(
        name="bsp_send_demo",
        nodes=(
            kernel.PlanNode("start", "起点", "stage"),
            kernel.PlanNode("dispatch", "动态派发", "stage"),
            kernel.PlanNode("worker", "并行计算", "stage"),
            kernel.PlanNode("join", "屏障汇聚", "terminal"),
        ),
        edges=(
            kernel.PlanEdge("start", "dispatch", "always"),
            kernel.PlanEdge("dispatch", "worker", "always"),
            kernel.PlanEdge("worker", "join", "always"),
        ),
        entry="start",
    )

    run = kernel.create_run(session, "bsp-spec", initial_node="start", bus=bus)
    ctx = kernel.RunContext(
        session=session,
        run=run,
        settings=object(),
        state=proj(session, run),
        channels={"results": kernel.ChannelSpec("results", kernel.merge_dict)},
        bus=bus,
        projector=proj,
        concurrency=3,
        budget_guard=guard,
    )

    def body_dispatch(_inp, _c):
        return kernel.Outcome(
            sends=[
                kernel.Send("worker", {"item": "A", "v": 10}, label="shard-A"),
                kernel.Send("worker", {"item": "B", "v": 20}, label="shard-B"),
                kernel.Send("worker", {"item": "C", "v": 30}, label="shard-C"),
            ]
        )

    def body_worker(inp, c):
        c.bus.fire("tool.after", {"tool": "mock_tool", "attempts": 1, "elapsed_seconds": 0.25, "usage": {"prompt_tokens": 50, "completion_tokens": 25, "total_tokens": 75}})
        return kernel.Outcome(
            shared={"results": {inp["item"]: inp["v"] * 2}},
            events=[("WORKER_DONE", "worker", {"item": inp["item"]})],
        )

    def body_join(_inp, c):
        merged = c.get("results") or {}
        return kernel.Outcome(events=[("DONE", "join", {"merged": merged, "sum": sum(merged.values())})])

    steps = kernel.Scheduler(plan, {"dispatch": body_dispatch, "worker": body_worker, "join": body_join}).drive(ctx)
    assert [s.node for s in steps] == ["dispatch", "worker", "worker", "worker", "join"]
    assert ctx.get("results") == {"A": 20, "B": 40, "C": 60}
    assert proj(session, run).status == "COMPLETED"
    assert proj(session, run).result_json == {"merged": {"A": 20, "B": 40, "C": 60}, "sum": 120}
    assert ("fanout", 3, ["shard-A", "shard-B", "shard-C"]) in waves_seen
    assert ("barrier", 3, ["shard-A", "shard-B", "shard-C"]) in waves_seen
    assert kernel.verify_chain(session, run) is True

    # 验证 BudgetGuard 精确统计全部波次的调用、Token 与步数
    snap = guard.snapshot()
    assert snap["llm_calls"] == 3
    assert snap["total_tokens"] == 225
    assert snap["steps"] == 5

    # 验证超限熔断：新建严格上限的 BudgetGuard，超限时广播 budget.exceeded 并抛 BudgetExceededError
    tight_bus = kernel.Bus()
    tight_bus.subscribe("budget.exceeded", lambda e: budget_breaches.append((e["metric"], e["used"], e["limit"])))
    tight_guard = kernel.BudgetGuard(kernel.BudgetLimits(max_total_tokens=100), run_id="tight-run").attach(tight_bus)
    tight_bus.fire("tool.after", {"tool": "t", "attempts": 1, "usage": {"total_tokens": 120}})
    with pytest.raises(kernel.BudgetExceededError) as exc_info:
        tight_bus.check_budget()
    assert exc_info.value.metric == "max_total_tokens"
    assert ("max_total_tokens", 120, 100) in budget_breaches


