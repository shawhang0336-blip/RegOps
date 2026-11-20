"""阶段二工作流对外唯一业务入口（Workflow Entrypoints）。

对外暴露的完整操作闭环：
- `_create_run` / `start_generic_run`：创建 Run 并驱动到第一个人工门或终态；
- `drive` / `continue_generic_run`：组装 `RunContext` 与前后段节点体，交由唯一 `Scheduler` 推进；
- `approve_generic_rules`：规则人工确权门（含幂等重入保护）；
- `confirm_generic_test_results`：测试结果确认门（含覆盖度门禁与幂等保护）；
- `retry_generic_run`：统一重试（存在失败审计报告时自愈靶向重绑，否则从 EventLog 最近检查点断点续跑）。
"""

import hashlib
import json
import uuid
from typing import Dict, Optional

from sqlalchemy.orm import Session

from app.infra.domain import canonical_json
from app.infra.llm import RealDesignLlm
from app.engine.workflow.nodes import _back_bodies, _front_bodies
from app.engine.workflow.runtime import _error_details, _pending_rules, _resolve_budget, _specs
from app.infra.asset_models import ApprovalModel
from app.infra.config import Settings
from app.infra.errors import WorkflowError
from app.infra.models import EventModel, RuleModel, RunModel, SpecificationModel
from app.kernel.blob_store import LocalBlobStore
from app.kernel.bus import global_bus
from app.kernel.event_log import append_event, latest_interrupt, matching_answer
from app.kernel.scheduler import Scheduler
from app.policy.regops_channels import build_context
from app.policy.regops_plan import DEFAULT_PLAN, pending_interrupt, project, self_heal_target


def _create_run(session: Session, spec_id: str, settings: Settings, operator: str = "UNSPECIFIED_OPERATOR") -> RunModel:
    _specs(session, spec_id)  # 校验 spec 存在且已确认；非法则抛 WorkflowError
    run = RunModel(id=str(uuid.uuid4()), spec_id=spec_id)
    session.add(run)
    session.flush()
    specification = session.get(SpecificationModel, spec_id)
    append_event(
        session,
        run,
        "RUN_CREATED",
        "table_confirmation",
        {
            "spec_id": spec_id,
            "provider": "REAL_GLM",
            "confirmation": specification.content_json.get("confirmation"),
            "operator": operator,
        },
    )
    return run


def drive(session: Session, run: RunModel, settings: Settings, llm=None, generation_budget=None, attempt_base=None) -> RunModel:
    """唯一执行入口：一个 Scheduler 按 Plan 驱动全流程。

    前后段节点全部注册到同一张 Plan，节点之间不直接调用；
    跨节点上下文由 build_context 从 EventLog + BlobStore 自动折叠恢复；
    人工门由 status 中断，自愈由 Outcome.goto 交回 Scheduler 重新调度。
    """
    state = project(session, run)
    if state.status in {"COMPLETED", "FAILED", "WAITING_APPROVAL", "WAITING_TEST_APPROVAL"}:
        return run

    specs = _specs(session, run.spec_id)
    llm = llm or RealDesignLlm(settings)
    resolved_budget, concurrency = _resolve_budget(session, generation_budget)
    ctx = build_context(
        session, run, settings, llm=llm, budget=resolved_budget,
        concurrency=concurrency, attempt_base=attempt_base,
    )
    if generation_budget is not None or ctx.get("use_plan"):
        ctx.put("use_plan", True)

    bodies = {}
    bodies.update(_front_bodies(specs, llm, resolved_budget, concurrency, settings))
    bodies.update(_back_bodies(specs, settings))
    scheduler = Scheduler(DEFAULT_PLAN, bodies)

    try:
        scheduler.drive(ctx)
    except Exception as exc:
        failed_node = ctx.current_node or ctx.state.current_node or "created"
        if hasattr(ctx.bus, "fire"):
            ctx.bus.fire("node.error", {"run_id": run.id, "node": failed_node, "error": f"{type(exc).__name__}: {exc}"})
        for rej in (ctx.bus.drain_rejections() if hasattr(ctx.bus, "drain_rejections") else []):
            append_event(session, run, "BUS_REJECTED", failed_node, rej, bus=ctx.bus)
        append_event(
            session, run, "RUN_FAILED", failed_node,
            {
                "node": failed_node,
                "resume_from_node": ctx.state.resume_from_node,
                "resume_when": ctx.state.resume_when,
                "result": {
                    "error": f"{type(exc).__name__}: {exc}",
                    "error_details": _error_details(exc, failed_node),
                },
            },
            bus=ctx.bus,
        )
        if hasattr(ctx.bus, "fire"):
            ctx.bus.fire("run.failed", {"run_id": run.id, "node": failed_node, "status": "FAILED", "error": f"{type(exc).__name__}: {exc}"})
        session.commit()
    return run


def start_generic_run(session: Session, spec_id: str, settings: Settings, llm=None, generation_budget=None) -> RunModel:
    """同步入口：创建 run 并执行到第一个 gate。测试与内部调用使用。"""
    run = _create_run(session, spec_id, settings)
    return drive(session, run, settings, llm, generation_budget)


def approve_generic_rules(
    session: Session,
    run: RunModel,
    approver: str,
    decisions: Dict[str, str],
    settings: Settings,
    llm=None,
    defer_continuation: bool = False,
) -> RunModel:
    # 幂等检查先于 status 校验：全决断后状态已离开 WAITING_APPROVAL，
    # 同样的请求重放应返回原结果，而不是报 409。
    normalized_input = {rule_hash: decision for rule_hash, decision in decisions.items()}
    fingerprint = hashlib.sha256(canonical_json(normalized_input).encode()).hexdigest()
    latest = latest_interrupt(session, run, "APPROVAL_REQUIRED")
    if latest and (latest.payload_json or {}).get("interrupt_id"):
        interrupt_id = latest.payload_json["interrupt_id"]
        prior = matching_answer(session, run, "HUMAN_DECISIONS_RECORDED", interrupt_id, fingerprint)
        if prior:
            return run
        answered = [
            event for event in session.query(EventModel)
            .filter(EventModel.run_id == run.id, EventModel.kind == "HUMAN_DECISIONS_RECORDED")
            .all()
            if (event.payload_json or {}).get("interrupt_id") == interrupt_id
        ]
        if answered:
            raise WorkflowError("This interrupt was already answered with different decisions")
    if project(session, run).status != "WAITING_APPROVAL":
        raise WorkflowError("Run is not waiting for rule approval")
    interrupt = pending_interrupt(session, run, "APPROVAL_REQUIRED")
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
    answer_payload = {"approver": approver, "decisions": normalized, "all_decided": all_decided}
    if interrupt and all_decided:
        # 只有全部决断才算"回答完成这次中断"；部分决断是同一中断的逐步收敛，
        # 不标记关联，否则下一次提交会被误判为"已用不同内容回答"
        answer_payload["interrupt_id"] = interrupt.payload_json["interrupt_id"]
        answer_payload["interrupt_seq"] = interrupt.seq
        answer_payload["answer_fingerprint"] = fingerprint
    append_event(session, run, "HUMAN_DECISIONS_RECORDED", "human_gate", answer_payload)
    session.commit()
    if not all_decided:
        return run
    if defer_continuation:
        return run
    return continue_generic_run(session, run, settings, llm)


def confirm_generic_test_results(session: Session, run: RunModel, approver: str) -> RunModel:
    # 幂等：同一中断 + 同一确认人重放，直接返回原结果
    latest = latest_interrupt(session, run, "TEST_CONFIRMATION_REQUIRED")
    fingerprint = hashlib.sha256(canonical_json({"approver": approver}).encode()).hexdigest()
    if latest and (latest.payload_json or {}).get("interrupt_id"):
        interrupt_id = latest.payload_json["interrupt_id"]
        if matching_answer(session, run, "TEST_RESULTS_CONFIRMED", interrupt_id, fingerprint):
            return run
    if project(session, run).status != "WAITING_TEST_APPROVAL":
        raise WorkflowError("Run is not waiting for test result confirmation")
    interrupt = pending_interrupt(session, run, "TEST_CONFIRMATION_REQUIRED")
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
    answer_payload = {"approver": approver, "object_hash": object_hash}
    if interrupt:
        answer_payload["interrupt_id"] = interrupt.payload_json["interrupt_id"]
        answer_payload["interrupt_seq"] = interrupt.seq
        answer_payload["answer_fingerprint"] = fingerprint
    append_event(session, run, "TEST_RESULTS_CONFIRMED", "test_confirmation", answer_payload)
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
    2. 若在审计前因超时/网络/进程重启中断（无审计报告）：写 RUN_RESUMED 事件，从 EventLog 最后一个已完成节点断点续跑。
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
        # 节点中途异常或重启中断：从 EventLog 折叠出的最近检查点原地续跑
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


def continue_generic_run(session: Session, run: RunModel, settings: Settings, llm=None, generation_budget=None, attempt_base: Optional[int] = None) -> RunModel:
    """兼容入口：统一走唯一 Scheduler（drive）。"""
    return drive(session, run, settings, llm, generation_budget, attempt_base)
