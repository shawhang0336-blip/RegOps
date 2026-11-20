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
