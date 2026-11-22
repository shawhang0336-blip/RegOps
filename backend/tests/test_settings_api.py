"""系统设置接口回归测试：验证异常透传、只读环境变量保护与执行预算读取。"""

import pytest
from fastapi import HTTPException

from app.api.v2 import settings as settings_api
from app.infra.config import Settings
from app.infra.config import DEFAULT_BUDGET, get_budget
from .helpers import make_session


def test_connection_preserves_422_when_oracle_or_llm_unconfigured(monkeypatch):
    session = make_session()
    monkeypatch.setattr(
        settings_api,
        "get_settings",
        lambda: Settings(oracle_sandbox_url=None, llm_api_key=None),
    )

    with pytest.raises(HTTPException) as oracle_exc:
        settings_api.test_connection("oracle", session=session)
    assert oracle_exc.value.status_code == 422

    with pytest.raises(HTTPException) as llm_exc:
        settings_api.test_connection("llm", session=session)
    assert llm_exc.value.status_code == 422


def test_save_setting_rejects_env_managed_keys_and_persists_budget():
    session = make_session()

    for readonly_key in ("mysql", "oracle", "llm"):
        with pytest.raises(HTTPException) as exc:
            settings_api.save_setting(readonly_key, settings_api.SettingRequest(value={"x": 1}), session=session)
        assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as unknown_exc:
        settings_api.save_setting("not_a_key", settings_api.SettingRequest(value={}), session=session)
    assert unknown_exc.value.status_code == 404

    assert get_budget(session) == DEFAULT_BUDGET
    result = settings_api.save_setting(
        "execution_budget",
        settings_api.SettingRequest(value={"max_retries": 5, "max_llm_concurrency": 6}),
        session=session,
    )
    assert result == {"key": "execution_budget", "saved": True}
    updated = get_budget(session)
    assert updated["max_retries"] == 5
    assert updated["max_llm_concurrency"] == 6
    assert updated["max_rules_per_table"] == DEFAULT_BUDGET["max_rules_per_table"]
    assert updated["max_llm_calls"] == DEFAULT_BUDGET["max_llm_calls"]
    assert updated["max_total_tokens"] == DEFAULT_BUDGET["max_total_tokens"]
    assert updated["max_elapsed_seconds"] == DEFAULT_BUDGET["max_elapsed_seconds"]

    # BudgetGuard 阈值支持设为 0（仅统计不熔断）
    settings_api.save_setting(
        "execution_budget",
        settings_api.SettingRequest(value={"max_llm_calls": 0, "max_total_tokens": 500000}),
        session=session,
    )
    updated2 = get_budget(session)
    assert updated2["max_llm_calls"] == 0
    assert updated2["max_total_tokens"] == 500000
