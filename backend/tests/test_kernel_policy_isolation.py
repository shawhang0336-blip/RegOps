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


def test_kernel_has_zero_domain_or_policy_imports():
    """AST 静态检查：app/kernel/*.py 严禁 import 任何业务层或策略层模块。"""
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
