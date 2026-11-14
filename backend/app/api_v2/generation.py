import hashlib
import uuid
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..api_v2.settings import get_budget
from ..asset_models import GenerationPlanModel, TableMetadataModel
from ..code_service import generation_blockers
from ..config import get_settings
from ..db import SessionLocal
from ..domain import canonical_json
from ..generic_workflow import start_generic_run
from ..metadata_service import metadata_to_spec
from ..models import RunModel, SpecificationModel
from ..production_service import build_production_bundle
from ..workflow import WorkflowError

router = APIRouter(prefix="/generation", tags=["generation"])


class GenerationRequest(BaseModel):
    table_ids: List[str]
    approver: str = Field(min_length=1, max_length=80)
    provider: str = "live"


@router.get("/blockers/{table_id}")
def blockers(table_id: str) -> Dict[str, Any]:
    with SessionLocal() as session:
        table = session.get(TableMetadataModel, table_id)
        if not table:
            raise HTTPException(status_code=404, detail="Metadata table not found")
        return {"table_id": table_id, "blockers": generation_blockers(session, table)}


@router.get("/plans/{run_id}")
def generation_plans(run_id: str) -> List[Dict[str, Any]]:
    with SessionLocal() as session:
        rows = session.query(GenerationPlanModel).filter(GenerationPlanModel.run_id == run_id).all()
        return [
            {"table_code": row.table_code, "plan": row.plan_json, "trace": row.trace_json, "created_at": row.created_at.isoformat()}
            for row in rows
        ]


@router.get("/runs/{run_id}/production-bundle")
def download_production_bundle(run_id: str):
    with SessionLocal() as session:
        run = session.get(RunModel, run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Generation run not found")
        try:
            _, content = build_production_bundle(session, run, get_settings().blob_dir)
        except WorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return StreamingResponse(
            iter([content]),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="production_{run_id[:8]}.zip"'},
        )


@router.post("/runs", status_code=201)
def create_generation(request: GenerationRequest) -> Dict[str, Any]:
    if request.provider != "live":
        raise HTTPException(status_code=422, detail="Production generation requires provider=live")
    if not request.table_ids:
        raise HTTPException(status_code=422, detail="Select at least one published table")
    with SessionLocal() as session:
        tables = [session.get(TableMetadataModel, table_id) for table_id in request.table_ids]
        if any(not table for table in tables):
            raise HTTPException(status_code=404, detail="Metadata table not found")
        specs = []
        for table in tables:
            try:
                specs.append(metadata_to_spec(session, table))
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=f"{table.table_code}: {exc}") from exc
            table_blockers = generation_blockers(session, table)
            if table_blockers:
                raise HTTPException(status_code=409, detail={"table_code": table.table_code, "blockers": table_blockers})
        snapshot_material = [
            {"table_id": table.id, "version": table.published_version, "spec_sha256": spec.source_sha256}
            for table, spec in zip(tables, specs)
        ]
        digest = hashlib.sha256(canonical_json(snapshot_material).encode()).hexdigest()
        snapshot = SpecificationModel(
            id=str(uuid.uuid4()),
            version="METADATA_PUBLISHED",
            table_code="METADATA_SNAPSHOT",
            source_name="published-metadata",
            source_sha256=digest,
            content_json={
                "selection_confirmed": True,
                "table_name": f"已发布元数据 · {len(specs)} 张表",
                "confirmation": {"approver": request.approver, "tables": snapshot_material},
                "tables": [spec.model_dump() for spec in specs],
            },
        )
        session.add(snapshot)
        session.flush()
        run = start_generic_run(
            session,
            snapshot.id,
            get_settings(),
            generation_budget=get_budget(session),
        )
        from ..main import run_view

        return run_view(session, run)
