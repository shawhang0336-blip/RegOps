import pytest

from app.config import Settings
from app.generic_ai import RealDesignLlm


def test_glm_uses_low_thinking_and_records_call_timing(monkeypatch):
    captured = {}

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

    def post(url, headers, json, timeout):
        captured["json"] = json
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("app.generic_ai.httpx.post", post)
    _, trace = RealDesignLlm(Settings(llm_api_key="offline-test")).assign(
        "assign_generation_plan", "test", {}, timeout=12
    )

    assert captured["json"]["thinking"] == {"type": "low"}
    assert captured["timeout"] == 12
    assert trace["http_status"] == 200
    assert trace["elapsed_seconds"] >= 0


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

    monkeypatch.setattr("app.generic_ai.httpx.post", lambda *args, **kwargs: Response())
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

    monkeypatch.setattr("app.generic_ai.httpx.post", lambda *args, **kwargs: Response())
    with pytest.raises(RuntimeError, match="1210"):
        RealDesignLlm(Settings(llm_api_key="offline-test")).assign("assign_generation_plan", "test", {})
