"""ProdAgent 架构分层边界测试：验证「机制在内（app.kernel），策略在外（app.policy）」。

核心断言：
1. `app/kernel/*.py` 通过 AST 静态依赖检查，零引用任何监管报送领域模块或策略层模块；
2. 仅依赖 `app.kernel` 即可独立定义并驱动一个与监管业务完全无关的通用工作流；
3. `app.policy` 将 RegOps 拓扑、五大门禁、16 个数据通道与规则回填策略插拔到内核上，
   且扁平门面模块（`app.blobs` / `app.bus` / `app.workflow` / `app.context` / `app.events` / `app.scheduler`）保持 100% 引用兼容。
"""

import ast
from pathlib import Path
from dataclasses import replace

import app.blobs as facade_blobs
import app.bus as facade_bus
import app.context as facade_context
import app.events as facade_events
import app.kernel as kernel
import app.policy as policy
import app.scheduler as facade_scheduler
import app.workflow as facade_workflow


FORBIDDEN_DOMAIN_MODULES = {
    "domain",
    "generic_domain",
    "rules",
    "asset_models",
    "spec_parser",
    "generic_oracle",
    "compiler",
    "code_set_oracle",
    "code_service",
    "policy",
    "generic_workflow",
    "structure_service",
    "production_service",
    "generic_ai",
}


def test_kernel_has_zero_domain_or_policy_imports():
    """AST 静态检查：app/kernel/*.py 严禁 import 任何监管业务领域或策略模块。"""
    kernel_dir = Path(kernel.__file__).resolve().parent
    py_files = sorted(kernel_dir.glob("*.py"))
    assert len(py_files) >= 6

    violations = []
    for file_path in py_files:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = set(alias.name.split("."))
                    hit = parts & FORBIDDEN_DOMAIN_MODULES
                    if hit:
                        violations.append(f"{file_path.name}: import {alias.name} (命中禁用模块 {sorted(hit)})")
            elif isinstance(node, ast.ImportFrom) and node.module:
                parts = set(node.module.split("."))
                hit = parts & FORBIDDEN_DOMAIN_MODULES
                if hit:
                    violations.append(f"{file_path.name}: from {node.module} import ... (命中禁用模块 {sorted(hit)})")

    assert violations == [], "ProdAgent 微内核存在越界依赖：\n" + "\n".join(violations)


def test_kernel_can_drive_non_regops_workflow_independently():
    """证明 app.kernel 不依赖任何监管业务代码即可独立编排、门禁拦截、合并 Channel 并完成状态折叠。"""
    from .helpers import make_session
    from app.models import RunModel
    import uuid

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
        # 触发一次通用门禁拦截，再写入 append_list 通道
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


def test_facade_modules_preserve_exact_identity_with_kernel_and_policy():
    """扁平门面模块必须与 kernel / policy 导出完全相同的对象引用，保证既有导入 100% 兼容。"""
    assert facade_blobs.LocalBlobStore is kernel.LocalBlobStore
    assert facade_bus.Bus is kernel.Bus
    assert facade_bus.global_bus is kernel.global_bus
    assert facade_bus.default_bus is policy.default_bus
    assert facade_bus.field_whitelist_adjudicator is policy.field_whitelist_adjudicator
    assert facade_workflow.WorkflowPlan is kernel.WorkflowPlan
    assert facade_workflow.DEFAULT_PLAN is policy.DEFAULT_PLAN
    assert facade_context.RunContext is kernel.RunContext
    assert facade_context.ChannelSpec is kernel.ChannelSpec
    assert facade_context.DEFAULT_CHANNELS is policy.DEFAULT_CHANNELS
    assert facade_context.build_context is policy.build_context
    assert facade_events.RunState is kernel.RunState
    assert facade_events.append_event is kernel.append_event
    assert facade_events.project is policy.project
    assert facade_scheduler.Scheduler is kernel.Scheduler
    assert facade_scheduler.Outcome is kernel.Outcome
