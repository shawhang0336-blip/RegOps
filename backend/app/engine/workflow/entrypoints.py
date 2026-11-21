"""阶段二工作流与 RegOps Run 生命周期唯一业务入口（Workflow Entrypoints）。

基于 Layer 1 微内核 `app.kernel.run`（机制在内），装配 RegOps 专属业务策略（策略在外）：
- `_create_run` / `start_generic_run` / `submit_generic_run`：统一创建与后台推进（带取消令牌与防重入）；
- `drive` / `continue_generic_run`：组装 `RunContext` 与前后段节点体，交由唯一 `Scheduler` 推进；
- `approve_generic_rules` / `confirm_generic_test_results`：基于 `kernel.run.answer_interrupt` 的幂等人工门；
- `retry_generic_run` / `reaudit_generic_run`：断点续跑、自愈重绑与人工改参轻量重审；
- `cancel_generic_run` / `recover_interrupted_generic_runs` / `active_generic_runs`：运行时取消与重启恢复；
- `delete_generic_run` / `history_cleanup_plan` / `cleanup_history_runs` / `replay_generic_run`：
  带 6 表 + BlobStore + OracleSandbox 清理钩子的统一删除、历史整理与哈希链回放。
"""

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.engine.oracle import OracleSandbox
from app.engine.workflow.nodes import _back_bodies, _front_bodies
from app.engine.workflow.runtime import _error_details, _pending_rules, _resolve_budget, _specs
from app.infra.asset_models import (
    ApprovalModel,
    ArtifactModel,
    AuditCaseDefinitionModel,
    GenerationPlanModel,
    GeneratorDefinitionModel,
    SchemaDefinitionModel,
)
from app.infra.config import Settings, get_settings
from app.infra.db import SessionLocal
from app.infra.domain import canonical_json
from app.infra.errors import WorkflowError
from app.infra.llm import RealDesignLlm
from app.infra.models import EventModel, RuleModel, RunModel, SpecificationModel
from app.kernel.blob_store import LocalBlobStore
from app.kernel.bus import global_bus
from app.kernel.event_log import append_event
from app.kernel.interrupt import answer_interrupt, record_interrupt_answer
from app.kernel.run import (
    CancellationToken,
    RunCancelledError,
    cancel_run as kernel_cancel_run,
    create_run as kernel_create_run,
    delete_run as kernel_delete_run,
    fail_run as kernel_fail_run,
    record_run_cancelled as kernel_record_run_cancelled,
    recover_interrupted_runs as kernel_recover_interrupted_runs,
    replay_run as kernel_replay_run,
    run_registry,
    submit_run as kernel_submit_run,
)
from app.kernel.scheduler import Scheduler
from app.policy.regops_channels import build_context
from app.policy.regops_plan import (
    ACTIVE_RUN_STATUSES,
    DEFAULT_PLAN,
    INTERRUPT_ANSWER_KINDS,
    project,
    project_many,
    self_heal_target,
)

_REGOPS_RUN_TABLES = (
    RuleModel,
    ArtifactModel,
    GenerationPlanModel,
    GeneratorDefinitionModel,
    SchemaDefinitionModel,
    AuditCaseDefinitionModel,
)


_STAGE_NODE_ALIAS = {
    "rule_table_worker": "rule_binder",
    "rule_join": "rule_binder",
    "data_table_worker": "data_generator_binder",
    "data_join": "data_generator_binder",
    "audit_table_worker": "audit_case_binder",
    "audit_join": "audit_case_binder",
}


def _display_stage_node(node: str) -> str:
    return _STAGE_NODE_ALIAS.get(node, node)


def _create_run(session: Session, spec_id: str, settings: Settings, operator: str = "UNSPECIFIED_OPERATOR") -> RunModel:
    _specs(session, spec_id)  # 校验 spec 存在且已确认；非法则抛 WorkflowError
    specification = session.get(SpecificationModel, spec_id)
    return kernel_create_run(
        session,
        spec_id,
        initial_node="table_confirmation",
        initial_payload={
            "provider": settings.llm_provider,
            "confirmation": specification.content_json.get("confirmation") if specification else None,
            "operator": operator,
        },
    )


def drive(
    session: Session,
    run: RunModel,
    settings: Settings,
    llm=None,
    generation_budget=None,
    attempt_base=None,
    cancellation: Optional[CancellationToken] = None,
) -> RunModel:
    """唯一执行入口：一个 Scheduler 按 Plan 驱动全流程。

    前后段节点全部注册到同一张 Plan，节点之间不直接调用；
    跨节点上下文由 build_context 从 EventLog + BlobStore 自动折叠恢复；
    人工门由一等 Interrupt 挂起，自愈由 Outcome.goto 交回 Scheduler 重新调度。
    """
    state = project(session, run)
    if state.status in {"COMPLETED", "FAILED", "WAITING_APPROVAL", "WAITING_TEST_APPROVAL"}:
        return run

    specs = _specs(session, run.spec_id)
    llm = llm or RealDesignLlm(settings)
    resolved_budget, concurrency = _resolve_budget(session, generation_budget)
    ctx = build_context(
        session,
        run,
        settings,
        llm=llm,
        budget=resolved_budget,
        concurrency=concurrency,
        attempt_base=attempt_base,
        cancellation=cancellation,
    )
    if generation_budget is not None or ctx.get("use_plan"):
        ctx.put("use_plan", True)

    bodies = {}
    bodies.update(_front_bodies(specs, llm, resolved_budget, concurrency, settings))
    bodies.update(_back_bodies(specs, settings))
    scheduler = Scheduler(DEFAULT_PLAN, bodies)

    try:
        scheduler.drive(ctx)
    except RunCancelledError as exc:
        raw_node = ctx.current_node or ctx.state.current_node or "created"
        cancelled_node = _display_stage_node(raw_node)
        budget_snap = ctx.budget_guard.snapshot() if ctx.budget_guard is not None else None
        kernel_record_run_cancelled(
            session,
            run,
            node=cancelled_node,
            reason=exc.reason,
            resume_from_node=ctx.state.resume_from_node,
            resume_when=ctx.state.resume_when,
            extra_result={"budget_usage": budget_snap} if budget_snap is not None else None,
            bus=ctx.bus,
            commit=True,
        )
    except Exception as exc:
        raw_node = ctx.current_node or ctx.state.current_node or "created"
        failed_node = _display_stage_node(raw_node)
        budget_snap = ctx.budget_guard.snapshot() if ctx.budget_guard is not None else None
        details = _error_details(exc, failed_node)
        if raw_node != failed_node:
            details["worker_node"] = raw_node
        kernel_fail_run(
            session,
            run,
            node=failed_node,
            error=f"{type(exc).__name__}: {exc}",
            error_details=details,
            resume_from_node=ctx.state.resume_from_node,
            resume_when=ctx.state.resume_when,
            extra_result={"budget_usage": budget_snap} if budget_snap is not None else None,
            bus=ctx.bus,
            commit=True,
        )
    return run


def start_generic_run(session: Session, spec_id: str, settings: Settings, llm=None, generation_budget=None) -> RunModel:
    """同步入口：创建 run 并执行到第一个 gate。测试与内部调用使用。"""
    run = _create_run(session, spec_id, settings)
    return drive(session, run, settings, llm, generation_budget)


def submit_generic_run(
    run_id: str,
    settings: Optional[Settings] = None,
    generation_budget=None,
    allowed_statuses: Iterable[str] = ("RUNNING", "READY_TO_AUDIT"),
    *,
    runner=None,
    registry=None,
) -> bool:
    """统一后台推进入口：委托 `kernel.run.submit_run`，按 `run:<id>` 防重入并挂载取消令牌。"""
    effective_settings = settings if settings is not None else get_settings()
    return kernel_submit_run(
        run_id,
        SessionLocal,
        lambda session, run, token: drive(
            session,
            run,
            effective_settings,
            generation_budget=generation_budget,
            cancellation=token,
        ),
        allowed_statuses=allowed_statuses,
        projector=project,
        error_details_fn=_error_details,
        runner=runner,
        registry=registry,
    )


def approve_generic_rules(
    session: Session,
    run: RunModel,
    approver: str,
    decisions: Dict[str, str],
    settings: Settings,
    llm=None,
    defer_continuation: bool = False,
) -> RunModel:
    normalized_input = {rule_hash: decision for rule_hash, decision in decisions.items()}
    fingerprint = hashlib.sha256(canonical_json(normalized_input).encode()).hexdigest()
    try:
        status, interrupt = answer_interrupt(
            session,
            run,
            "APPROVAL_REQUIRED",
            "HUMAN_DECISIONS_RECORDED",
            fingerprint,
            answer_kinds=INTERRUPT_ANSWER_KINDS,
            reject_conflict=True,
        )
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    if status == "REPLAY":
        return run
    if project(session, run).status != "WAITING_APPROVAL":
        raise WorkflowError("Run is not waiting for rule approval")

    pending = {rule.rule_hash: rule for rule in _pending_rules(session, run.id)}
    if not decisions or any(rule_hash not in pending for rule_hash in decisions):
        raise WorkflowError("Decisions must target currently pending rules")
    normalized = []
    for rule_hash, decision in decisions.items():
        if decision not in {"APPROVED", "REJECTED"}:
            raise WorkflowError("Decision must be APPROVED or REJECTED")
        pending[rule_hash].status = decision
        normalized.append({"rule_hash": rule_hash, "decision": decision})
        session.add(
            ApprovalModel(
                id=str(uuid.uuid4()),
                object_type="RULE",
                object_hash=rule_hash,
                decision=decision,
                approver=approver,
                comment="",
            )
        )
    session.flush()
    all_decided = not _pending_rules(session, run.id)
    record_interrupt_answer(
        session,
        run,
        "HUMAN_DECISIONS_RECORDED",
        "human_gate",
        {"approver": approver, "decisions": normalized, "all_decided": all_decided},
        interrupt=interrupt,
        answer_fingerprint=fingerprint,
        attach_interrupt=all_decided,
    )
    session.commit()
    if not all_decided or defer_continuation:
        return run
    return continue_generic_run(session, run, settings, llm)


def confirm_generic_test_results(session: Session, run: RunModel, approver: str) -> RunModel:
    fingerprint = hashlib.sha256(canonical_json({"approver": approver}).encode()).hexdigest()
    status, interrupt = answer_interrupt(
        session,
        run,
        "TEST_CONFIRMATION_REQUIRED",
        "TEST_RESULTS_CONFIRMED",
        fingerprint,
        answer_kinds=INTERRUPT_ANSWER_KINDS,
        reject_conflict=False,
    )
    if status == "REPLAY":
        return run
    if project(session, run).status != "WAITING_TEST_APPROVAL":
        raise WorkflowError("Run is not waiting for test result confirmation")
    summary = (project(session, run).result_json or {}).get("audit_summary", {})
    if not summary.get("all_passed"):
        raise WorkflowError("Test results can only be confirmed after all Oracle checks pass")
    if summary.get("coverage_complete") is False:
        raise WorkflowError("Test results cannot be confirmed while validation rules remain uncovered")
    report_uri = (project(session, run).result_json or {}).get("audit_report_blob", "")
    object_hash = hashlib.sha256(f"{run.id}:{report_uri}".encode()).hexdigest()
    session.add(
        ApprovalModel(
            id=str(uuid.uuid4()),
            object_type="TEST_RESULTS",
            object_hash=object_hash,
            decision="APPROVED",
            approver=approver,
            comment="",
        )
    )
    record_interrupt_answer(
        session,
        run,
        "TEST_RESULTS_CONFIRMED",
        "test_confirmation",
        {"approver": approver, "object_hash": object_hash},
        interrupt=interrupt,
        answer_fingerprint=fingerprint,
    )
    global_bus().fire("run.completed", {"run_id": run.id, "node": "export", "status": "COMPLETED"})
    session.commit()
    return run


def retry_generic_run(
    session: Session,
    run: RunModel,
    settings: Settings,
    defer_continuation: bool = False,
    llm=None,
) -> RunModel:
    """统一重试与断点恢复入口：
    1. 若存在失败的审计报告（AUDIT_REJECTED）：触发 GO_REDIRECT 回到 rule_binder，由 Scheduler 驱动靶向重绑与重验；
    2. 若在审计前因超时/网络/取消/进程重启中断（无审计报告）：写 RUN_RESUMED 事件，从 EventLog 最后一个已完成节点断点续跑。
    """
    state = project(session, run)
    report_uri = (state.result_json or {}).get("audit_report_blob")
    if not report_uri:
        redirect = (
            session.query(EventModel)
            .filter(EventModel.run_id == run.id, EventModel.kind == "GO_REDIRECT")
            .order_by(EventModel.seq.desc())
            .first()
        )
        report_uri = ((redirect.payload_json.get("failed_result") or {}).get("audit_report_blob") if redirect else None)

    if not report_uri:
        append_event(
            session,
            run,
            "RUN_RESUMED",
            "human_retry",
            {
                "resume_from_node": state.resume_from_node,
                "resume_when": state.resume_when,
                "pending_target": state.pending_target,
                "failed_node": state.current_node,
                "reason": "Resume interrupted run from the last completed EventLog checkpoint",
            },
        )
        session.commit()
        if defer_continuation:
            return run
        return drive(session, run, settings, llm)

    _specs(session, run.spec_id)
    report = json.loads(LocalBlobStore(settings.blob_dir).get(report_uri).decode("utf-8"))
    failed_keys = sorted(
        {rule_key for case in report.get("cases", []) for rule_key in case.get("missing_rules", [])}
    )
    revisable = (
        session.query(RuleModel)
        .filter(RuleModel.run_id == run.id, RuleModel.rule_key.in_(failed_keys), RuleModel.status == "APPROVED")
        .count()
        if failed_keys
        else 0
    )
    if not revisable:
        raise WorkflowError("No failed active rules could be revised")

    append_event(
        session,
        run,
        "GO_REDIRECT",
        "human_retry",
        {
            "target": self_heal_target(),
            "reason": "Repair rules from the persisted failed audit before regenerating data",
            "origin": "human_retry",
            "failed_result": {"audit_report_blob": report_uri},
        },
    )
    session.commit()
    if defer_continuation:
        return run
    return drive(session, run, settings, llm, attempt_base=state.attempt + 1)


def reaudit_generic_run(
    session: Session,
    run: RunModel,
    settings: Optional[Settings] = None,
    *,
    defer_continuation: bool = False,
) -> RunModel:
    """人工改参后的轻量重审：保留规则集合不动，仅重跑造数与 Oracle 沙箱验证。"""
    if project(session, run).status in ACTIVE_RUN_STATUSES or run_registry().is_active(run.id):
        raise WorkflowError("批次正在执行中，请等当前验证结束后再发起重审")
    pending = _pending_rules(session, run.id)
    if pending:
        raise WorkflowError(f"还有 {len(pending)} 条规则待确认，请先在第二步处理完再重审")
    approved = (
        session.query(RuleModel)
        .filter(RuleModel.run_id == run.id, RuleModel.status == "APPROVED")
        .count()
    )
    if not approved:
        raise WorkflowError("这个批次没有已批准的规则可验证")
    for required_kind in ("SCHEMA_ASSIGNED", "CROSS_RULES_ASSIGNED"):
        exists = (
            session.query(EventModel)
            .filter(EventModel.run_id == run.id, EventModel.kind == required_kind)
            .first()
        )
        if not exists:
            raise WorkflowError("该批次缺少生成上下文（未完成过规则绑定），请改用“重新生成并验证”")
    append_event(
        session,
        run,
        "REAUDIT_REQUESTED",
        "build_audit",
        {"reason": "保留规则与人工修改，仅重跑 AI 造数与 Oracle 沙箱验证"},
    )
    session.commit()
    if not defer_continuation:
        submit_generic_run(run.id, settings or get_settings(), allowed_statuses=("READY_TO_AUDIT", "RUNNING"))
    return run


def cancel_generic_run(
    session: Session,
    run: RunModel,
    reason: str = "Run cancelled by operator",
) -> Dict[str, Any]:
    """取消正在运行中的 RegOps Run（活跃线程投递取消令牌，挂起态直接落账 RUN_CANCELLED）。"""
    try:
        return kernel_cancel_run(
            session,
            run,
            active_statuses=ACTIVE_RUN_STATUSES,
            reason=reason,
            projector=project,
        )
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc


def recover_interrupted_generic_runs(session: Optional[Session] = None) -> List[str]:
    """进程重启中断恢复入口：仅回收 Step 2 规则验证 Run（关联 SpecificationModel）。"""
    def _run(db: Session) -> List[str]:
        gen_runs = (
            db.query(RunModel)
            .join(SpecificationModel, RunModel.spec_id == SpecificationModel.id)
            .all()
        )
        return kernel_recover_interrupted_runs(
            db, ACTIVE_RUN_STATUSES, runs=gen_runs, projector_many=project_many
        )

    if session is not None:
        return _run(session)
    with SessionLocal() as own_session:
        return _run(own_session)


def active_generic_runs() -> List[str]:
    """返回当前进程内正在执行的 Run ID 列表。"""
    return run_registry().active_run_ids()


def replay_generic_run(session: Session, run: RunModel) -> Dict[str, Any]:
    """统一回放与哈希链验签入口。"""
    return kernel_replay_run(session, run, projector=project)


# ── RegOps 删除与历史清理（基于 kernel.run.delete_run 钩子框架）────────────────


def collect_run_oracle_tables(session: Session, run: RunModel) -> set:
    """从投影结果与全量历史事件中收集该批次历次 attempt 建过的所有 Oracle 沙箱表（含失败/自愈/重审批次）。"""
    oracle_tables = set()

    def _add_from_mapping(container) -> None:
        if not isinstance(container, dict):
            return
        for key in (
            "oracle_tables",
            "oracle_target_tables",
            "oracle_code_set_tables",
            "physical_tables",
            "target_tables",
            "code_set_tables",
        ):
            mapping = container.get(key)
            if isinstance(mapping, dict):
                oracle_tables.update(str(v) for v in mapping.values() if v)

    _add_from_mapping(project(session, run).result_json)
    for event in session.query(EventModel).filter(EventModel.run_id == run.id).all():
        payload = event.payload_json or {}
        if not isinstance(payload, dict):
            continue
        _add_from_mapping(payload)
        for nested_key in ("result", "failed_result", "assets"):
            _add_from_mapping(payload.get(nested_key))
    return oracle_tables


def _regops_delete_pre_hook(session: Session, run: RunModel, context: Dict[str, Any]) -> None:
    context["oracle_tables"] = collect_run_oracle_tables(session, run)
    context["blob_uris"] = {
        row.blob_uri for row in session.query(ArtifactModel).filter(ArtifactModel.run_id == run.id).all()
    }
    for model in _REGOPS_RUN_TABLES:
        session.query(model).filter(model.run_id == run.id).delete(synchronize_session=False)


def _cleanup_orphaned_resources(
    session: Session,
    blob_uris: set,
    oracle_tables: set,
    run_ids: List[str],
    settings: Settings,
) -> List[str]:
    remaining_uris = {row[0] for row in session.query(ArtifactModel.blob_uri).distinct().all()}
    store = LocalBlobStore(settings.blob_dir)
    for uri in blob_uris - remaining_uris:
        try:
            store.delete(uri)
        except ValueError:
            pass
    if (not oracle_tables and not run_ids) or not settings.oracle_sandbox_url:
        return []
    try:
        return OracleSandbox(settings.oracle_sandbox_url).drop_tables(sorted(oracle_tables), run_ids=run_ids)
    except Exception as exc:
        return [f"沙箱表清理失败：{type(exc).__name__}"]


def delete_generic_run(session: Session, run: RunModel, settings: Optional[Settings] = None) -> Dict[str, Any]:
    """统一删除单个 Run：经 `kernel.run.delete_run` 校验运行态 + 删 6 张业务表/事件 + 清理 Blob 与 Oracle 沙箱表。"""
    effective_settings = settings or get_settings()
    run_id = run.id
    warnings: List[str] = []

    def _post_hook(s: Session, ctx: Dict[str, Any]) -> None:
        warnings.extend(
            _cleanup_orphaned_resources(
                s,
                ctx.get("blob_uris", set()),
                ctx.get("oracle_tables", set()),
                [run_id],
                effective_settings,
            )
        )

    try:
        kernel_delete_run(
            session,
            run,
            active_statuses=ACTIVE_RUN_STATUSES,
            projector=project,
            pre_delete_hooks=(_regops_delete_pre_hook,),
            post_commit_hooks=(_post_hook,),
            commit=True,
        )
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    return {"deleted_id": run_id, "sandbox_cleanup": warnings}


def _spec_table_codes(session: Session, run: RunModel) -> Tuple[str, ...]:
    spec = session.get(SpecificationModel, run.spec_id)
    content = (spec.content_json or {}) if spec else {}
    tables = content.get("tables") or ([content] if content else [])
    codes = tuple(sorted(item["table_code"] for item in tables if isinstance(item, dict) and item.get("table_code")))
    return codes or (run.spec_id,)


def history_cleanup_plan(session: Session):
    """每组报表保留最新批次和最近一次通过验证的批次，运行中批次永不清理（仅限 Step 2 验证批次）。"""
    runs = (
        session.query(RunModel)
        .join(SpecificationModel, RunModel.spec_id == SpecificationModel.id)
        .order_by(RunModel.created_at.desc())
        .all()
    )
    states = project_many(session, runs)
    groups: Dict[tuple, List[RunModel]] = {}
    for run in runs:
        groups.setdefault(_spec_table_codes(session, run), []).append(run)
    keep = set()
    active_ids = set(run_registry().active_run_ids())
    for group in groups.values():
        ordered = sorted(group, key=lambda item: item.created_at or datetime.min, reverse=True)
        keep.add(ordered[0].id)
        completed = next((item for item in ordered if states[item.id].status == "COMPLETED"), None)
        if completed:
            keep.add(completed.id)
        keep.update(
            item.id for item in ordered if states[item.id].status in ACTIVE_RUN_STATUSES or item.id in active_ids
        )
    deletes = [run for run in runs if run.id not in keep]
    return runs, deletes, states


def cleanup_history_runs(session: Session, settings: Optional[Settings] = None) -> Dict[str, Any]:
    """批量清理历史冗余批次（共享事务内删除行 + 提交后统一清理 Blob 与 Oracle 沙箱表）。"""
    effective_settings = settings or get_settings()
    _, deletes, _ = history_cleanup_plan(session)
    deleted_ids = [run.id for run in deletes]
    blob_uris: set = set()
    oracle_tables: set = set()
    for run in deletes:
        ctx = kernel_delete_run(
            session,
            run,
            active_statuses=ACTIVE_RUN_STATUSES,
            projector=project,
            pre_delete_hooks=(_regops_delete_pre_hook,),
            commit=False,
        )
        blob_uris.update(ctx.get("blob_uris", set()))
        oracle_tables.update(ctx.get("oracle_tables", set()))
    session.commit()
    warnings = _cleanup_orphaned_resources(session, blob_uris, oracle_tables, deleted_ids, effective_settings)
    return {"deleted_count": len(deleted_ids), "deleted_ids": deleted_ids, "sandbox_cleanup": warnings}


def continue_generic_run(
    session: Session,
    run: RunModel,
    settings: Settings,
    llm=None,
    generation_budget=None,
    attempt_base: Optional[int] = None,
) -> RunModel:
    """兼容入口：统一走唯一 Scheduler（drive）。"""
    return drive(session, run, settings, llm, generation_budget, attempt_base)
