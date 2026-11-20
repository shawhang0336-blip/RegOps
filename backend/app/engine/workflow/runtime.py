"""工作流运行时支撑（Workflow Runtime）。

职责边界：
- 受控大模型调用 `_call_validated`：瞬时错误退避重试 + Bus 事前确定性门禁拦截 + 拒绝原因回喂模型；
- BSP 并发波次扇出 `_fanout`：按表并发执行 + `wave.fanout` / `wave.barrier` 生命周期广播；
- 通用辅助：规则落库 `_upsert_rule`、规格读取 `_specs`、码表上下文 `_code_set_context`、
  事件写集 `_emit` 与失败上下文诊断 `_error_details`。
"""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.orm import Session

from app.engine.rules import RuleValidationError
from app.infra.domain import RuleDefinition, SpecDocument
from app.infra.errors import WorkflowError
from app.infra.llm import LlmTimeoutError, configure_llm_concurrency
from app.infra.models import EventModel, RuleModel, RunModel, SpecificationModel
from app.infra.settings_store import get_budget
from app.kernel.bus import global_bus
from app.kernel.event_log import append_event
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


STAGE_BY_TOOL = {
    "assign_generation_plan": "generation_planner",
    "assign_schema": "schema_mapper",
    "assign_rules": "rule_binder",
    "assign_generators": "data_generator_binder",
    "assign_audit_cases": "audit_case_binder",
}

ERROR_SUGGESTIONS = {
    "timeout": "模型响应超时（已重试一次仍失败）。可调大 LLM_TIMEOUT，或检查模型服务状态与队列。",
    "model_error": "模型调用失败或未返回工具调用。检查 API key、模型名与服务可用性。",
    "validation": "AI 返回的参数连续两次未通过确定性校验。检查工具契约与输入 payload。",
    "internal": "非模型错误。查看 message 与后端日志定位。",
}


def _locate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    table_code = payload.get("table_code") or (payload.get("table") or {}).get("table_code")
    fields = payload.get("fields") or []
    field = None
    if isinstance(fields, list) and len(fields) == 1 and isinstance(fields[0], dict):
        field = fields[0].get("name")
    return {"table_code": table_code, "field": field}


def _fail_context(tool_name: str, payload: Dict[str, Any], error_kind: str, retried: bool = False) -> Dict[str, Any]:
    context = {
        "tool_name": tool_name,
        "stage": STAGE_BY_TOOL.get(tool_name, tool_name),
        "error_kind": error_kind,
        "retried": retried,
    }
    context.update(_locate_payload(payload))
    return context


def _error_details(exc: BaseException, fallback_stage: str) -> Dict[str, Any]:
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


# 瞬时错误（网络/超时/模型不调工具）的重试预算。线上 8 次失败里 6 次属于这类，
# 给 4 次机会 + 退避能把「一次抖动毁掉整批」的概率压到很低。
_TRANSIENT_ATTEMPTS = 4
# 参数校验失败的重试预算：把拒绝原因回喂给模型，通常第二次就能改对
_VALIDATION_ATTEMPTS = 3
_BACKOFF_SECONDS = (2.0, 5.0, 10.0)
_NO_TOOL_CALL_HINT = (
    "上一次没有返回工具调用。必须通过工具返回结构化参数，禁止输出自然语言或 SQL；"
    "字段名、规则参数必须严格取自输入。"
)


def _backoff_wait(steps, attempt: int) -> None:
    index = min(max(attempt - 1, 0), len(steps) - 1)
    delay = steps[index] if steps else 0
    if delay:
        time.sleep(delay)


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
    """带退避与 Bus 前置门禁的模型调用。

    原来只重试 2 次、HTTP 错误直接抛、超时只给 1 次机会。线上 8 次失败里有 6 次是
    这类瞬时错误（"The read operation timed out" / "Model returned no tool call"），
    一次网络抖动就整批 FAILED，代价太大。现在按错误性质分开退避：

    - 网络/超时：重试 4 次，超时窗口逐次放宽，不回喂 feedback（模型侧可能压根没收到）；
    - 模型没调工具：重试 4 次，给一句更硬的指令（这是行为问题，不是网络问题）；
    - Bus 门禁拦截 / 参数校验不过：重试 3 次，把拒绝原因回喂给模型，且全程保留拦截记录。
    """
    feedback = None
    rejection = None
    bus_rejections: List[Dict[str, Any]] = []
    last_kind = "validation"
    last_error = ""
    retried_after_timeout = False
    transient_used = 0
    validation_used = 0
    wait_steps = _BACKOFF_SECONDS if backoff is None else backoff
    bus = bus if bus is not None else default_bus()

    for attempt in range(1, _TRANSIENT_ATTEMPTS + _VALIDATION_ATTEMPTS + 1):
        # 超时过就放宽窗口，避免同一个请求反复被同一个超时掐掉
        attempt_timeout = timeout
        if timeout and transient_used:
            attempt_timeout = int(min(timeout * (1 + 0.5 * transient_used), timeout * 2))
        try:
            if attempt_timeout is None:
                arguments, trace = llm.assign(tool_name, instruction, payload, feedback)
            else:
                arguments, trace = llm.assign(tool_name, instruction, payload, feedback, timeout=attempt_timeout)
            # Bus 前置裁决：字段名白名单、跨表引用白名单、算子白名单、码表值域等硬门。
            # 裁决失败按校验失败处理，原因回喂模型重试，并跨轮次保留在 bus_rejections 供审计。
            rejection_reason = bus.adjudicate(tool_name, arguments, payload)
            if rejection_reason:
                bus_rejections.append(
                    {
                        "attempt": attempt,
                        "tool_name": tool_name,
                        "reason": rejection_reason,
                        "arguments": arguments,
                    }
                )
                trace["bus_rejected"] = rejection_reason
                raise RuleValidationError(rejection_reason)
            result = validator(arguments)
            if bus_rejections:
                trace["bus_rejected"] = bus_rejections[-1]["reason"]
                trace["bus_rejections"] = bus_rejections
            if rejection:
                trace["previous_rejection"] = rejection
            if retried_after_timeout:
                trace["retried_after_timeout"] = True
            if attempt > 1:
                trace["attempts"] = attempt
            if hasattr(bus, "fire"):
                bus.fire(
                    "tool.after",
                    {
                        "tool": tool_name,
                        "attempts": attempt,
                        "table_code": payload.get("table_code") or (payload.get("table") or {}).get("table_code"),
                    },
                )
            return result, trace
        except LlmTimeoutError as exc:
            transient_used += 1
            retried_after_timeout = True
            last_kind, last_error = "timeout", str(exc)
            if transient_used >= _TRANSIENT_ATTEMPTS:
                break
            _backoff_wait(wait_steps, transient_used)
        except (httpx.HTTPError, TimeoutError, ConnectionError) as exc:
            transient_used += 1
            last_kind, last_error = "transport", f"{type(exc).__name__}: {exc}"
            if transient_used >= _TRANSIENT_ATTEMPTS:
                break
            _backoff_wait(wait_steps, transient_used)
        except RuntimeError as exc:
            # 模型没返回工具调用：给一句更硬的指令再来一次，而不是直接判死
            transient_used += 1
            last_kind, last_error = "model_no_tool_call", str(exc)
            feedback = _NO_TOOL_CALL_HINT
            if transient_used >= _TRANSIENT_ATTEMPTS:
                break
            _backoff_wait(wait_steps, transient_used)
        except (RuleValidationError, ValueError, KeyError, TypeError) as exc:
            validation_used += 1
            rejection = str(exc)
            feedback = rejection
            last_kind, last_error = "validation", rejection
            if validation_used >= _VALIDATION_ATTEMPTS:
                break

    if last_kind == "timeout":
        message = f"{tool_name} model call timed out after retry ({transient_used} 次): {last_error}"
    elif last_kind == "validation":
        message = f"{tool_name} 连续 {validation_used} 次未通过参数校验：{last_error}"
    else:
        message = f"{tool_name} 连续 {transient_used} 次没有拿到可用返回（{last_kind}）：{last_error}"
    raise WorkflowError(message, context=_fail_context(tool_name, payload, last_kind, retried=transient_used > 1))


def _resolve_budget(session: Session, generation_budget=None):
    """预算只读一次并贯通到底：max_retries 决定自愈轮数，max_llm_concurrency 决定并发宽度。"""
    if generation_budget is None:
        generation_budget = get_budget(session)
    concurrency = max(1, int(generation_budget.get("max_llm_concurrency", 4)))
    configure_llm_concurrency(concurrency)
    return generation_budget, concurrency


def _inner_width(concurrency: int, outer_workers: int) -> int:
    """外层扇出已占用的并发额度，剩下的才给字段级内层池，避免外层 4 × 内层 4 = 16 线程争抢。"""
    return max(1, concurrency // max(1, outer_workers))


def _fanout(
    session: Session,
    run: RunModel,
    stage: str,
    specs: List[SpecDocument],
    worker,
    max_workers: int = 4,
    events: Optional[List[tuple]] = None,
    bus: Any = None,
) -> List[Any]:
    table_codes = [spec.table_code for spec in specs]
    active_bus = bus if bus is not None else global_bus()
    if hasattr(active_bus, "fire"):
        active_bus.fire("wave.fanout", {"run_id": run.id, "stage": stage, "table_codes": table_codes, "wave_size": len(specs)})
    _emit(
        events,
        session,
        run,
        "BSP_FANOUT",
        stage,
        {"stage": stage, "table_codes": table_codes},
    )
    with ThreadPoolExecutor(max_workers=min(max_workers, len(specs))) as executor:
        futures = [executor.submit(worker, spec) for spec in specs]
        results = [future.result() for future in futures]
    if hasattr(active_bus, "fire"):
        active_bus.fire("wave.barrier", {"run_id": run.id, "stage": stage, "table_codes": table_codes, "merged_count": len(results)})
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
