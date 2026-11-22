"""受控 Tool-Call 大模型 HTTP 客户端与并发限流（基础设施层 Layer 0）。

职责边界：
- 仅负责底层 HTTP 传输、TCP/TLS 连接池复用、并发信号量控制、非 JSON 网关错误页（502/503/504）安全包装与参数 JSON 修复；
- 业务 Tool JSON Schema 与作用域 Prompt 契约已独立拆至 `app.infra.llm.schemas`。
"""

import json
import threading
import time
from typing import Any, Dict, Optional, Tuple

import httpx
from json_repair import repair_json

from app.infra.config import Settings
from app.infra.llm.schemas import TOOL_SCHEMAS, tool_prompt_preamble

# LLM 并发闸门：默认值只是兜底，实际宽度由系统设置的 max_llm_concurrency 决定，
# 生成批次启动时通过 configure_llm_concurrency 调整。
_semaphore_lock = threading.Lock()
_llm_semaphore = threading.BoundedSemaphore(4)
_llm_semaphore_limit = 4

# 复用进程级 httpx.Client 连接池（避免每次请求重新 TCP/TLS 握手），同时兼容单测对 httpx.post 的 monkeypatch
_ORIGINAL_HTTPX_POST = httpx.post
_http_client_lock = threading.Lock()
_http_client: Optional[httpx.Client] = None


def _get_http_client() -> httpx.Client:
    global _http_client
    with _http_client_lock:
        if _http_client is None or _http_client.is_closed:
            _http_client = httpx.Client(
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16)
            )
        return _http_client


def close_http_client() -> None:
    """应用关闭时释放共享 HTTP 连接池。"""
    global _http_client
    with _http_client_lock:
        client = _http_client
        _http_client = None
    if client is not None and not client.is_closed:
        client.close()


def configure_llm_concurrency(limit: int) -> None:
    global _llm_semaphore, _llm_semaphore_limit
    limit = max(1, int(limit))
    with _semaphore_lock:
        if limit != _llm_semaphore_limit:
            _llm_semaphore_limit = limit
            _llm_semaphore = threading.BoundedSemaphore(limit)


def llm_call_semaphore() -> threading.BoundedSemaphore:
    return _llm_semaphore


class LlmTimeoutError(RuntimeError):
    """模型调用超时。重试是安全的：模型侧很可能根本没处理到该请求。

    与参数校验失败（RuleValidationError）不同，超时不意味着参数有问题，
    因此重试时不应把超时信息作为 feedback 回喂给模型。
    """


class RealDesignLlm:
    def __init__(self, settings: Settings) -> None:
        if not settings.llm_api_key:
            raise RuntimeError("LLM_API_KEY is required")
        self.settings = settings

    def assign(
        self,
        tool_name: str,
        instruction: str,
        payload: Dict[str, Any],
        feedback: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if timeout is None:
            timeout = self.settings.llm_timeout
        if tool_name not in TOOL_SCHEMAS:
            raise RuntimeError(f"Unknown LLM tool: {tool_name}")
        schema = TOOL_SCHEMAS[tool_name]
        prompt = (
            f"{tool_prompt_preamble(tool_name)}\n"
            f"任务：{instruction}\n输入：{json.dumps(payload, ensure_ascii=False)}"
        )
        if feedback:
            prompt += f"\n上次参数被确定性门禁拒绝，请修正：{feedback}"
        started_at = time.monotonic()
        request_json = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": "只进行工具参数赋值，不输出自由文本。"},
                {"role": "user", "content": prompt},
            ],
            "tools": [{"type": "function", "function": {"name": tool_name, "description": instruction, "parameters": schema}}],
            "temperature": 0,
            "max_tokens": 8192,
        }
        if self.settings.llm_tool_choice == "forced":
            request_json["tool_choice"] = {"type": "function", "function": {"name": tool_name}}
        else:
            request_json["tool_choice"] = "auto"
        if self.settings.llm_thinking:
            request_json["thinking"] = {"type": self.settings.llm_thinking}
        url = f"{self.settings.llm_base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        try:
            with llm_call_semaphore():
                if httpx.post is _ORIGINAL_HTTPX_POST:
                    response = _get_http_client().post(url, headers=headers, json=request_json, timeout=timeout)
                else:
                    response = httpx.post(url, headers=headers, json=request_json, timeout=timeout)
        except httpx.TimeoutException as exc:
            elapsed = round(time.monotonic() - started_at, 2)
            raise LlmTimeoutError(f"{type(exc).__name__} after {elapsed}s; request_chars={len(prompt)}") from exc
        except httpx.HTTPError as exc:
            elapsed = round(time.monotonic() - started_at, 2)
            raise RuntimeError(f"{type(exc).__name__} after {elapsed}s; request_chars={len(prompt)}") from exc
        if response.is_error:
            elapsed = round(time.monotonic() - started_at, 2)
            # 网关/反代 502/503/504 常返回 HTML 或纯文本而非 JSON，绝不能抛 JSONDecodeError 误导守卫层走校验分支
            try:
                parsed_err = response.json()
                error = parsed_err.get("error", {}) if isinstance(parsed_err, dict) else {}
                code = error.get("code", "")
                msg = error.get("message", "")
            except Exception:  # noqa: BLE001
                code = ""
                raw_text = (getattr(response, "text", "") or "").strip().replace("\n", " ")
                msg = raw_text[:240] or "non-JSON error body"
            raise RuntimeError(
                f"LLM HTTP {response.status_code} {code}: {msg}; elapsed={elapsed}s"
            )
        try:
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            elapsed = round(time.monotonic() - started_at, 2)
            raise RuntimeError(f"LLM HTTP 502 invalid JSON response: {exc}; elapsed={elapsed}s") from exc
        if not isinstance(body, dict):
            elapsed = round(time.monotonic() - started_at, 2)
            raise RuntimeError(f"LLM HTTP 502 non-object JSON response; elapsed={elapsed}s")
        # 部分企业网关/聚合代理在限流或上游异常时返回 HTTP 200 + {"error": ...} 或空 choices
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            elapsed = round(time.monotonic() - started_at, 2)
            err = body.get("error") if isinstance(body.get("error"), dict) else {}
            if err:
                raise RuntimeError(
                    f"LLM HTTP 502 upstream error {err.get('code', '')}: {err.get('message', '')}; elapsed={elapsed}s"
                )
            raise RuntimeError(f"LLM HTTP 502 empty choices in response; elapsed={elapsed}s")
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        calls = message.get("tool_calls") or []
        if not calls:
            diagnostic = {
                "request_id": body.get("id"),
                "finish_reason": choice.get("finish_reason"),
                "content_chars": len(message.get("content") or ""),
                "reasoning_chars": len(message.get("reasoning_content") or ""),
                "request_chars": len(prompt),
                "usage": body.get("usage"),
                "elapsed_seconds": round(time.monotonic() - started_at, 2),
            }
            raise RuntimeError(f"Model returned no tool call: {json.dumps(diagnostic, ensure_ascii=False)}")
        arguments: Dict[str, Any] = {}
        json_repaired = False
        for call in calls:
            fn_obj = call.get("function") if isinstance(call, dict) else None
            if not isinstance(fn_obj, dict) or fn_obj.get("name") != tool_name:
                raise RuntimeError("Model returned an unexpected tool")
            raw_arguments = fn_obj.get("arguments")
            if isinstance(raw_arguments, dict):
                current = raw_arguments
            elif isinstance(raw_arguments, str):
                try:
                    current = json.loads(raw_arguments)
                except json.JSONDecodeError:
                    current = repair_json(raw_arguments, return_objects=True)
                    json_repaired = True
            else:
                raise ValueError(f"工具 {tool_name} 返回的参数不是有效 JSON 字符串或对象")
            if not isinstance(current, dict):
                raise ValueError(f"工具 {tool_name} 返回的参数无法解析为 JSON 对象")
            for key, value in current.items():
                if isinstance(value, list):
                    arguments.setdefault(key, []).extend(value)
                else:
                    arguments[key] = value
        trace = {
            "provider": self.settings.llm_provider,
            "model": self.settings.llm_model,
            "request_id": body.get("id"),
            "tool_name": tool_name,
            "tool_call_count": len(calls),
            "json_repaired": json_repaired,
            "http_status": response.status_code,
            "request_chars": len(prompt),
            "elapsed_seconds": round(time.monotonic() - started_at, 2),
            "usage": body.get("usage"),
            "arguments": arguments,
        }
        return arguments, trace
