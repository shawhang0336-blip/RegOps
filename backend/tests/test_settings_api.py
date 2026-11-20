"""系统设置接口回归测试：验证异常透传、只读环境变量保护与执行预算读取。"""

import pytest
from fastapi import HTTPException

from app.api.v2 import settings as settings_api
from app.infra.config import Settings
from app.infra.settings_store import DEFAULT_BUDGET, get_budget
from .helpers import make_session


def test_connection_preserves_422_when_oracle_or_llm_unconfigured(monkeypatch):
    monkeypatch.setattr(
        settings_api,
        "get_settings",
        lambda: Settings(oracle_sandbox_url=None, llm_api_key=None),
    )

    with pytest.raises(HTTPException) as oracle_exc:
        settings_api.test_connection("oracle")
    assert oracle_exc.value.status_code == 422

    with pytest.raises(HTTPException) as llm_exc:
        settings_api.test_connection("llm")
    assert llm_exc.value.status_code == 422


def test_save_setting_rejects_env_managed_keys_and_persists_budget(monkeypatch):
    session = make_session()

    class _SessionCtx:
        def __enter__(self):
            return session

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(settings_api, "SessionLocal", lambda: _SessionCtx())

    for readonly_key in ("mysql", "oracle", "llm"):
        with pytest.raises(HTTPException) as exc:
            settings_api.save_setting(readonly_key, settings_api.SettingRequest(value={"x": 1}))
        assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as unknown_exc:
        settings_api.save_setting("not_a_key", settings_api.SettingRequest(value={}))
    assert unknown_exc.value.status_code == 404

    assert get_budget(session) == DEFAULT_BUDGET
    result = settings_api.save_setting(
        "execution_budget",
        settings_api.SettingRequest(value={"max_retries": 5, "max_llm_concurrency": 6}),
    )
    assert result == {"key": "execution_budget", "saved": True}
    updated = get_budget(session)
    assert updated["max_retries"] == 5
    assert updated["max_llm_concurrency"] == 6
    assert updated["max_rules_per_table"] == DEFAULT_BUDGET["max_rules_per_table"]
