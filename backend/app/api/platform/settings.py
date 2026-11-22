from typing import Any, Dict, Optional, Tuple

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.common import db_session
from app.infra.db_tables import SystemSettingModel
from app.infra.config import get_settings
from app.infra.llm import RealDesignLlm
from app.infra.config import DEFAULT_BUDGET, get_budget

router = APIRouter(prefix="/settings", tags=["settings"])

EDITABLE_SETTINGS = {"execution_budget"}
READONLY_ENV_SETTINGS = {"mysql", "oracle", "llm"}

BUDGET_BOUNDS: Dict[str, Tuple[int, int]] = {
    "max_rules_per_table": (1, 500),
    "max_cases_per_table": (1, 20000),
    "max_retries": (0, 20),
    "max_llm_concurrency": (1, 32),
    "max_llm_calls": (0, 10000),
    "max_total_tokens": (0, 100_000_000),
    "max_elapsed_seconds": (0, 86400),
}


def _validate_execution_budget(raw: Dict[str, Any]) -> Dict[str, int]:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="execution_budget must be a JSON object")
    unknown = sorted(set(raw.keys()) - set(BUDGET_BOUNDS.keys()))
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown budget fields: {', '.join(unknown)}")
    validated: Dict[str, int] = {}
    for field_name, val in raw.items():
        if val is None:
            continue
        if isinstance(val, bool) or not isinstance(val, int):
            raise HTTPException(status_code=422, detail=f"Budget field '{field_name}' must be an integer")
        lo, hi = BUDGET_BOUNDS[field_name]
        if val < lo or val > hi:
            raise HTTPException(
                status_code=422,
                detail=f"Budget field '{field_name}' must be between {lo} and {hi}",
            )
        validated[field_name] = val
    return validated


class SettingRequest(BaseModel):
    value: Dict[str, Any]
    secret_reference: Optional[str] = None


@router.get("")
def settings_view(session: Session = Depends(db_session)) -> Dict[str, Any]:
    settings = get_settings()
    stored = {
        row.key: {"value": row.value_json, "secret_reference": row.secret_reference}
        for row in session.query(SystemSettingModel).all()
    }
    budget = get_budget(session)
    return {
        "mysql": {"configured": bool(settings.database_url), "driver": settings.database_url.split(":", 1)[0]},
        "oracle": {"configured": bool(settings.oracle_sandbox_url)},
        "llm": {
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "credential_configured": bool(settings.llm_api_key),
        },
        "execution_budget": budget,
        "stored": stored,
    }


@router.put("/{key}")
def save_setting(key: str, request: SettingRequest, session: Session = Depends(db_session)) -> Dict[str, Any]:
    if key in READONLY_ENV_SETTINGS:
        raise HTTPException(
            status_code=422,
            detail=f"Setting '{key}' is managed via .env and cannot be modified through the API",
        )
    if key not in EDITABLE_SETTINGS:
        raise HTTPException(status_code=404, detail="Unknown setting")
    value_to_save: Dict[str, Any] = (
        _validate_execution_budget(request.value) if key == "execution_budget" else request.value
    )
    row = session.get(SystemSettingModel, key)
    if not row:
        row = SystemSettingModel(key=key, value_json=value_to_save, secret_reference=request.secret_reference)
        session.add(row)
    else:
        row.value_json = value_to_save
        row.secret_reference = request.secret_reference
    session.commit()
    return {"key": key, "saved": True}


@router.post("/test/{kind}")
def test_connection(kind: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    settings = get_settings()
    try:
        if kind == "mysql":
            row = session.execute(text("SELECT VERSION(), CURRENT_USER(), @@character_set_database")).one()
            return {"kind": kind, "ok": True, "version": row[0], "user": row[1], "charset": row[2]}
        if kind == "oracle":
            if not settings.oracle_sandbox_url:
                raise HTTPException(status_code=422, detail="Oracle sandbox is not configured")
            from sqlalchemy import create_engine

            engine = create_engine(settings.oracle_sandbox_url)
            try:
                with engine.connect() as connection:
                    row = connection.execute(text("SELECT USER, SYS_CONTEXT('USERENV','CON_NAME') FROM dual")).one()
            finally:
                engine.dispose()
            return {"kind": kind, "ok": True, "user": row[0], "container": row[1]}
        if kind == "llm":
            if not settings.llm_api_key:
                raise HTTPException(status_code=422, detail="LLM credential is not configured")
            _, trace = RealDesignLlm(settings).assign(
                "assign_generation_plan",
                "执行连接健康检查，返回最小的合法生成计划。",
                {"budget": DEFAULT_BUDGET},
                timeout=60,
            )
            return {
                "kind": kind,
                "ok": True,
                "request_id": trace.get("request_id"),
                "model": trace.get("model"),
                "elapsed_seconds": trace.get("elapsed_seconds"),
            }
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"LLM connection failed: {type(exc).__name__}") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Connection failed: {type(exc).__name__}") from exc
    raise HTTPException(status_code=404, detail="Unknown connection kind")
