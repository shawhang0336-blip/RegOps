"""统一受控大模型调用守卫（基础设施层 Layer 0，跨阶段复用）。

职责边界：
- 位于 Layer 0（infra），紧邻 `infra/llm.py`，零依赖 `kernel` 与 `policy`，可供 ingestion / assets / engine 三阶段共同复用；
- 通过鸭子类型无缝配合上层通过 `llm_execution_scope` 或 `bus=` 参数注入的 Bus（门禁裁决 / 预算熔断 / 生命周期广播）与 CancellationToken（协作式取消）；
- 按错误性质分轨容错：
  1. 网络/超时/5xx 瞬时错误：消耗 `transient_attempts`，按带抖动退避序列重试并逐次放宽超时窗口，不向模型注入无关 feedback；
  2. 模型漏调工具（no tool call / unexpected tool）：消耗 `validation_attempts`，立即注入硬约束提示重试，不虚耗退避等待与超时放宽；
  3. Bus 事前门禁拦截或确定性 validator 拒绝（ValueError / KeyError）：消耗 `validation_attempts`，把拒绝原因回喂给模型修正并留痕 `bus_rejections`；
  4. 鉴权/配置/不可用等不可恢复 RuntimeError，或 validator 内部代码缺陷（如 TypeError）：快速失败，不空耗重试预算。
"""

from contextlib import contextmanager
import contextvars
import random
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import httpx

from app.infra.errors import WorkflowError
from app.infra.llm import LlmTimeoutError

_ACTIVE_BUS: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar("regops_active_llm_bus", default=None)
_ACTIVE_CANCEL: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar("regops_active_llm_cancel", default=None)


def current_llm_bus() -> Optional[Any]:
    """返回当前上下文作用域挂载的 Run 级 Bus（如在 Step 1 / Step 2 Run 中运行）。"""
    return _ACTIVE_BUS.get()


def current_cancellation() -> Optional[Any]:
    """返回当前上下文作用域挂载的 CancellationToken（供并发 worker 协作式检查取消）。"""
    return _ACTIVE_CANCEL.get()


@contextmanager
def llm_execution_scope(*, bus: Any = None, cancellation: Any = None) -> Iterator[None]:
    """把 Run 级 Bus（含 BudgetGuard）与 CancellationToken 透传到所有深层 call_llm_validated 调用。"""
    bus_token = _ACTIVE_BUS.set(bus) if bus is not None else None
    cancel_token = _ACTIVE_CANCEL.set(cancellation) if cancellation is not None else None
    try:
        yield
    finally:
        if cancel_token is not None:
            _ACTIVE_CANCEL.reset(cancel_token)
        if bus_token is not None:
            _ACTIVE_BUS.reset(bus_token)


def submit_with_context(executor: Any, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """向线程池提交任务时显式复制当前 contextvars，确保 worker 继承 Bus/BudgetGuard/CancellationToken。"""
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, fn, *args, **kwargs)


STAGE_BY_TOOL = {
    "inspect_document_structure": "document_overview",
    "assign_document_structure": "document_structure",
    "assign_block_segments": "block_segmenter",
    "assign_metadata": "metadata_extractor",
    "review_metadata": "metadata_reviewer",
    "assign_code_set": "code_set_extractor",
    "review_code_set": "code_set_reviewer",
    "assign_generation_plan": "generation_planner",
    "assign_schema": "schema_mapper",
    "assign_rules": "rule_binder",
    "assign_generators": "data_generator_binder",
    "assign_audit_cases": "audit_case_binder",
}

ERROR_SUGGESTIONS = {
    "timeout": "模型响应超时（已退避重试仍失败）。可调大 LLM_TIMEOUT，或检查模型服务状态与队列。",
    "model_error": "模型调用失败或未返回工具调用。检查 API key、模型名与服务可用性。",
    "model_no_tool_call": "模型多次未返回结构化工具调用。检查模型是否支持 function calling 或调整 LLM_TOOL_CHOICE。",
    "transport": "模型接口网络抖动或连接中断。检查网络代理与接口端点可用性。",
    "validation": "AI 返回的参数连续多次未通过确定性校验或 Bus 门禁。检查工具契约与输入 payload。",
    "internal": "非模型错误。查看 message 与后端日志定位。",
}

DEFAULT_TRANSIENT_ATTEMPTS = 4
DEFAULT_VALIDATION_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = (2.0, 5.0, 10.0)

_NO_TOOL_CALL_HINT = (
    "上一次没有返回工具调用。必须通过工具返回结构化参数，禁止输出自然语言或 SQL；"
    "字段名、规则参数必须严格取自输入。"
)

_TRANSIENT_HTTP_MARKERS = ("LLM HTTP 429", "LLM HTTP 500", "LLM HTTP 502", "LLM HTTP 503", "LLM HTTP 504")
_TRANSIENT_TRANSPORT_MARKERS = (
    "ConnectError",
    "ConnectTimeout",
    "ReadError",
    "WriteError",
    "RemoteProtocolError",
    "PoolTimeout",
    "NetworkError",
    "ProxyError",
    "CloseError",
    "ProtocolError",
    "TimeoutException",
    "ConnectionResetError",
    "ConnectionError",
)


def locate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """从工具输入 payload 中提取定位信息（table_code / 单字段名），用于结构化错误诊断。"""
    safe = payload if isinstance(payload, dict) else {}
    table_obj = safe.get("table")
    table_code = safe.get("table_code") or (table_obj.get("table_code") if isinstance(table_obj, dict) else None)
    fields = safe.get("fields") or []
    field = None
    if isinstance(fields, list) and len(fields) == 1 and isinstance(fields[0], dict):
        field = fields[0].get("name") or fields[0].get("field_en")
    return {"table_code": table_code, "field": field}


def fail_context(tool_name: str, payload: Dict[str, Any], error_kind: str, retried: bool = False) -> Dict[str, Any]:
    """构造带阶段与表/字段定位的结构化失败上下文。"""
    context = {
        "tool_name": tool_name,
        "stage": STAGE_BY_TOOL.get(tool_name, tool_name),
        "error_kind": error_kind,
        "retried": retried,
    }
    context.update(locate_payload(payload))
    return context


def backoff_wait(
    steps: Tuple[float, ...],
    attempt: int,
    *,
    cancellation: Any = None,
    jitter: float = 0.2,
) -> None:
    """带随机抖动（防惊群）且可被 CancellationToken 瞬间唤醒的退避等待。"""
    index = min(max(attempt - 1, 0), len(steps) - 1)
    delay = float(steps[index]) if steps else 0.0
    if delay <= 0:
        return
    if jitter > 0:
        delay = max(0.0, delay * random.uniform(1.0 - jitter, 1.0 + jitter))
    if cancellation is not None and hasattr(cancellation, "wait"):
        if cancellation.wait(delay):
            cancellation.raise_if_cancelled()
        return
    time.sleep(delay)


def _classify_runtime_error(exc: RuntimeError) -> str:
    if isinstance(exc.__cause__, (httpx.HTTPError, TimeoutError, ConnectionError)):
        return "transport"
    text = str(exc)
    low = text.lower()
    if "no tool call" in low or "unexpected tool" in low:
        return "model_no_tool_call"
    if any(marker in text for marker in _TRANSIENT_HTTP_MARKERS) or any(
        marker in text for marker in _TRANSIENT_TRANSPORT_MARKERS
    ):
        return "transport"
    return "fatal_model_error"


def call_llm_validated(
    llm: Any,
    tool_name: str,
    instruction: str,
    payload: Dict[str, Any],
    validator: Optional[Callable[[Dict[str, Any]], Any]] = None,
    *,
    timeout: Optional[int] = None,
    backoff: Optional[Tuple[float, ...]] = None,
    bus: Any = None,
    transient_attempts: int = DEFAULT_TRANSIENT_ATTEMPTS,
    validation_attempts: int = DEFAULT_VALIDATION_ATTEMPTS,
) -> Tuple[Any, Dict[str, Any]]:
    """统一受控大模型调用：退避重试 + 可插拔 Bus 前置门禁 + 校验失败回喂修正。"""
    feedback: Optional[str] = None
    rejection: Optional[str] = None
    bus_rejections: List[Dict[str, Any]] = []
    last_kind = "validation"
    last_error = ""
    retried_after_timeout = False
    transient_used = 0
    validation_used = 0
    wait_steps = DEFAULT_BACKOFF_SECONDS if backoff is None else backoff
    active_bus = bus if bus is not None else _ACTIVE_BUS.get()
    cancellation = _ACTIVE_CANCEL.get()
    validate_fn = validator if validator is not None else (lambda args: args)

    max_total = max(1, transient_attempts) + max(1, validation_attempts)
    for attempt in range(1, max_total + 1):
        if cancellation is not None and hasattr(cancellation, "raise_if_cancelled"):
            cancellation.raise_if_cancelled()
        if active_bus is not None and hasattr(active_bus, "check_budget"):
            active_bus.check_budget()
        attempt_timeout = timeout
        if timeout and transient_used:
            attempt_timeout = int(min(timeout * (1 + 0.5 * transient_used), timeout * 2))
        try:
            if attempt_timeout is None:
                if feedback is None:
                    arguments, trace = llm.assign(tool_name, instruction, payload)
                else:
                    arguments, trace = llm.assign(tool_name, instruction, payload, feedback)
            else:
                if feedback is None:
                    arguments, trace = llm.assign(tool_name, instruction, payload, timeout=attempt_timeout)
                else:
                    arguments, trace = llm.assign(tool_name, instruction, payload, feedback, timeout=attempt_timeout)

            if not isinstance(arguments, dict):
                raise ValueError(f"工具 {tool_name} 必须返回 JSON 对象参数，实际收到 {type(arguments).__name__}")

            rejection_reason = (
                active_bus.adjudicate(tool_name, arguments, payload)
                if active_bus is not None and hasattr(active_bus, "adjudicate")
                else None
            )
            if rejection_reason:
                bus_rejections.append(
                    {
                        "attempt": attempt,
                        "tool_name": tool_name,
                        "reason": rejection_reason,
                        "arguments": arguments,
                    }
                )
                if isinstance(trace, dict):
                    trace["bus_rejected"] = rejection_reason
                raise ValueError(rejection_reason)

            result = validate_fn(arguments)
            if isinstance(trace, dict):
                if bus_rejections:
                    trace["bus_rejected"] = bus_rejections[-1]["reason"]
                    trace["bus_rejections"] = bus_rejections
                if rejection:
                    trace["previous_rejection"] = rejection
                if retried_after_timeout:
                    trace["retried_after_timeout"] = True
                if attempt > 1:
                    trace["attempts"] = attempt
            if active_bus is not None and hasattr(active_bus, "fire"):
                loc = locate_payload(payload)
                after_payload: Dict[str, Any] = {
                    "tool": tool_name,
                    "attempts": attempt,
                    "table_code": loc["table_code"],
                }
                if isinstance(trace, dict):
                    if trace.get("elapsed_seconds") is not None:
                        after_payload["elapsed_seconds"] = trace.get("elapsed_seconds")
                    if isinstance(trace.get("usage"), dict):
                        after_payload["usage"] = trace["usage"]
                active_bus.fire("tool.after", after_payload)
            return result, trace
        except LlmTimeoutError as exc:
            transient_used += 1
            retried_after_timeout = True
            last_kind, last_error = "timeout", str(exc)
            if transient_used >= transient_attempts:
                break
            backoff_wait(wait_steps, transient_used, cancellation=cancellation)
        except (httpx.HTTPError, TimeoutError, ConnectionError) as exc:
            transient_used += 1
            last_kind, last_error = "transport", f"{type(exc).__name__}: {exc}"
            if transient_used >= transient_attempts:
                break
            backoff_wait(wait_steps, transient_used, cancellation=cancellation)
        except RuntimeError as exc:
            kind = _classify_runtime_error(exc)
            last_error = str(exc)
            if kind == "fatal_model_error":
                transient_used += 1
                last_kind = "model_error"
                break
            if kind == "model_no_tool_call":
                # 模型漏调工具属于输出契约违约，消耗校验配额并直接带硬提示重试，不虚耗网络退避时间
                validation_used += 1
                last_kind = kind
                feedback = _NO_TOOL_CALL_HINT
                if validation_used >= validation_attempts:
                    break
                continue
            transient_used += 1
            last_kind = kind
            if transient_used >= transient_attempts:
                break
            backoff_wait(wait_steps, transient_used, cancellation=cancellation)
        except (ValueError, KeyError) as exc:
            validation_used += 1
            rejection = f"缺少必填字段 {exc}" if isinstance(exc, KeyError) else str(exc)
            feedback = rejection
            last_kind, last_error = "validation", rejection
            if validation_used >= validation_attempts:
                break

    if last_kind == "timeout":
        message = f"{tool_name} model call timed out after retry ({transient_used} 次): {last_error}"
    elif last_kind == "validation":
        message = f"{tool_name} 连续 {validation_used} 次未通过参数校验：{last_error}"
    elif last_kind == "model_no_tool_call":
        message = f"{tool_name} 连续 {validation_used} 次没有拿到工具调用返回（{last_kind}）：{last_error}"
    else:
        message = f"{tool_name} 连续 {transient_used} 次没有拿到可用返回（{last_kind}）：{last_error}"
    raise WorkflowError(message, context=fail_context(tool_name, payload, last_kind, retried=transient_used > 1))
