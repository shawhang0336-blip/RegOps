"""规则生成装配与生产包下载路由（`/api/generation/*`）：薄路由，预检委托给 `generation_service.py`。"""

import re
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.common import db_session, run_view, spawn_run_driver
from app.api.generation.generation_service import prepare_generation_run
from app.catalog.code_sets.linking import generation_blockers
from app.delivery.production_service import build_production_bundle
from app.infra.config import OPERATOR_PLACEHOLDER, get_settings
from app.infra.db_tables import GenerationPlanModel, RunModel, TableMetadataModel
from app.infra.errors import WorkflowError

router = APIRouter(prefix="/generation", tags=["generation"])


class GenerationRequest(BaseModel):
    table_ids: List[str]
    approver: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)
    provider: str = "live"
    accept_warnings: bool = False


def _prepare_generation_run(
    session: Session,
    request: GenerationRequest,
    settings,
) -> Tuple[RunModel, Dict[str, int], Dict[str, List[Dict[str, Any]]], List[str]]:
    return prepare_generation_run(
        session,
        table_ids=request.table_ids,
        approver=request.approver,
        accept_warnings=request.accept_warnings,
        settings=settings,
    )


@router.get("/blockers/{table_id}")
def blockers(table_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    table = session.get(TableMetadataModel, table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Metadata table not found")
    return {"table_id": table_id, "blockers": generation_blockers(session, table)}


@router.get("/plans/{run_id}")
def generation_plans(run_id: str, session: Session = Depends(db_session)) -> List[Dict[str, Any]]:
    rows = session.query(GenerationPlanModel).filter(GenerationPlanModel.run_id == run_id).all()
    return [
        {
            "table_code": row.table_code,
            "plan": row.plan_json,
            "trace": row.trace_json,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


@router.get("/runs/{run_id}/production-bundle")
def download_production_bundle(run_id: str, session: Session = Depends(db_session)):
    run = session.get(RunModel, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Generation run not found")
    try:
        _, content = build_production_bundle(session, run, get_settings().blob_dir)
    except WorkflowError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    safe_stem = re.sub(r"[^A-Za-z0-9_-]", "", run_id[:8]) or "bundle"
    return StreamingResponse(
        iter([content]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="production_{safe_stem}.zip"'},
    )


@router.post("/runs", status_code=202)
def create_generation(request: GenerationRequest, session: Session = Depends(db_session)) -> Dict[str, Any]:
    if request.provider != "live":
        raise HTTPException(status_code=422, detail="Production generation requires provider=live")
    if not request.table_ids:
        raise HTTPException(status_code=422, detail="Select at least one published table")
    settings = get_settings()
    run, budget, auto_published, auto_finalized = _prepare_generation_run(session, request, settings)
    payload = run_view(session, run)
    if auto_published:
        payload["auto_published_code_sets"] = auto_published
    if auto_finalized:
        payload["auto_finalized_tables"] = auto_finalized
    spawn_run_driver(run.id, settings, generation_budget=budget, allowed_statuses=("RUNNING",))
    return payload
