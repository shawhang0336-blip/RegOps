import hashlib
import uuid
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import db_session, run_view, spawn_run_driver
from app.assets.code_sets.readiness import auto_publish_referenced_code_sets
from app.assets.code_sets.service import generation_blockers
from app.assets.metadata_service import metadata_to_spec, publish_table
from app.assets.readiness import evaluate_table, split_preflight_issues
from app.delivery.production_service import build_production_bundle
from app.engine.workflow import _create_run
from app.infra.db_tables import GenerationPlanModel, TableMetadataModel
from app.infra.config import get_settings
from app.infra.contracts import canonical_json
from app.infra.errors import WorkflowError
from app.infra.config import OPERATOR_PLACEHOLDER
from app.infra.db_tables import RunModel, SpecificationModel
from app.infra.config import get_budget

router = APIRouter(prefix="/generation", tags=["generation"])


class GenerationRequest(BaseModel):
    table_ids: List[str]
    approver: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)
    provider: str = "live"
    # 规则/码值不就绪时默认拒绝；确认过风险才允许推进
    accept_warnings: bool = False


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
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return StreamingResponse(
        iter([content]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="production_{run_id[:8]}.zip"'},
    )


def _prepare_generation_run(
    session: Session,
    request: GenerationRequest,
    settings,
) -> Tuple[RunModel, Dict[str, int], Dict[str, List[Dict[str, Any]]], List[str]]:
    """两遍预检：自动定稿并发布关联码表 → 校验就绪度 → 冻结元数据快照并创建 Run。"""
    tables = [session.get(TableMetadataModel, table_id) for table_id in request.table_ids]
    if any(not table for table in tables):
        raise HTTPException(status_code=404, detail="Metadata table not found")

    specs = []
    pending_warnings: List[Dict[str, Any]] = []
    auto_published: Dict[str, List[Dict[str, Any]]] = {}
    auto_finalized: List[str] = []

    # 第一遍：自动定稿 + 取规格 + 自动发布码表（仅 NEEDS_REVIEW 阻断）
    for table in tables:
        if table.status == "NEEDS_REVIEW":
            raise HTTPException(
                status_code=409,
                detail={
                    "stage": "metadata",
                    "table_code": table.table_code,
                    "message": (
                        f"{table.table_code} 还有违反原文的字段问题没处理完，"
                        "先到元数据页把标红的问题解决掉再选它。"
                    ),
                },
            )
        if not (table.status == "PUBLISHED" and table.published_version == table.current_version):
            try:
                finalized = publish_table(session, table, request.approver, table.current_version)
            except ValueError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"stage": "ddl", "table_code": table.table_code, "message": str(exc)},
                ) from exc
            auto_finalized.append(table.table_code)
            if finalized.get("published"):
                auto_published[table.table_code] = finalized["published"]
        try:
            specs.append(metadata_to_spec(session, table))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=f"{table.table_code}: {exc}") from exc
        auto = auto_publish_referenced_code_sets(session, table)
        if auto["published"]:
            auto_published[table.table_code] = auto["published"]

    # 先落库：避免后续 409 回滚丢失已定稿的表与自动发布的码表
    if auto_published or auto_finalized:
        session.commit()

    # 第二遍：就绪度硬/软门禁检查
    for table in tables:
        readiness = evaluate_table(session, table)
        if not readiness["ddl"]["ready"]:
            blocker = readiness["ddl"]["blockers"][0]
            raise HTTPException(
                status_code=409,
                detail={
                    "table_code": table.table_code,
                    "stage": "ddl",
                    "blockers": readiness["ddl"]["blockers"],
                    "message": blocker["message"],
                },
            )
        issues = split_preflight_issues(readiness)
        hard_blockers = issues["hard"]
        if hard_blockers:
            raise HTTPException(
                status_code=409,
                detail={
                    "stage": "code_sets",
                    "table_code": table.table_code,
                    "blockers": hard_blockers,
                    "message": (
                        "码值未就绪，阶段 2 无法装配："
                        + hard_blockers[0]["message"]
                        + "。请先在码表页补齐缺失码值后再选这张表（能自动发布的已经替你发了）。"
                    ),
                },
            )
        soft_warnings = issues["soft"]
        if soft_warnings:
            pending_warnings.append({"table_code": table.table_code, "blockers": soft_warnings})
        table_blockers = generation_blockers(session, table)
        if table_blockers:
            raise HTTPException(status_code=409, detail={"table_code": table.table_code, "blockers": table_blockers})

    if pending_warnings and not request.accept_warnings:
        raise HTTPException(
            status_code=409,
            detail={
                "stage": "preflight",
                "requires_ack": True,
                "message": "有表的规则或码值未就绪，继续推进会导致规则缺失或阶段 2 直接报错",
                "tables": pending_warnings,
            },
        )

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
    run = _create_run(session, snapshot.id, settings, operator=request.approver.strip())
    budget = get_budget(session)
    session.commit()
    return run, budget, auto_published, auto_finalized


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
