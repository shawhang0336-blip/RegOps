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
from app.infra.db_tables import RunModel

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


EXPECTED_KERNEL_MODULES = {
    "__init__.py",
    "plan.py",
    "bus.py",
    "event_log.py",
    "interrupt.py",
    "run.py",
    "scheduler.py",
}


def test_kernel_has_zero_domain_or_policy_imports():
    """AST 静态检查：app/kernel/ 严格收敛为六大核心模块，且严禁 import 任何业务层或策略层模块。"""
    kernel_dir = Path(kernel.__file__).resolve().parent
    py_files = sorted(kernel_dir.glob("*.py"))
    assert {p.name for p in py_files} == EXPECTED_KERNEL_MODULES

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


def test_kernel_ambiguous_write_guard_and_add_number_reducer():
    """验证 BSP 屏障的 AmbiguousWriteError 并发写冲突守卫与 add_number 累加 Reducer。"""
    import pytest

    session = make_session()

    def reducer(state: kernel.RunState, kind: str, payload: dict) -> kernel.RunState:
        if kind == "RUN_CREATED":
            return replace(state, status="RUNNING", current_node="start", when="always")
        if kind == "DONE":
            return replace(state, status="COMPLETED", current_node="join", result_json=payload)
        return state

    proj = lambda s, r: kernel.project_with(s, r, reducer=reducer)
    plan = kernel.WorkflowPlan(
        name="ambiguous_write_demo",
        nodes=(
            kernel.PlanNode("start", "起点", "stage"),
            kernel.PlanNode("fanout", "派发", "stage"),
            kernel.PlanNode("worker", "并发节点", "stage"),
            kernel.PlanNode("join", "汇总", "terminal"),
        ),
        edges=(
            kernel.PlanEdge("start", "fanout", "always"),
            kernel.PlanEdge("fanout", "worker", "always"),
            kernel.PlanEdge("worker", "join", "always"),
        ),
        entry="start",
    )

    # 1) 同波次并发写 last_value 通道 -> 立即触发 AmbiguousWriteError，且不污染事件账本与 shared
    run1 = kernel.create_run(session, "aw-spec-1", initial_node="start")
    ctx1 = kernel.RunContext(
        session=session,
        run=run1,
        settings=object(),
        state=proj(session, run1),
        channels={"phase": kernel.ChannelSpec("phase", kernel.last_value)},
        projector=proj,
    )

    def fanout_body(_inp, _c):
        return kernel.Outcome(
            sends=[
                kernel.Send("worker", {"n": 10}, label="w1"),
                kernel.Send("worker", {"n": 20}, label="w2"),
            ]
        )

    def conflict_worker(inp, _c):
        return kernel.Outcome(
            shared={"phase": f"from-{inp['n']}"},
            events=[("WORKER_STEP", "worker", {"n": inp["n"]})],
        )

    with pytest.raises(kernel.AmbiguousWriteError) as exc_info:
        kernel.Scheduler(
            plan,
            {"fanout": fanout_body, "worker": conflict_worker, "join": lambda _i, _c: kernel.Outcome()},
        ).drive(ctx1)
    assert exc_info.value.channel == "phase"
    assert exc_info.value.writers == ["w1", "w2"]
    assert "phase" not in ctx1.shared
    # 冲突在落账前被拦下，WORKER_STEP 未写入 EventLog
    trace_conflict = kernel.project_trace(session, run1, plan=plan)
    assert all("WORKER_STEP" not in s.kinds for s in trace_conflict.spans)

    # 2) 使用 add_number / merge_dict / append_list / 显式 allow_multi=True 通道 -> 并发归约成功
    run2 = kernel.create_run(session, "aw-spec-2", initial_node="start")
    ctx2 = kernel.RunContext(
        session=session,
        run=run2,
        settings=object(),
        state=proj(session, run2),
        channels={
            "total": kernel.ChannelSpec("total", kernel.add_number),
            "last_seen": kernel.ChannelSpec("last_seen", kernel.last_value, allow_multi=True),
        },
        projector=proj,
    )

    def safe_worker(inp, _c):
        return kernel.Outcome(
            shared={"total": inp["n"], "last_seen": inp["n"]},
            events=[("WORKER_STEP", "worker", {"n": inp["n"], "label": f"shard-{inp['n']}"})],
        )

    def join_body(_inp, c):
        return kernel.Outcome(events=[("DONE", "join", {"total": c.get("total"), "last_seen": c.get("last_seen")})])

    steps = kernel.Scheduler(
        plan,
        {"fanout": fanout_body, "worker": safe_worker, "join": join_body},
    ).drive(ctx2)
    assert [s.node for s in steps] == ["fanout", "worker", "worker", "join"]
    assert ctx2.get("total") == 30
    assert ctx2.get("last_seen") == 20
    assert proj(session, run2).status == "COMPLETED"


def test_kernel_node_retry_policy_timeout_and_eventlog_trace():
    """验证节点级声明式 RetryPolicy / timeout、取消永不重试，以及从 EventLog 零成本折叠 Trace/Span。"""
    import time
    import pytest

    session = make_session()
    bus = kernel.Bus()
    retries_seen: list = []
    exits_seen: list = []
    bus.subscribe("node.retry", lambda e: retries_seen.append((e["node"], e["attempt"], e["max_attempts"])))
    bus.subscribe("node.exit", lambda e: exits_seen.append((e["node"], e["attempts"], e["duration_ms"])))

    def reducer(state: kernel.RunState, kind: str, payload: dict) -> kernel.RunState:
        if kind == "RUN_CREATED":
            return replace(state, status="RUNNING", current_node="start", when="always")
        if kind == "DONE":
            return replace(state, status="COMPLETED", current_node="done", result_json=payload)
        return state

    proj = lambda s, r: kernel.project_with(s, r, reducer=reducer)
    plan = kernel.WorkflowPlan(
        name="resilience_trace_demo",
        nodes=(
            kernel.PlanNode("start", "起点", "stage"),
            kernel.PlanNode(
                "flaky",
                "抖动节点",
                "stage",
                retry=kernel.RetryPolicy(max_attempts=3, base_delay=0.005, factor=1.5, retry_on=(ConnectionError,)),
            ),
            kernel.PlanNode(
                "timed",
                "超时自愈节点",
                "stage",
                timeout=0.04,
                retry=kernel.RetryPolicy(max_attempts=2, base_delay=0.005, retry_on=(TimeoutError,)),
            ),
            kernel.PlanNode("done", "完成", "terminal"),
        ),
        edges=(
            kernel.PlanEdge("start", "flaky", "always"),
            kernel.PlanEdge("flaky", "timed", "always"),
            kernel.PlanEdge("timed", "done", "always"),
        ),
        entry="start",
    )

    flaky_calls = {"n": 0}
    timed_calls = {"n": 0}

    def body_flaky(_inp, _c):
        flaky_calls["n"] += 1
        if flaky_calls["n"] < 3:
            raise ConnectionError(f"blip #{flaky_calls['n']}")
        return kernel.Outcome(events=[("FLAKY_OK", "flaky", {"elapsed_seconds": 0.12})])

    def body_timed(_inp, _c):
        timed_calls["n"] += 1
        if timed_calls["n"] == 1:
            time.sleep(0.15)
        return kernel.Outcome(events=[("DONE", "done", {"ok": True, "duration_ms": 15})])

    run = kernel.create_run(session, "resilience-spec", initial_node="start", bus=bus)
    ctx = kernel.RunContext(
        session=session,
        run=run,
        settings=object(),
        state=proj(session, run),
        bus=bus,
        projector=proj,
    )

    steps = kernel.Scheduler(plan, {"flaky": body_flaky, "timed": body_timed}).drive(ctx)
    assert [s.node for s in steps] == ["flaky", "timed"]
    assert steps[0].attempts == 3
    assert steps[1].attempts == 2
    assert steps[0].duration_ms >= 0
    assert retries_seen == [("flaky", 1, 3), ("flaky", 2, 3), ("timed", 1, 2)]
    assert any(node == "flaky" and attempts == 3 for node, attempts, _ in exits_seen)
    assert proj(session, run).status == "COMPLETED"
    assert kernel.verify_chain(session, run) is True

    # 从同一份 EventLog 折叠 Trace 视图（含重试统计、节点汇总、中文标签与文本瀑布）
    trace = kernel.project_trace(session, run, plan=plan)
    assert trace.run_id == run.id
    assert trace.status == "ok"
    assert trace.retry_count == 3
    assert trace.by_node["flaky"]["retries"] == 2
    assert trace.by_node["flaky"]["label"] == "抖动节点"
    assert trace.by_node["flaky"]["total_duration_ms"] == 120
    assert trace.by_node["timed"]["retries"] == 1
    rendered = kernel.render_trace(trace)
    assert "抖动节点" in rendered and "retries=2" in rendered

    # 非匹配异常（ValueError）不触发重试；取消异常永不重试
    non_match_calls = {"n": 0}
    run_err = kernel.create_run(session, "err-spec", initial_node="start")
    ctx_err = kernel.RunContext(session=session, run=run_err, settings=object(), state=proj(session, run_err), projector=proj)

    def body_bad(_inp, _c):
        non_match_calls["n"] += 1
        raise ValueError("not retryable")

    with pytest.raises(ValueError, match="not retryable"):
        kernel.Scheduler(plan, {"flaky": body_bad, "timed": body_timed}).drive(ctx_err)
    assert non_match_calls["n"] == 1

    # timeout 超限且未配置重试时直接抛 NodeTimeoutError
    strict_plan = kernel.WorkflowPlan(
        name="strict_timeout",
        nodes=(
            kernel.PlanNode("start", "起点", "stage"),
            kernel.PlanNode("slow", "慢节点", "stage", timeout=0.03),
            kernel.PlanNode("end", "终点", "terminal"),
        ),
        edges=(
            kernel.PlanEdge("start", "slow", "always"),
            kernel.PlanEdge("slow", "end", "always"),
        ),
        entry="start",
    )
    run_to = kernel.create_run(session, "to-spec", initial_node="start")
    ctx_to = kernel.RunContext(session=session, run=run_to, settings=object(), state=proj(session, run_to), projector=proj)
    with pytest.raises(kernel.NodeTimeoutError):
        kernel.Scheduler(strict_plan, {"slow": lambda _i, _c: (time.sleep(0.15), kernel.Outcome())[1]}).drive(ctx_to)


def test_runs_trace_api_projects_regops_eventlog_without_extra_tables():
    """验证 /api/runs/{run_id}/trace 直接从 RegOps EventLog 折叠耗时瀑布与分片维度。"""
    from app.api.runs import trace_run

    session = make_session()
    run = kernel.create_run(session, "regops-trace-spec", initial_node="created")
    kernel.append_event(
        session,
        run,
        "AI_TOOL_ASSIGNED",
        "rule_table_worker",
        {"table_code": "T01", "elapsed_seconds": 1.25},
    )
    kernel.append_event(
        session,
        run,
        "TABLE_RULES_BOUND",
        "rule_table_worker",
        {"table_code": "T01", "rule_count": 4},
    )
    kernel.append_event(
        session,
        run,
        "AUDIT_COMPLETED",
        "oracle_executor",
        {"result": {"all_passed": True}},
    )
    session.commit()

    payload = trace_run(run.id, session=session)
    assert payload["run_id"] == run.id
    assert payload["event_count"] == 4
    worker_spans = [s for s in payload["spans"] if s["node"] == "rule_table_worker" and s["shard"] == "T01"]
    assert len(worker_spans) == 1
    assert worker_spans[0]["label"] == "单表规则并行绑定"
    assert worker_spans[0]["duration_ms"] == 1250
    assert payload["by_node"]["oracle_executor"]["label"] == "Oracle 沙箱审计"


def test_kernel_resilience_jitter_dual_ids_and_cooperative_cancellation():
    """验证弹性四板斧补齐项：Jitter 防惊群、call_id vs idempotency_key 双 ID 隔离、退避与波次取消即时唤醒。"""
    import threading
    import time
    import pytest
    from app.infra.llm.guard import backoff_wait

    # 1) RetryPolicy 指数退避 + Jitter 边界校验
    no_jitter = kernel.RetryPolicy(max_attempts=3, base_delay=0.1, factor=2.0, jitter=0.0)
    assert no_jitter.delay_for(0) == pytest.approx(0.1)
    assert no_jitter.delay_for(1) == pytest.approx(0.2)
    with_jitter = kernel.RetryPolicy(max_attempts=3, base_delay=1.0, factor=2.0, jitter=0.2, max_delay=2.1)
    samples = [with_jitter.delay_for(1) for _ in range(30)]
    assert all(1.6 <= d <= 2.1 for d in samples)
    assert len(set(round(d, 6) for d in samples)) > 1

    # 2) infra.llm.guard.backoff_wait 在长退避期间收到取消信号立即唤醒并抛 RunCancelledError（不傻等 10s）
    cancel_tok = kernel.CancellationToken()
    threading.Timer(0.03, lambda: cancel_tok.cancel("stop during backoff")).start()
    t0 = time.monotonic()
    with pytest.raises(kernel.RunCancelledError, match="stop during backoff"):
        backoff_wait((10.0,), 1, cancellation=cancel_tok, jitter=0.0)
    assert time.monotonic() - t0 < 1.0

    # 3) 双 ID（call_id 每次尝试递增变化；idempotency_key 跨重试恒定、且并发 Send Worker 之间严格隔离）
    session = make_session()
    bus = kernel.Bus()
    retry_bus_events: list = []
    bus.subscribe("node.retry", lambda e: retry_bus_events.append((e["call_id"], e["idempotency_key"])))

    def reducer(state: kernel.RunState, kind: str, payload: dict) -> kernel.RunState:
        if kind == "RUN_CREATED":
            return replace(state, status="RUNNING", current_node="start", when="always")
        if kind == "DONE":
            return replace(state, status="COMPLETED", current_node="join", result_json=payload)
        return state

    proj = lambda s, r: kernel.project_with(s, r, reducer=reducer)
    plan = kernel.WorkflowPlan(
        name="dual_id_demo",
        nodes=(
            kernel.PlanNode("start", "起点", "stage"),
            kernel.PlanNode("fanout", "派发", "stage"),
            kernel.PlanNode(
                "worker",
                "幂等并发表",
                "stage",
                timeout=1.0,
                retry=kernel.RetryPolicy(max_attempts=3, base_delay=0.005, jitter=0.0, retry_on=(ConnectionError,)),
            ),
            kernel.PlanNode("join", "汇合", "terminal"),
        ),
        edges=(
            kernel.PlanEdge("start", "fanout", "always"),
            kernel.PlanEdge("fanout", "worker", "always"),
            kernel.PlanEdge("worker", "join", "always"),
        ),
        entry="start",
    )

    seen_attempts: dict = {}
    side_effect_ledger: dict = {}
    lock = threading.Lock()

    def body_fanout(_inp, _c):
        return kernel.Outcome(
            sends=[
                kernel.Send("worker", "T1", label="T1"),
                kernel.Send("worker", "T2", label="T2"),
            ]
        )

    def body_worker(tcode, c: kernel.RunContext):
        assert c.call_id is not None and c.idempotency_key is not None
        with lock:
            rec = seen_attempts.setdefault(tcode, [])
            rec.append((c.call_id, c.idempotency_key))
            # 模拟按稳定业务幂等键登记副作用（重试时同键去重，不会写入两次）
            side_effect_ledger.setdefault(c.idempotency_key, []).append(c.call_id)
            attempt_n = len(rec)
        if tcode == "T1" and attempt_n == 1:
            raise ConnectionError("transient network drop on T1")
        return kernel.Outcome(shared={"done": {tcode: c.idempotency_key}})

    def body_join(_inp, c):
        return kernel.Outcome(events=[("DONE", "join", {"done": c.get("done")})])

    run = kernel.create_run(session, "dual-id-spec", initial_node="start", bus=bus)
    ctx = kernel.RunContext(
        session=session,
        run=run,
        settings=object(),
        state=proj(session, run),
        channels={"done": kernel.ChannelSpec("done", kernel.merge_dict)},
        bus=bus,
        projector=proj,
        concurrency=2,
    )
    steps = kernel.Scheduler(
        plan, {"fanout": body_fanout, "worker": body_worker, "join": body_join}
    ).drive(ctx)

    # T1 重试 2 次：call_id 严格不同，idempotency_key 严格相同；T1 与 T2 幂等键彼此隔离
    assert len(seen_attempts["T1"]) == 2
    t1_c1, t1_k1 = seen_attempts["T1"][0]
    t1_c2, t1_k2 = seen_attempts["T1"][1]
    t2_c1, t2_k1 = seen_attempts["T2"][0]
    assert t1_c1 != t1_c2
    assert t1_k1 == t1_k2
    assert t1_k1 != t2_k1
    assert len({t1_c1, t1_c2, t2_c1}) == 3
    assert retry_bus_events == [(t1_c1, t1_k1)]
    worker_steps = [s for s in steps if s.node == "worker"]
    assert {s.label: s.idempotency_key for s in worker_steps} == {"T1": t1_k1, "T2": t2_k1}
    assert {s.label: s.call_id for s in worker_steps} == {"T1": t1_c2, "T2": t2_c1}

    # 4) execute_wave 收到取消时立即撤销尚未开始排队的后续 future
    wave_cancel = kernel.CancellationToken()
    executed_items: list = []

    def wave_worker(item: int) -> int:
        executed_items.append(item)
        if item == 1:
            wave_cancel.cancel("stop wave")
            wave_cancel.raise_if_cancelled()
        time.sleep(0.05)
        return item

    with pytest.raises(kernel.RunCancelledError, match="stop wave"):
        kernel.execute_wave(
            [1, 2, 3, 4, 5, 6],
            wave_worker,
            max_workers=1,
            cancellation=wave_cancel,
        )
    assert executed_items == [1]


def test_kernel_wave_multi_error_aggregation_and_edge_input_delivery():
    """验证同波多报错全量汇总（WaveExecutionError 不吞错）+ 6 种前驱/Goto/Send 沿边自动喂入 input。"""
    import pytest

    # 1) 同波多错汇总：3 张表并发，T1 成功（进入 partial_results_out），T2/T3 同时报错 -> 抛 WaveExecutionError 保留两项错误
    partial: list = []

    def multi_fail_worker(label: str) -> str:
        if label == "T2":
            raise ValueError("bad field on T2")
        if label == "T3":
            raise RuntimeError("timeout on T3")
        return f"ok:{label}"

    with pytest.raises(kernel.WaveExecutionError) as exc_info:
        kernel.execute_wave(
            ["T1", "T2", "T3"],
            multi_fail_worker,
            labels=["T1", "T2", "T3"],
            max_workers=3,
            partial_results_out=partial,
        )
    assert partial == ["ok:T1"]
    assert exc_info.value.failed_labels == ["T2", "T3"]
    assert len(exc_info.value.errors) == 2
    assert isinstance(exc_info.value.errors[0][1], ValueError)
    assert isinstance(exc_info.value.errors[1][1], RuntimeError)
    assert "T2" in str(exc_info.value) and "T3" in str(exc_info.value)

    # 2) 单错仍原样抛出原始异常（不额外包装）
    with pytest.raises(ValueError, match="bad field on T2"):
        kernel.execute_wave(["T1", "T2"], multi_fail_worker, labels=["T1", "T2"], max_workers=2)

    # 3) 沿边数据自动喂入 input：入口 initial_input -> 单前驱 -> 静态多前驱 dict -> Send 动态 payload -> 模板前驱 list -> Goto control
    session = make_session()

    def reducer(state: kernel.RunState, kind: str, payload: dict) -> kernel.RunState:
        if kind == "RUN_CREATED":
            return replace(state, status="RUNNING", current_node="start", when="always")
        if kind == "DONE":
            return replace(state, status="COMPLETED", current_node="finish", result_json=payload)
        return state

    proj = lambda s, r: kernel.project_with(s, r, reducer=reducer)
    plan = kernel.WorkflowPlan(
        name="edge_input_demo",
        nodes=(
            kernel.PlanNode("start", "入口", "stage"),
            kernel.PlanNode("prep", "预处理", "stage"),
            kernel.PlanNode("branch_a", "静态分支A", "stage"),
            kernel.PlanNode("branch_b", "静态分支B", "stage"),
            kernel.PlanNode("merge", "多前驱汇合", "stage"),
            kernel.PlanNode("worker", "Send模板", "stage"),
            kernel.PlanNode("collect", "模板汇合", "stage"),
            kernel.PlanNode("finish", "终点", "terminal"),
        ),
        edges=(
            kernel.PlanEdge("start", "prep", "always"),
            kernel.PlanEdge("prep", "branch_a", "always"),
            kernel.PlanEdge("prep", "branch_b", "always"),
            kernel.PlanEdge("branch_a", "merge", "always"),
            kernel.PlanEdge("branch_b", "merge", "always"),
            kernel.PlanEdge("merge", "worker", "always"),
            kernel.PlanEdge("worker", "collect", "always"),
            kernel.PlanEdge("collect", "finish", "always"),
        ),
        entry="start",
    )

    received: dict = {}

    def body_prep(inp, _c):
        received["prep"] = inp
        return kernel.Outcome(output={"seed": inp["task"], "v": 10})

    def body_branch_a(inp, _c):
        received["branch_a"] = inp
        return kernel.Outcome(output=inp["v"] + 1)

    def body_branch_b(inp, _c):
        received["branch_b"] = inp
        return kernel.Outcome(output=inp["v"] + 2)

    def body_merge(inp, _c):
        received["merge"] = inp
        # 收到 {"branch_a": 11, "branch_b": 12} -> 动态 Send 两个副本
        return kernel.Outcome(
            sends=[
                kernel.Send("worker", {"k": "a", "val": inp["branch_a"]}, label="a"),
                kernel.Send("worker", {"k": "b", "val": inp["branch_b"]}, label="b"),
            ]
        )

    def body_worker(inp, _c):
        return kernel.Outcome(output=f"{inp['k']}:{inp['val'] * 2}")

    def body_collect(inp, c):
        received.setdefault("collect_calls", []).append(inp)
        # 第一次收到 Send 模板列表 -> 演示 Goto 传递 control 作为重入 input
        if len(received["collect_calls"]) == 1:
            return kernel.Outcome(goto="collect", control={"retry_items": inp, "round": 2})
        return kernel.Outcome(events=[("DONE", "finish", {"final": inp})])

    run = kernel.create_run(session, "edge-input-spec", initial_node="start")
    ctx = kernel.RunContext(
        session=session,
        run=run,
        settings=object(),
        state=proj(session, run),
        projector=proj,
        concurrency=2,
    )
    kernel.Scheduler(
        plan,
        {
            "prep": body_prep,
            "branch_a": body_branch_a,
            "branch_b": body_branch_b,
            "merge": body_merge,
            "worker": body_worker,
            "collect": body_collect,
        },
    ).drive(ctx, initial_input={"task": "hello"})

    assert received["prep"] == {"task": "hello"}
    assert received["branch_a"] == {"seed": "hello", "v": 10}
    assert received["branch_b"] == {"seed": "hello", "v": 10}
    assert received["merge"] == {"branch_a": 11, "branch_b": 12}
    assert received["collect_calls"][0] == ["a:22", "b:24"]
    assert received["collect_calls"][1] == {"retry_items": ["a:22", "b:24"], "round": 2}
    assert proj(session, run).status == "COMPLETED"
    assert proj(session, run).result_json == {"final": {"retry_items": ["a:22", "b:24"], "round": 2}}


def test_kernel_scheduler_guards_max_waves_and_graph_stalled():
    """验证调度器两项止损兜底：无限 Goto 环跑满 max_steps 抛 MaxWavesExceededError；断链 when 抛 GraphStalledError。"""
    import pytest

    session = make_session()

    def reducer(state: kernel.RunState, kind: str, payload: dict) -> kernel.RunState:
        if kind == "RUN_CREATED":
            return replace(state, status="RUNNING", current_node="start", when="always")
        if kind == "DONE":
            return replace(state, status="COMPLETED", current_node="end", result_json=payload)
        return state

    proj = lambda s, r: kernel.project_with(s, r, reducer=reducer)

    # 1) 无限 Goto 死循环 -> 跑满 max_steps=4 明确抛出 MaxWavesExceededError 熔断
    loop_plan = kernel.WorkflowPlan(
        name="infinite_loop_guard",
        nodes=(
            kernel.PlanNode("start", "起点", "stage"),
            kernel.PlanNode("looper", "自环节点", "stage"),
            kernel.PlanNode("end", "终点", "terminal"),
        ),
        edges=(
            kernel.PlanEdge("start", "looper", "always"),
            kernel.PlanEdge("looper", "end", "done"),
        ),
        entry="start",
    )
    run_loop = kernel.create_run(session, "loop-spec", initial_node="start")
    ctx_loop = kernel.RunContext(
        session=session, run=run_loop, settings=object(), state=proj(session, run_loop), projector=proj
    )
    with pytest.raises(kernel.MaxWavesExceededError) as loop_exc:
        kernel.Scheduler(
            loop_plan,
            {"looper": lambda _i, _c: kernel.Outcome(goto="looper", control={"reason": "spin"})},
        ).drive(ctx_loop, max_steps=4)
    assert loop_exc.value.max_steps == 4
    assert loop_exc.value.pending_nodes == ["looper"]

    # 2) 条件边断链（when="typo_branch" 找不到任何匹配边，且非终态/非门控）-> 抛出 GraphStalledError 并点名卡死节点
    stall_plan = kernel.WorkflowPlan(
        name="stall_guard",
        nodes=(
            kernel.PlanNode("start", "起点", "stage"),
            kernel.PlanNode("router", "路由", "stage"),
            kernel.PlanNode("ok_end", "成功终态", "terminal"),
            kernel.PlanNode("err_end", "失败终态", "terminal"),
        ),
        edges=(
            kernel.PlanEdge("start", "router", "always"),
            kernel.PlanEdge("router", "ok_end", "ok"),
            kernel.PlanEdge("router", "err_end", "err"),
        ),
        entry="start",
    )
    run_stall = kernel.create_run(session, "stall-spec", initial_node="start")
    ctx_stall = kernel.RunContext(
        session=session, run=run_stall, settings=object(), state=proj(session, run_stall), projector=proj
    )
    with pytest.raises(kernel.GraphStalledError) as stall_exc:
        kernel.Scheduler(
            stall_plan,
            {"router": lambda _i, _c: kernel.Outcome(when="typo_branch")},
        ).drive(ctx_stall)
    assert stall_exc.value.stalled_nodes == ["router"]
    assert stall_exc.value.when_tags == {"router": "typo_branch"}


def test_kernel_outcome_park_multi_interrupt_and_scheduler_resume():
    """验证 HITL 落盘放手闭环：Outcome.park 自动补 node、同波多中断全落账、Scheduler.resume 线程隔离喂值重跑。"""
    from app.infra.db_tables import EventModel

    session = make_session()
    bus = kernel.Bus()
    interrupted_events: list = []
    resumed_events: list = []
    bus.subscribe("run.interrupted", lambda e: interrupted_events.append(e))
    bus.subscribe("run.resumed", lambda e: resumed_events.append(e))

    def reducer(state: kernel.RunState, kind: str, payload: dict) -> kernel.RunState:
        payload = payload or {}
        if kind == "RUN_CREATED":
            return replace(state, status="RUNNING", current_node="start", when="always")
        if kind == "INPUT_REQUIRED":
            return replace(
                state,
                status="WAITING_INPUT",
                current_node=payload.get("resume_node") or state.current_node,
                resume_from_node=payload.get("resume_node") or state.current_node,
            )
        if kind == "RUN_RESUMED":
            return replace(state, status="RUNNING")
        if kind == "DONE":
            return replace(state, status="COMPLETED", current_node="merge", result_json=payload)
        return state

    proj = lambda s, r: kernel.project_with(s, r, reducer=reducer)

    # 1) 单节点中途 Outcome.park -> 落盘放手 -> 新建 RunContext 用 Scheduler.resume 喂裸值重跑
    single_plan = kernel.WorkflowPlan(
        name="single_park_resume",
        nodes=(
            kernel.PlanNode("start", "起点", "stage"),
            kernel.PlanNode("ask", "询问汇率", "stage"),
            kernel.PlanNode("finish", "汇总结算", "terminal"),
        ),
        edges=(
            kernel.PlanEdge("start", "ask", "always"),
            kernel.PlanEdge("ask", "finish", "always"),
        ),
        entry="start",
    )
    finish_seen_resume = []

    def body_ask(_inp, c: kernel.RunContext):
        if c.resume_value is None:
            return kernel.Outcome.park("INPUT_REQUIRED", "请提供 USD 汇率", {"currency": "USD"})
        return kernel.Outcome(output={"rate": c.resume_value["rate"], "amount": 100 * c.resume_value["rate"]})

    def body_finish(inp, c: kernel.RunContext):
        finish_seen_resume.append(c.resume_value)
        return kernel.Outcome(events=[("DONE", "finish", inp)])

    run1 = kernel.create_run(session, "park-single", initial_node="start", bus=bus)
    ctx1 = kernel.RunContext(session=session, run=run1, settings=object(), state=proj(session, run1), bus=bus, projector=proj)
    sch1 = kernel.Scheduler(single_plan, {"ask": body_ask, "finish": body_finish})
    sch1.drive(ctx1)
    assert proj(session, run1).status == "WAITING_INPUT"
    assert interrupted_events[-1]["node"] == "ask"
    assert interrupted_events[-1]["interrupts"][0]["node"] == "ask"

    # 模拟进程重启后重建全新 RunContext，通过 Scheduler.resume 喂入外部值重跑 ask
    ctx1_rebuilt = kernel.RunContext(session=session, run=run1, settings=object(), state=proj(session, run1), bus=bus, projector=proj)
    sch1.resume(ctx1_rebuilt, resume_value={"rate": 7.25})
    assert proj(session, run1).status == "COMPLETED"
    assert proj(session, run1).result_json == {"rate": 7.25, "amount": 725.0}
    # resume_value 只对被恢复的首波生效，不污染下游 finish 节点
    assert finish_seen_resume == [None]
    assert len(resumed_events) == 1
    assert kernel.verify_chain(session, run1) is True

    # 2) 同波双节点并发挂起 -> 两条中断工单全部落账 -> Scheduler.resume 传 {node: value} 并发线程隔离重跑并汇合
    multi_plan = kernel.WorkflowPlan(
        name="multi_park_resume",
        nodes=(
            kernel.PlanNode("start", "起点", "stage"),
            kernel.PlanNode("branch_a", "审批A", "stage"),
            kernel.PlanNode("branch_b", "审批B", "stage"),
            kernel.PlanNode("merge", "终态汇总", "terminal"),
        ),
        edges=(
            kernel.PlanEdge("start", "branch_a", "always"),
            kernel.PlanEdge("start", "branch_b", "always"),
            kernel.PlanEdge("branch_a", "merge", "always"),
            kernel.PlanEdge("branch_b", "merge", "always"),
        ),
        entry="start",
    )

    def body_branch_a(_inp, c: kernel.RunContext):
        if c.resume_value is None:
            return kernel.Outcome.park("INPUT_REQUIRED", "A 需要审批", {"branch": "A"})
        return kernel.Outcome(output=f"A:{c.resume_value}")

    def body_branch_b(_inp, c: kernel.RunContext):
        if c.resume_value is None:
            return kernel.Outcome.park("INPUT_REQUIRED", "B 需要审批", {"branch": "B"})
        return kernel.Outcome(output=f"B:{c.resume_value}")

    def body_merge(inp, c: kernel.RunContext):
        assert c.resume_value is None
        return kernel.Outcome(events=[("DONE", "merge", inp)])

    run2 = kernel.create_run(session, "park-multi", initial_node="start", bus=bus)
    ctx2 = kernel.RunContext(
        session=session, run=run2, settings=object(), state=proj(session, run2), bus=bus, projector=proj, concurrency=2
    )
    sch2 = kernel.Scheduler(
        multi_plan, {"branch_a": body_branch_a, "branch_b": body_branch_b, "merge": body_merge}
    )
    sch2.drive(ctx2)
    intr_rows = (
        session.query(EventModel)
        .filter(EventModel.run_id == run2.id, EventModel.kind == "INPUT_REQUIRED")
        .order_by(EventModel.seq)
        .all()
    )
    assert [row.node for row in intr_rows] == ["branch_a", "branch_b"]
    assert [i["node"] for i in interrupted_events[-1]["interrupts"]] == ["branch_a", "branch_b"]

    ctx2_rebuilt = kernel.RunContext(
        session=session, run=run2, settings=object(), state=proj(session, run2), bus=bus, projector=proj, concurrency=2
    )
    sch2.resume(ctx2_rebuilt, resume_value={"branch_a": "PASS_A", "branch_b": "PASS_B"})
    assert proj(session, run2).status == "COMPLETED"
    assert proj(session, run2).result_json == {"branch_a": "A:PASS_A", "branch_b": "B:PASS_B"}
    assert kernel.verify_chain(session, run2) is True


def test_kernel_bus_fail_closed_check_and_bounded_stream_subscription():
    """验证 Bus 三协议完备性：check 裁决器异常 fail-closed 拦截留痕 + stream 有界队列 drop 丢帧计数与 block 反压。"""
    import threading
    import time

    bus = kernel.Bus()
    rejected_seen: list = []
    bus.subscribe("tool.rejected", lambda e: rejected_seen.append(e))

    # 1) check 裁决器自身抛异常 -> fail-closed 拦截、记入 drain_rejections、广播 tool.rejected
    def buggy_adjudicator(_tool, _args, _payload):
        raise RuntimeError("adjudicator crashed unexpectedly")

    bus.add_adjudicator(buggy_adjudicator)
    reason = bus.check("write_op", {"x": 1}, {})
    assert reason is not None
    assert "fail-closed" in reason
    assert "buggy_adjudicator" in reason
    assert len(rejected_seen) == 1
    drained = bus.drain_rejections()
    assert len(drained) == 1
    assert drained[0]["adjudicator"] == "buggy_adjudicator"

    # 2) stream(overflow="drop") 有界队列防爆 + 精确 dropped 计数 + with 退订防泄漏
    drop_bus = kernel.Bus()
    with drop_bus.stream("token.delta", maxsize=2, overflow="drop") as sub:
        for i in range(5):
            drop_bus.fire("token.delta", {"idx": i})
        assert sub.qsize == 2
        assert sub.dropped == 3
        assert sub.get_nowait()["idx"] == 0
        assert sub.get_nowait()["idx"] == 1
        assert sub.get_nowait() is None
    assert sub.closed is True
    # 关闭后不再接收新事件
    drop_bus.fire("token.delta", {"idx": 99})
    assert sub.qsize == 0

    # 3) stream(overflow="block") 满载反压不丢帧
    block_bus = kernel.Bus()
    received_indices: list = []
    with block_bus.stream("stream.chunk", maxsize=1, overflow="block", block_timeout=1.0) as sub:
        def _consumer():
            time.sleep(0.03)
            for _ in range(3):
                item = sub.get(timeout=1.0)
                if item is not None:
                    received_indices.append(item["idx"])

        t = threading.Thread(target=_consumer)
        t.start()
        for i in range(3):
            block_bus.fire("stream.chunk", {"idx": i})
        t.join(timeout=2.0)
        assert sub.dropped == 0
    assert received_indices == [0, 1, 2]


def test_kernel_deep_audit_fixes_across_all_five_modules():
    """回归：针对 interrupt / bus / event_log / run / scheduler 深度自审发现的全部暗坑。"""
    import pytest

    session = make_session()

    # 1) interrupt.py：identity 不能覆盖 interrupt_id；空 kind 拦截；同波双中断逐个回答不误报冲突
    with pytest.raises(ValueError, match="Interrupt.kind 不能为空"):
        kernel.Interrupt(kind="  ", node="gate", question="q")
    intr_obj = kernel.Interrupt(
        kind="APPROVAL_REQUIRED",
        node="gate",
        question="real_q",
        identity={"operator": "alice", "interrupt_id": "forged", "question": "forged_q"},
    )
    intr_payload = intr_obj.to_payload()
    assert intr_payload["operator"] == "alice"
    assert intr_payload["question"] == "real_q"
    assert intr_payload["interrupt_id"] != "forged"

    run_multi = kernel.create_run(session, "multi-intr-spec", initial_node="start")
    ev1 = kernel.append_event(session, run_multi, "NEED_INPUT", "n1", kernel.Interrupt("NEED_INPUT", "n1", "q1").to_payload())
    ev2 = kernel.append_event(session, run_multi, "NEED_INPUT", "n2", kernel.Interrupt("NEED_INPUT", "n2", "q2").to_payload())
    session.commit()
    st_first, target_first = kernel.answer_interrupt(session, run_multi, "NEED_INPUT", "INPUT_GIVEN", "fp_n2")
    assert st_first == "PENDING" and target_first.seq == ev2.seq
    kernel.record_interrupt_answer(session, run_multi, "INPUT_GIVEN", "n2", {"v": 2}, interrupt=ev2, answer_fingerprint="fp_n2")
    session.commit()
    # 重复提交 fp_n2 -> 幂等重放
    st_rep, _ = kernel.answer_interrupt(session, run_multi, "NEED_INPUT", "INPUT_GIVEN", "fp_n2")
    assert st_rep == "REPLAY"
    # 接着回答同波仍未回答的 ev1（不同指纹 fp_n1）-> 应返回 PENDING + ev1，绝不误判冲突
    st_second, target_second = kernel.answer_interrupt(session, run_multi, "NEED_INPUT", "INPUT_GIVEN", "fp_n1")
    assert st_second == "PENDING" and target_second.seq == ev1.seq

    # 2) bus.py：payload 不能覆盖 seq/topic/timestamp；fire("*") 不双发；三级父子总线逐级冒泡；非字典 table 不崩
    root_bus = kernel.Bus()
    mid_bus = kernel.Bus(parent=root_bus)
    leaf_bus = kernel.Bus(parent=mid_bus)
    root_seen: list = []
    root_bus.subscribe("*", lambda e: root_seen.append(e))
    env = leaf_bus.fire("custom.evt", {"seq": 999, "topic": "fake", "timestamp": 0, "ok": True})
    assert env["seq"] == 1 and env["topic"] == "custom.evt" and env["timestamp"] > 0 and env["ok"] is True
    assert len(root_seen) == 1 and root_seen[0]["topic"] == "custom.evt"

    star_calls = []
    star_bus = kernel.Bus()
    star_bus.subscribe("*", lambda e: star_calls.append(e))
    star_bus.fire("*", {"x": 1})
    assert len(star_calls) == 1
    assert star_bus.adjudicate("tool_x", {}, {"table": "NOT_A_DICT", "fields": "NOT_A_LIST"}) is None

    # 3) event_log.py：调用方事后修改 payload 不破坏哈希链；rollback 清空投影缓存；RUN_RESUMED 恢复 trace.status
    run_ev = kernel.create_run(session, "ev-audit-spec", initial_node="start")
    session.commit()
    mutable = {"k": "original", "nums": (1, 2)}
    kernel.append_event(session, run_ev, "STEP_OK", "s1", mutable)
    mutable["k"] = "tampered_after_append"
    session.commit()
    assert kernel.verify_chain(session, run_ev) is True

    # 回滚后投影缓存自动失效，不会读到已回滚的幽灵状态（显式 reducer 同样命中 session 级缓存并在 rollback 后失效）
    def ev_reducer(state: kernel.RunState, kind: str, payload: dict) -> kernel.RunState:
        if kind == "RUN_CREATED":
            return replace(state, status="RUNNING", current_node="start")
        if kind == "RUN_FAILED":
            return replace(state, status="FAILED", current_node=payload.get("node", state.current_node))
        return state

    proj_ev = lambda s, r: kernel.project_with(s, r, reducer=ev_reducer)
    before_state = proj_ev(session, run_ev)
    assert before_state.status == "RUNNING"
    kernel.append_event(session, run_ev, "RUN_FAILED", "s1", {"node": "s1", "result": {"error": "temp"}})
    assert proj_ev(session, run_ev).status == "FAILED"  # 填充缓存为 FAILED
    session.rollback()
    after_rollback_state = proj_ev(session, run_ev)
    assert after_rollback_state.status == "RUNNING"

    tr = kernel.build_trace(
        [
            {"seq": 1, "kind": "RUN_CREATED", "node": "start", "payload_json": {}},
            {"seq": 2, "kind": "APPROVAL_REQUIRED", "node": "gate", "payload_json": {"interrupt_id": "abc"}},
            {"seq": 3, "kind": "RUN_RESUMED", "node": "gate", "payload_json": {}},
        ],
        run_id="r-trace",
    )
    assert tr.status == "running"

    # 4) run.py：BudgetGuard 重复 attach 幂等不双倍记账；detach 后不再记账
    bg_bus = kernel.Bus()
    guard = kernel.BudgetGuard(run_id="bg-idempotent").attach(bg_bus).attach(bg_bus)
    bg_bus.fire("tool.after", {"tool": "t", "attempts": 1, "usage": {"total_tokens": 50}})
    assert guard.snapshot()["llm_calls"] == 1
    assert guard.snapshot()["total_tokens"] == 50
    guard.detach()
    bg_bus.fire("tool.after", {"tool": "t", "attempts": 1, "usage": {"total_tokens": 50}})
    assert guard.snapshot()["llm_calls"] == 1
    assert bg_bus.budget_guard is None

    # 5) scheduler.py：max_steps < 1 报错；终态不被残留 goto 复活
    def term_reducer(state: kernel.RunState, kind: str, payload: dict) -> kernel.RunState:
        if kind == "RUN_CREATED":
            return replace(state, status="RUNNING", current_node="start", when="always")
        if kind == "DONE":
            return replace(state, status="COMPLETED", current_node="finish", result_json=payload)
        if kind == "GO_REDIRECT":
            return replace(state, status="RUNNING", current_node=payload["target"], pending_target=payload["target"])
        return state

    proj_t = lambda s, r: kernel.project_with(s, r, reducer=term_reducer)
    term_plan = kernel.WorkflowPlan(
        name="terminal_guard",
        nodes=(
            kernel.PlanNode("start", "起点", "stage"),
            kernel.PlanNode("work", "干活", "stage"),
            kernel.PlanNode("finish", "终点", "terminal"),
        ),
        edges=(
            kernel.PlanEdge("start", "work", "always"),
            kernel.PlanEdge("work", "finish", "always"),
        ),
        entry="start",
    )
    run_t = kernel.create_run(session, "term-spec", initial_node="start")
    ctx_t = kernel.RunContext(session=session, run=run_t, settings=object(), state=proj_t(session, run_t), projector=proj_t)
    sch_t = kernel.Scheduler(
        term_plan,
        {"work": lambda _i, _c: kernel.Outcome(events=[("DONE", "finish", {"ok": True})], goto="work")},
    )
    with pytest.raises(ValueError, match="max_steps"):
        sch_t.drive(ctx_t, max_steps=0)
    steps_t = sch_t.drive(ctx_t)
    assert len(steps_t) == 1
    assert proj_t(session, run_t).status == "COMPLETED"


def test_policy_structure_clean_three_modules_and_infra_llm_guard():
    """验证 policy 重构红线：
    1. app/policy/ 严格收敛为三大核心策略模块（regops_plan / regops_bus / regops_channels + __init__），零 import 期全局污染；
    2. llm_guard 归位 app/infra/（Layer 0，零反向依赖 kernel/policy）；
    3. validator 内部 TypeError 代码缺陷快速失败不空耗重试，model_no_tool_call 走校验配额不触发网络退避休眠。
    """
    import time
    import pytest
    from app.infra.llm.guard import call_llm_validated

    # 1) policy 目录收敛为精准 3 策略模块 + __init__.py，且 __all__ 全部可解析
    policy_dir = Path(policy.__file__).resolve().parent
    actual_policy_files = {p.name for p in policy_dir.glob("*.py")}
    assert actual_policy_files == {
        "__init__.py",
        "regops_plan.py",
        "regops_bus.py",
        "regops_channels.py",
    }
    for name in policy.__all__:
        assert hasattr(policy, name), f"policy.__all__ 缺少导出符号：{name}"

    # 2) 内核 global_bus() 未被 policy import 污染任何监管业务门禁
    assert kernel.global_bus()._adjudicators == []

    # 3) validator 内部 TypeError 属于程序 Bug，应立即抛出而非被吞进 LLM 重试循环
    calls = {"n": 0}

    class OkLlm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            calls["n"] += 1
            return {"rules": []}, {"provider": "test"}

    def buggy_validator(_args):
        raise TypeError("unexpected internal bug")

    with pytest.raises(TypeError, match="unexpected internal bug"):
        call_llm_validated(OkLlm(), "assign_rules", "instr", {}, buggy_validator)
    assert calls["n"] == 1

    # 4) model_no_tool_call 走 validation_attempts 配额，不触发长 backoff sleep
    no_tool_calls = {"n": 0}

    class NoToolLlm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            no_tool_calls["n"] += 1
            raise RuntimeError("Model returned no tool call")

    t0 = time.monotonic()
    with pytest.raises(Exception) as exc_info:
        call_llm_validated(
            NoToolLlm(),
            "assign_rules",
            "instr",
            {},
            backoff=(5.0, 5.0),
            transient_attempts=4,
            validation_attempts=2,
        )
    assert time.monotonic() - t0 < 1.0
    assert no_tool_calls["n"] == 2
    assert getattr(exc_info.value, "context", {}).get("error_kind") == "model_no_tool_call"


def test_infra_clean_structure_mysql_pool_and_blob_concurrency(tmp_path):
    """验证 infra 重构红线与生产加固：
    1. app/infra/ 严格收敛为 10 个高内聚模块 + __init__.py（无碎片文件、models 统一收口、llm 与 llm_schemas 清晰解耦）；
    2. db._build_engine_kwargs 为真实 MySQL/Oracle 配齐 pool_recycle/pool_size/max_overflow/pool_timeout；
    3. LocalBlobStore 多线程并发写同一内容零竞态（唯一临时文件 + 原子 os.replace）且严校验 blob:// 前缀。
    """
    from concurrent.futures import ThreadPoolExecutor
    import pytest
    import app.infra as infra_pkg
    from app.infra.blob_store import LocalBlobStore
    from app.infra.db import _build_engine_kwargs

    infra_dir = Path(infra_pkg.__file__).resolve().parent
    actual_infra_entries = {p.name for p in infra_dir.iterdir() if p.name != "__pycache__"}
    assert actual_infra_entries == {
        "__init__.py",
        "blob_store.py",
        "config.py",
        "contracts.py",
        "db.py",
        "db_tables.py",
        "errors.py",
        "llm",
        "task_runner.py",
    }
    actual_llm_files = {p.name for p in (infra_dir / "llm").glob("*.py")}
    assert actual_llm_files == {"__init__.py", "client.py", "guard.py", "schemas.py"}

    mysql_kwargs = _build_engine_kwargs("mysql+pymysql://user:pw@127.0.0.1:3306/regops")
    assert mysql_kwargs["pool_pre_ping"] is True
    assert mysql_kwargs["pool_recycle"] == 1800
    assert mysql_kwargs["pool_size"] >= 10
    assert mysql_kwargs["max_overflow"] >= 10

    store = LocalBlobStore(str(tmp_path / "blobs"))
    payload = b"concurrent-regops-blob-payload" * 100
    with ThreadPoolExecutor(max_workers=12) as pool:
        uris = list(pool.map(lambda _: store.put(payload), range(24)))
    assert len(set(uris)) == 1
    assert store.get(uris[0]) == payload
    with pytest.raises(ValueError, match="Invalid blob URI"):
        store.get(uris[0].removeprefix("blob://"))

    # 4) MySQL 8 表列宽与 LONGTEXT 变体、quote_identifier 大小写方言安全、get_budget 忽略 None 脏配置
    from sqlalchemy.dialects import mysql
    from sqlalchemy.schema import CreateTable
    from app.infra.config import DEFAULT_BUDGET, get_budget
    from app.infra.db import quote_identifier
    from app.infra.db_tables import RawBlockModel, RuleModel, SpecificationModel, SystemSettingModel

    mysql_dialect = mysql.dialect()
    raw_block_ddl = str(CreateTable(RawBlockModel.__table__).compile(dialect=mysql_dialect))
    assert "LONGTEXT" in raw_block_ddl.upper()
    spec_ddl = str(CreateTable(SpecificationModel.__table__).compile(dialect=mysql_dialect))
    assert "VARCHAR(96)" in spec_ddl.upper()
    rule_ddl = str(CreateTable(RuleModel.__table__).compile(dialect=mysql_dialect))
    assert "VARCHAR(192)" in rule_ddl.upper()

    assert quote_identifier("RPTNO", "ORACLE") == '"RPTNO"'
    assert quote_identifier("RPTNO", "oracle+oracledb") == '"RPTNO"'
    assert quote_identifier("RPTNO", "MYSQL") == "`RPTNO`"
    with pytest.raises(ValueError, match="Unsupported SQL dialect"):
        quote_identifier("RPTNO", "postgres")
    with pytest.raises(ValueError, match="Unsafe SQL identifier"):
        quote_identifier(123, "oracle")  # type: ignore[arg-type]

    s = make_session()
    s.add(SystemSettingModel(key="execution_budget", value_json={"max_llm_concurrency": None, "max_retries": 5}))
    s.commit()
    budget = get_budget(s)
    assert budget["max_llm_concurrency"] == DEFAULT_BUDGET["max_llm_concurrency"]
    assert budget["max_retries"] == 5












