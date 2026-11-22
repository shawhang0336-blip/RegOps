import httpx
import pytest

from app.infra.config import Settings
from app.infra.llm import LlmTimeoutError, RealDesignLlm


def _ok_response():
    class Response:
        status_code = 200
        is_error = False

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "offline-test",
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "function": {
                                "name": "assign_generation_plan",
                                "arguments": '{"mandatory_rule_target":1,"expanded_rule_target":0,"golden_count":1,"mutations_per_rule":1,"rationale":"test"}',
                            }
                        }]
                    }
                }],
            }

    return Response()


def test_default_omits_thinking_and_records_call_timing(monkeypatch):
    captured = {}

    def post(url, headers, json, timeout):
        captured["json"] = json
        captured["timeout"] = timeout
        return _ok_response()

    monkeypatch.setattr("app.infra.llm.client.httpx.post", post)
    _, trace = RealDesignLlm(Settings(llm_api_key="offline-test")).assign(
        "assign_generation_plan", "test", {}, timeout=12
    )

    assert "thinking" not in captured["json"]
    assert captured["timeout"] == 12
    assert trace["http_status"] == 200
    assert trace["elapsed_seconds"] >= 0


def test_configured_thinking_is_sent(monkeypatch):
    captured = {}

    def post(url, headers, json, timeout):
        captured["json"] = json
        return _ok_response()

    monkeypatch.setattr("app.infra.llm.client.httpx.post", post)
    RealDesignLlm(Settings(llm_api_key="offline-test", llm_thinking="low")).assign(
        "assign_generation_plan", "test", {}, timeout=12
    )

    assert captured["json"]["thinking"] == {"type": "low"}


def test_assign_defaults_to_configured_timeout(monkeypatch):
    captured = {}

    def post(url, headers, json, timeout):
        captured["timeout"] = timeout
        return _ok_response()

    monkeypatch.setattr("app.infra.llm.client.httpx.post", post)
    RealDesignLlm(Settings(llm_api_key="offline-test", llm_timeout=77)).assign(
        "assign_generation_plan", "test", {}
    )

    assert captured["timeout"] == 77


def test_timeout_raises_llm_timeout_error(monkeypatch):
    def post(url, headers, json, timeout):
        raise httpx.ReadTimeout("read timeout")

    monkeypatch.setattr("app.infra.llm.client.httpx.post", post)
    with pytest.raises(LlmTimeoutError):
        RealDesignLlm(Settings(llm_api_key="offline-test")).assign("assign_generation_plan", "test", {})


def test_missing_tool_call_records_response_diagnostics(monkeypatch):
    class Response:
        status_code = 200
        is_error = False

        def json(self):
            return {
                "id": "diagnostic-test",
                "choices": [{"finish_reason": "content_filter", "message": {"content": ""}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 0},
            }

    monkeypatch.setattr("app.infra.llm.client.httpx.post", lambda *args, **kwargs: Response())
    with pytest.raises(RuntimeError) as error:
        RealDesignLlm(Settings(llm_api_key="offline-test")).assign("assign_generation_plan", "test", {})

    message = str(error.value)
    assert "content_filter" in message
    assert "request_chars" in message
    assert "usage" in message


def test_http_error_includes_model_error_code(monkeypatch):
    class Response:
        status_code = 400
        is_error = True

        def json(self):
            return {"error": {"code": "1210", "message": "thinking setting rejected"}}

    monkeypatch.setattr("app.infra.llm.client.httpx.post", lambda *args, **kwargs: Response())
    with pytest.raises(RuntimeError, match="1210"):
        RealDesignLlm(Settings(llm_api_key="offline-test")).assign("assign_generation_plan", "test", {})


def test_http_502_html_error_triggers_transient_retry_not_json_decode_error(monkeypatch):
    """回归：网关 502/504 返回 HTML 页时不抛 JSONDecodeError（防误判为校验失败），正确走瞬时退避重试。"""
    from app.infra.llm.guard import call_llm_validated

    calls = {"n": 0}

    class Html502Response:
        status_code = 502
        is_error = True
        text = "<html><body>502 Bad Gateway</body></html>"

        def json(self):
            raise ValueError("Expecting value: line 1 column 1")

    def post(url, headers, json, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return Html502Response()
        return _ok_response()

    monkeypatch.setattr("app.infra.llm.client.httpx.post", post)
    args, _ = call_llm_validated(
        RealDesignLlm(Settings(llm_api_key="offline-test")),
        "assign_generation_plan",
        "test",
        {},
        backoff=(0,),
    )
    assert calls["n"] == 2
    assert args["golden_count"] == 1


def test_non_dict_repaired_arguments_raises_value_error_and_rule_prompt_complete(monkeypatch):
    """回归：repair_json 返回非 dict 时抛 ValueError 而非 AttributeError；规则 Prompt 覆盖全部监管算子。"""
    from app.infra.llm.schemas import tool_prompt_preamble

    class BadArgsResponse:
        status_code = 200
        is_error = False

        def json(self):
            return {
                "id": "bad-args",
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "function": {"name": "assign_generation_plan", "arguments": "not-an-object-just-text"}
                        }]
                    }
                }],
            }

    monkeypatch.setattr("app.infra.llm.client.httpx.post", lambda *args, **kwargs: BadArgsResponse())
    with pytest.raises(ValueError, match="JSON 对象"):
        RealDesignLlm(Settings(llm_api_key="offline-test")).assign("assign_generation_plan", "test", {})

    rule_prompt = tool_prompt_preamble("assign_rules")
    for op in ("COND_VALUE", "ARITHMETIC_BALANCE", "UNIQUE_KEY"):
        assert op in rule_prompt


def test_http_200_disguised_error_dict_args_unknown_tool_and_golden_overrides_schema(monkeypatch):
    """回归：HTTP 200 伪装错误/空 choices 走瞬时重试而非 KeyError；预解析 dict 参数正常；未知工具名快失败；assign_rules 声明 golden_overrides。"""
    from app.infra.errors import WorkflowError
    from app.infra.llm.guard import call_llm_validated
    from app.infra.llm.schemas import TOOL_SCHEMAS

    # 1) assign_rules schema 必须声明可选 golden_overrides（跨表外键黄金值对齐）
    assert "golden_overrides" in TOOL_SCHEMAS["assign_rules"]["properties"]
    assert "golden_overrides" not in TOOL_SCHEMAS["assign_rules"]["required"]

    # 2) HTTP 200 返回 {"error": ...} 或 {"choices": []} 触发瞬时退避重试，而非 KeyError/IndexError
    calls = {"n": 0}

    class DisguisedErrorResponse:
        status_code = 200
        is_error = False

        def json(self):
            return {"error": {"code": "429", "message": "upstream rate limit"}}

    class DictArgsResponse:
        status_code = 200
        is_error = False

        def json(self):
            return {
                "id": "dict-args",
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "function": {
                                "name": "assign_generation_plan",
                                "arguments": {
                                    "mandatory_rule_target": 2,
                                    "expanded_rule_target": 1,
                                    "golden_count": 1,
                                    "mutations_per_rule": 1,
                                    "rationale": "pre-parsed dict",
                                },
                            }
                        }]
                    }
                }],
            }

    def post(url, headers, json, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return DisguisedErrorResponse()
        return DictArgsResponse()

    monkeypatch.setattr("app.infra.llm.client.httpx.post", post)
    args, _ = call_llm_validated(
        RealDesignLlm(Settings(llm_api_key="offline-test")),
        "assign_generation_plan",
        "test",
        {},
        backoff=(0,),
    )
    assert calls["n"] == 2
    assert args["mandatory_rule_target"] == 2

    # 3) 未知 tool_name 直接快速失败（fatal_model_error），不白白重试 3 次
    with pytest.raises(WorkflowError) as err:
        call_llm_validated(
            RealDesignLlm(Settings(llm_api_key="offline-test")),
            "ghost_tool_name",
            "test",
            {},
            backoff=(0,),
        )
    assert err.value.context["error_kind"] == "model_error"
    assert err.value.context["retried"] is False
