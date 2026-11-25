"""工作流运行时支撑（Workflow Runtime）。

职责边界：
- 受控大模型调用 `_call_validated`：瞬时错误退避重试 + Bus 事前确定性门禁拦截 + 拒绝原因回喂模型；
- BSP 并发波次扇出 `_fanout`：按表并发执行 + `wave.fanout` / `wave.barrier` 生命周期广播；
- 通用辅助：规则落库 `_upsert_rule`、规格读取 `_specs`、码表上下文 `_code_set_context`、
  事件写集 `_emit` 与失败上下文诊断 `_error_details`。
"""

from concurrent.futures import ThreadPoolExecutor
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from app.infra.contracts import RuleDefinition, SpecDocument
from app.infra.errors import ProviderAuthOrBillingError, WorkflowError
from app.infra.llm import configure_llm_concurrency, configure_llm_rate_limit
from app.infra.db_tables import EventModel, RuleModel, RunModel, SpecificationModel
from app.infra.config import get_budget
from app.kernel.bus import global_bus
from app.kernel.event_log import append_event
from app.kernel.run import BudgetExceededError
from app.kernel.scheduler import execute_wave
from app.infra.llm.guard import (
    DEFAULT_BACKOFF_SECONDS as _BACKOFF_SECONDS,
    DEFAULT_TRANSIENT_ATTEMPTS as _TRANSIENT_ATTEMPTS,
    DEFAULT_VALIDATION_ATTEMPTS as _VALIDATION_ATTEMPTS,
    ERROR_SUGGESTIONS,
    STAGE_BY_TOOL,
    call_llm_validated,
    current_cancellation,
    current_llm_bus,
    fail_context as _fail_context,
    locate_payload as _locate_payload,
    submit_with_context,
)
from app.policy.regops_bus import default_bus


def _upsert_rule(session: Session, run: RunModel, rule: RuleDefinition, status: str) -> None:
    existing = (
        session.query(RuleModel)
        .filter(RuleModel.run_id == run.id, RuleModel.rule_hash == rule.rule_hash)
        .one_or_none()
    )
    if existing:
        if existing.status == "REJECTED" and status == "PENDING":
            existing.status = "PENDING"
        return
    session.add(
        RuleModel(
            row_id=str(uuid.uuid4()),
            run_id=run.id,
            spec_id=run.spec_id,
            table_code=rule.table_code,
            rule_key=rule.rule_key,
            rule_hash=rule.rule_hash,
            source=rule.source,
            status=status,
            definition_json=rule.model_dump(),
        )
    )
    session.flush()


def _pending_rules(session: Session, run_id: str) -> List[RuleModel]:
    return session.query(RuleModel).filter(RuleModel.run_id == run_id, RuleModel.status == "PENDING").all()


def _specs(session: Session, spec_id: str) -> List[SpecDocument]:
    row = session.get(SpecificationModel, spec_id)
    if not row or "tables" not in row.content_json:
        raise WorkflowError("Confirmed specification snapshot not found")
    if not row.content_json.get("selection_confirmed"):
        raise WorkflowError("Tables must be confirmed before any AI binding or DDL execution")
    return [SpecDocument.model_validate(item) for item in row.content_json["tables"]]


def _code_set_context(field_code_sets, table_code: str, field: str) -> Dict[str, Any]:
    code_set = field_code_sets.get((table_code, field))
    if not code_set:
        return {}
    return {
        "published_code_set": {
            "code": code_set["code"],
            "name": code_set["name"],
            "version": code_set["version"],
            "values": [item["value"] for item in code_set["values"]],
        }
    }


def _error_details(exc: BaseException, fallback_stage: str) -> Dict[str, Any]:
    if isinstance(exc, BudgetExceededError):
        return {
            "stage": fallback_stage,
            "error_type": type(exc).__name__,
            "error_kind": "budget",
            "metric": exc.metric,
            "used": exc.used,
            "limit": exc.limit,
            "budget_snapshot": exc.snapshot,
            "suggestion": "执行已触达内核预算上限（BudgetGuard 熔断）。可在系统设置放宽预算或缩小单批表数量后重试。",
        }
    if isinstance(exc, ProviderAuthOrBillingError):
        context = getattr(exc, "context", None) or {}
        return {
            "stage": context.get("stage") or fallback_stage,
            "error_type": type(exc).__name__,
            "error_kind": "billing_or_auth",
            "tool_name": context.get("tool_name"),
            "table_code": context.get("table_code"),
            "field": context.get("field"),
            "suggestion": ERROR_SUGGESTIONS.get("billing_or_auth", ERROR_SUGGESTIONS["internal"]),
        }
    context = getattr(exc, "context", None) or {}
    kind = context.get("error_kind") or "internal"
    return {
        "stage": context.get("stage") or fallback_stage,
        "error_type": type(exc).__name__,
        "error_kind": kind,
        "tool_name": context.get("tool_name"),
        "table_code": context.get("table_code"),
        "field": context.get("field"),
        "retried_after_timeout": context.get("retried"),
        "suggestion": ERROR_SUGGESTIONS.get(kind, ERROR_SUGGESTIONS["internal"]),
    }


def _emit(
    events: Optional[List[tuple]],
    session: Session,
    run: RunModel,
    kind: str,
    node: str,
    payload: Dict[str, Any],
) -> None:
    """将事件收入节点写集（由 Scheduler 屏障处统一落账）；仅在单测直调辅助函数时直接写库。"""
    if events is not None:
        events.append((kind, node, payload))
    else:
        append_event(session, run, kind, node, payload)


def _call_validated(llm, tool_name, instruction, payload, validator, timeout=None, backoff=None, bus=None):
    """兼容入口：委托给底座统一的 call_llm_validated，默认装配 RegOps 门禁总线。"""
    active_bus = bus if bus is not None else (current_llm_bus() or default_bus())
    return call_llm_validated(
        llm,
        tool_name,
        instruction,
        payload,
        validator,
        timeout=timeout,
        backoff=backoff,
        bus=active_bus,
        transient_attempts=_TRANSIENT_ATTEMPTS,
        validation_attempts=_VALIDATION_ATTEMPTS,
    )


def _resolve_budget(session: Session, generation_budget=None):
    """预算只读一次并贯通到底：max_retries 决定自愈轮数，max_llm_concurrency / max_llm_rpm 决定并发与限速。"""
    if generation_budget is None:
        generation_budget = get_budget(session)
    concurrency = max(1, int(generation_budget.get("max_llm_concurrency", 4)))
    configure_llm_concurrency(concurrency)
    configure_llm_rate_limit(int(generation_budget.get("max_llm_rpm") or 0))
    return generation_budget, concurrency


def _inner_width(concurrency: int, outer_workers: int) -> int:
    """外层扇出已占用的并发额度，剩下的才给字段级内层池，避免外层 4 × 内层 4 = 16 线程争抢。"""
    return max(1, concurrency // max(1, outer_workers))


def _map_with_context(fn: Callable[[Any], Any], items: Sequence[Any], max_workers: int) -> List[Any]:
    """保留 contextvars（取消令牌 / Run 级 Bus / BudgetGuard）的内层并发映射，遇取消或预算熔断立即撤回排队任务。"""
    cancellation = current_cancellation()
    if max_workers <= 1 or len(items) <= 1:
        results = []
        for item in items:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            results.append(fn(item))
        return results
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [submit_with_context(executor, fn, item) for item in items]
        results = []
        try:
            for future in futures:
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                results.append(future.result())
            return results
        except BaseException:
            for pending in futures:
                pending.cancel()
            raise


def _fanout(
    session: Session,
    run: RunModel,
    stage: str,
    specs: List[SpecDocument],
    worker,
    max_workers: int = 4,
    events: Optional[List[tuple]] = None,
    bus: Any = None,
    cancellation: Any = None,
) -> List[Any]:
    """多表 BSP 波次扇出：统一委托至微内核 `kernel.scheduler.execute_wave`。"""
    table_codes = [spec.table_code for spec in specs]
    active_bus = bus if bus is not None else global_bus()
    _emit(
        events,
        session,
        run,
        "BSP_FANOUT",
        stage,
        {"stage": stage, "table_codes": table_codes},
    )
    results = execute_wave(
        specs,
        worker,
        bus=active_bus,
        run_id=run.id,
        stage=stage,
        labels=table_codes,
        max_workers=max_workers,
        cancellation=cancellation,
    )
    for spec, (_, trace) in zip(specs, results):
        _emit(
            events,
            session,
            run,
            "AI_TOOL_ASSIGNED",
            stage,
            {"table_code": spec.table_code, **trace},
        )
    return results


def _latest_payload(session: Session, run_id: str, kind: str) -> Dict[str, Any]:
    event = (
        session.query(EventModel)
        .filter(EventModel.run_id == run_id, EventModel.kind == kind)
        .order_by(EventModel.seq.desc())
        .first()
    )
    if not event:
        raise WorkflowError(f"Missing event payload: {kind}")
    return event.payload_json
