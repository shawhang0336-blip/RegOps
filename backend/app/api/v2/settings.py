from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.infra.asset_models import SystemSettingModel
from app.infra.config import Settings, get_settings
from app.infra.db import SessionLocal
from app.engine.llm import RealDesignLlm

router = APIRouter(prefix="/settings", tags=["settings"])

DEFAULT_BUDGET = {
    "max_rules_per_table": 50,
    "max_cases_per_table": 1000,
    "max_retries": 3,
    "max_llm_concurrency": 4,
}


class SettingRequest(BaseModel):
    value: Dict[str, Any]
    secret_reference: Optional[str] = None


def get_budget(session: Session) -> Dict[str, int]:
    row = session.get(SystemSettingModel, "execution_budget")
    return {**DEFAULT_BUDGET, **(row.value_json if row else {})}


@router.get("")
def settings_view() -> Dict[str, Any]:
    settings = get_settings()
    with SessionLocal() as session:
        stored = {row.key: {"value": row.value_json, "secret_reference": row.secret_reference} for row in session.query(SystemSettingModel).all()}
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
def save_setting(key: str, request: SettingRequest) -> Dict[str, Any]:
    if key not in {"execution_budget", "mysql", "oracle", "llm"}:
        raise HTTPException(status_code=404, detail="Unknown setting")
    with SessionLocal() as session:
        row = session.get(SystemSettingModel, key)
        if not row:
            row = SystemSettingModel(key=key, value_json=request.value, secret_reference=request.secret_reference)
            session.add(row)
        else:
            row.value_json = request.value
            row.secret_reference = request.secret_reference
        session.commit()
    return {"key": key, "saved": True}


@router.post("/test/{kind}")
def test_connection(kind: str) -> Dict[str, Any]:
    settings = get_settings()
    try:
        if kind == "mysql":
            with SessionLocal() as session:
                row = session.execute(text("SELECT VERSION(), CURRENT_USER(), @@character_set_database")).one()
            return {"kind": kind, "ok": True, "version": row[0], "user": row[1], "charset": row[2]}
        if kind == "oracle":
            if not settings.oracle_sandbox_url:
                raise HTTPException(status_code=422, detail="Oracle sandbox is not configured")
            from sqlalchemy import create_engine

            engine = create_engine(settings.oracle_sandbox_url)
            with engine.connect() as connection:
                row = connection.execute(text("SELECT USER, SYS_CONTEXT('USERENV','CON_NAME') FROM dual")).one()
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
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"LLM connection failed: {type(exc).__name__}") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Connection failed: {type(exc).__name__}") from exc
    raise HTTPException(status_code=404, detail="Unknown connection kind")
