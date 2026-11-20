"""统一受控大模型调用守卫（策略层，跨阶段复用）。

职责边界：
- 位于 Layer 2（policy），仅依赖 infra（Layer 0）与 kernel（Layer 1），可供 ingestion / assets / engine 三阶段共同复用；
- 按错误性质分类退避：
  1. 网络/超时/5xx 瞬时错误：按退避序列重试并逐次放宽超时窗口，不向模型注入无关 feedback；
  2. 模型漏调工具（no tool call / unexpected tool）：注入硬约束提示重试；
  3. Bus 事前门禁拦截或确定性 validator 拒绝：把拒绝原因回喂给模型修正，并全程留痕 `bus_rejections`；
  4. 鉴权/配置/不可用等不可恢复 RuntimeError（如 `LLM_API_KEY is required`、`offline unavailable`、`HTTP 400/401/403`）：快速失败不空耗重试预算。
"""

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from app.infra.errors import WorkflowError
from app.infra.llm import LlmTimeoutError
from app.policy.regops_bus import default_bus

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


def _locate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    table_code = payload.get("table_code") or (payload.get("table") or {}).get("table_code")
    fields = payload.get("fields") or []
    field = None
    if isinstance(fields, list) and len(fields) == 1 and isinstance(fields[0], dict):
        field = fields[0].get("name") or fields[0].get("field_en")
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


def _backoff_wait(steps: Tuple[float, ...], attempt: int) -> None:
    index = min(max(attempt - 1, 0), len(steps) - 1)
    delay = steps[index] if steps else 0
    if delay:
        time.sleep(delay)


def _classify_runtime_error(exc: RuntimeError) -> str:
    text = str(exc)
    low = text.lower()
    if "no tool call" in low or "unexpected tool" in low:
        return "model_no_tool_call"
    if any(marker in text for marker in _TRANSIENT_HTTP_MARKERS):
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
    """统一受控大模型调用：退避重试 + Bus 前置确定性门禁 + 校验失败回喂修正。"""
    feedback: Optional[str] = None
    rejection: Optional[str] = None
    bus_rejections: List[Dict[str, Any]] = []
    last_kind = "validation"
    last_error = ""
    retried_after_timeout = False
    transient_used = 0
    validation_used = 0
    wait_steps = DEFAULT_BACKOFF_SECONDS if backoff is None else backoff
    bus = bus if bus is not None else default_bus()
    validate_fn = validator if validator is not None else (lambda args: args)

    max_total = max(1, transient_attempts) + max(1, validation_attempts)
    for attempt in range(1, max_total + 1):
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

            rejection_reason = bus.adjudicate(tool_name, arguments, payload) if hasattr(bus, "adjudicate") else None
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
            if transient_used >= transient_attempts:
                break
            _backoff_wait(wait_steps, transient_used)
        except (httpx.HTTPError, TimeoutError, ConnectionError) as exc:
            transient_used += 1
            last_kind, last_error = "transport", f"{type(exc).__name__}: {exc}"
            if transient_used >= transient_attempts:
                break
            _backoff_wait(wait_steps, transient_used)
        except RuntimeError as exc:
            kind = _classify_runtime_error(exc)
            transient_used += 1
            last_error = str(exc)
            if kind == "fatal_model_error":
                last_kind = "model_error"
                break
            last_kind = kind
            if kind == "model_no_tool_call":
                feedback = _NO_TOOL_CALL_HINT
            if transient_used >= transient_attempts:
                break
            _backoff_wait(wait_steps, transient_used)
        except (ValueError, KeyError, TypeError) as exc:
            validation_used += 1
            rejection = str(exc)
            feedback = rejection
            last_kind, last_error = "validation", rejection
            if validation_used >= validation_attempts:
                break

    if last_kind == "timeout":
        message = f"{tool_name} model call timed out after retry ({transient_used} 次): {last_error}"
    elif last_kind == "validation":
        message = f"{tool_name} 连续 {validation_used} 次未通过参数校验：{last_error}"
    else:
        message = f"{tool_name} 连续 {transient_used} 次没有拿到可用返回（{last_kind}）：{last_error}"
    raise WorkflowError(message, context=_fail_context(tool_name, payload, last_kind, retried=transient_used > 1))
