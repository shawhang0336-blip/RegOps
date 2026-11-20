"""系统健康检查、Blob 读取与业务库只读抽样路由（`/api/*`）。"""

import json
from typing import Dict, List

from fastapi import APIRouter, HTTPException, Query

from app.delivery.business_db import BusinessDatabaseError, ReadOnlyBusinessDatabase
from app.infra.config import get_settings
from app.kernel.blob_store import LocalBlobStore

router = APIRouter(tags=["system"])


@router.get("/api/health")
def health() -> Dict:
    settings = get_settings()
    return {
        "status": "ok",
        "database": settings.database_url.split(":", 1)[0],
        "llm_provider": settings.llm_provider,
        "business_database_configured": bool(settings.business_database_url),
    }


@router.get("/api/blobs/{digest}")
def get_blob(digest: str) -> Dict:
    settings = get_settings()
    try:
        content = LocalBlobStore(settings.blob_dir).get(f"blob://{digest}").decode("utf-8")
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="Blob not found") from exc
    try:
        return {"digest": digest, "content": content, "json": json.loads(content)}
    except json.JSONDecodeError:
        return {"digest": digest, "content": content, "json": None}


@router.get("/api/business/preview")
def business_preview(table: str = Query(min_length=1), limit: int = Query(default=20, ge=1, le=100)) -> List[Dict]:
    settings = get_settings()
    try:
        return ReadOnlyBusinessDatabase(settings).preview(table, limit)
    except BusinessDatabaseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Business database query failed: {type(exc).__name__}") from exc
