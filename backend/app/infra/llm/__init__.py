"""受控大模型子包（Infra Layer 0）：HTTP 传输客户端 + 工具契约 + 容错调用守卫。

职责边界：
- `client.py`：OpenAI 兼容 HTTP 客户端（`RealDesignLlm`）、`httpx.Client` 连接池复用、并发信号量与 502/504 非 JSON 容错；
- `schemas.py`：12 个受控工具 JSON Schema、`spec_payload` 与作用域前置 Prompt 契约；
- `guard.py`：跨阶段受控大模型调用守卫（分轨退避重试、上下文作用域透传与鸭子类型 Bus 门禁回喂）。
"""

from .client import (
    LlmTimeoutError,
    RealDesignLlm,
    TokenBucketRateLimiter,
    close_http_client,
    configure_llm_concurrency,
    configure_llm_rate_limit,
    llm_call_semaphore,
    llm_rate_limiter,
    notify_llm_rate_limited,
    set_cancellation_provider,
)
from .guard import (
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_TRANSIENT_ATTEMPTS,
    DEFAULT_VALIDATION_ATTEMPTS,
    ERROR_SUGGESTIONS,
    STAGE_BY_TOOL,
    backoff_wait,
    call_llm_validated,
    current_cancellation,
    current_llm_bus,
    fail_context,
    llm_execution_scope,
    locate_payload,
    submit_with_context,
)
from .schemas import (
    CODE_SET_PROPERTIES,
    CODE_VALUE_PROPERTIES,
    FIELD_PROPERTIES,
    METADATA_FIELD_PROPERTIES,
    METADATA_TABLE_PROPERTIES,
    RULE_PROPERTIES,
    TOOL_SCHEMAS,
    spec_payload,
    tool_prompt_preamble,
)

__all__ = [
    "CODE_SET_PROPERTIES",
    "CODE_VALUE_PROPERTIES",
    "DEFAULT_BACKOFF_SECONDS",
    "DEFAULT_TRANSIENT_ATTEMPTS",
    "DEFAULT_VALIDATION_ATTEMPTS",
    "ERROR_SUGGESTIONS",
    "FIELD_PROPERTIES",
    "LlmTimeoutError",
    "METADATA_FIELD_PROPERTIES",
    "METADATA_TABLE_PROPERTIES",
    "RULE_PROPERTIES",
    "RealDesignLlm",
    "STAGE_BY_TOOL",
    "TOOL_SCHEMAS",
    "TokenBucketRateLimiter",
    "backoff_wait",
    "call_llm_validated",
    "close_http_client",
    "configure_llm_concurrency",
    "configure_llm_rate_limit",
    "current_cancellation",
    "current_llm_bus",
    "fail_context",
    "llm_call_semaphore",
    "llm_execution_scope",
    "llm_rate_limiter",
    "locate_payload",
    "notify_llm_rate_limited",
    "set_cancellation_provider",
    "spec_payload",
    "submit_with_context",
    "tool_prompt_preamble",
]
