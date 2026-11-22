"""监管规范文档接入、切块、解析进度与版本对比路由（`/api/documents/*`）。"""

import hashlib
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.catalog import documents_service as svc
from app.api.catalog.documents_service import ALLOWED_BLOCK_TYPES, FILE_TYPES, document_summary
from app.api.common import db_session
from app.catalog.extraction import extract_parallel_for_blocks
from app.catalog.pipeline_run import (
    cancel_document_run,
    document_progress_view,
    list_document_runs,
    start_document_extract_run,
    start_document_prepare_run,
)
from app.catalog.version_diff import diff_documents
from app.infra.config import get_settings
from app.infra.db import SessionLocal
from app.infra.db_tables import DocumentArchitectureModel, SourceDocumentModel
from app.infra.errors import WorkflowError
from app.infra.task_runner import task_runner
from app.ingestion.parsers.extractors import extract_document
from app.ingestion.structure_service import (
    analyze_document_structure,
    inspect_document_overview,
    latest_document_architecture,
    reextract_document_blocks,
)

router = APIRouter(prefix="/documents", tags=["documents"])


class ParallelExtractRequest(BaseModel):
    block_ids: List[str]
    targets: Optional[List[str]] = None


class CancelDocumentRequest(BaseModel):
    reason: Optional[str] = None


def store_initial_blocks(document_id: str) -> SourceDocumentModel:
    return svc.store_initial_blocks(
        document_id,
        session_factory=SessionLocal,
        get_settings_fn=get_settings,
        extract_document_fn=extract_document,
    )


def ingest_uploaded_file(filename: str, content: bytes, *, prepare: bool = True) -> SourceDocumentModel:
    return svc.ingest_uploaded_file(
        filename,
        content,
        prepare=prepare,
        session_factory=SessionLocal,
        get_settings_fn=get_settings,
        extract_document_fn=extract_document,
    )


@router.post("", status_code=201)
async def upload_document(file: UploadFile = File(...)) -> Dict[str, Any]:
    """保存文件后立即返回，结构检查与切块交由统一的 Ingestion Run（kernel.run）驱动。"""
    content = await file.read()
    document, reimport_deleted = svc.ingest_uploaded_file_with_flag(
        file.filename,
        content,
        prepare=False,
        session_factory=SessionLocal,
        get_settings_fn=get_settings,
        extract_document_fn=extract_document,
    )
    already_ready = (
        not reimport_deleted
        and document.status in {"UPLOADED", "EXTRACTED", "EXTRACTION_PARTIAL", "STRUCTURE_REVIEWED"}
        and bool(document.block_count)
    )
    if already_ready:
        return {**document_summary(document), "processing": False}
    run_id = start_document_prepare_run(
        document.id,
        document.file_name,
        get_settings(),
        reimport_deleted=reimport_deleted,
        initial_bytes=len(content),
        session_factory=SessionLocal,
        runner=task_runner(),
        inspect_overview_fn=inspect_document_overview,
        reextract_blocks_fn=reextract_document_blocks,
        store_blocks_fn=store_initial_blocks,
    )
    return {**document_summary(document), "processing": True, "run_id": run_id}


@router.get("")
def list_documents(session: Session = Depends(db_session)) -> list:
    return svc.list_active_documents(session)


@router.delete("/{document_id}")
def delete_document(document_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    return svc.soft_delete_document(session, document_id)


@router.get("/diff")
def compare_documents(
    base_id: str,
    target_id: str,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    """跨版本监管制度文档差异对比与影响面分析（返回受影响表清单用于增量重生成）。"""
    try:
        return diff_documents(session, base_id, target_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{document_id}")
def document_detail(document_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    return svc.get_document_detail(session, document_id)


@router.get("/{document_id}/progress")
def document_progress(document_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    """前端进度视图：优先实时内存态，重启后从不可变 EventLog 折叠态无损还原。"""
    try:
        return document_progress_view(session, document_id)
    except WorkflowError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{document_id}/runs")
def document_runs(document_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    """查看指定文档历次 Ingestion Run 审计记录（含 BudgetGuard 成本与哈希链校验）。"""
    if not session.get(SourceDocumentModel, document_id):
        raise HTTPException(status_code=404, detail="Document not found")
    items = list_document_runs(session, document_id)
    return {"document_id": document_id, "count": len(items), "runs": items}


@router.post("/{document_id}/cancel")
def cancel_document(
    document_id: str,
    request: Optional[CancelDocumentRequest] = None,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    """协作式中止正在运行的文档解析任务（经由 kernel.run 取消令牌）。"""
    reason = (request.reason.strip() if request and request.reason else "") or "文档解析已由操作员中止"
    try:
        return cancel_document_run(session, document_id, reason=reason)
    except WorkflowError as exc:
        status_code = 404 if "not found" in str(exc).lower() else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@router.post("/{document_id}/prepare", status_code=202)
def prepare_document(document_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    document = session.get(SourceDocumentModel, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    run_id = start_document_prepare_run(
        document_id,
        document.file_name,
        get_settings(),
        session=session,
        reimport_deleted=True,
        session_factory=SessionLocal,
        runner=task_runner(),
        inspect_overview_fn=inspect_document_overview,
        reextract_blocks_fn=reextract_document_blocks,
        store_blocks_fn=store_initial_blocks,
    )
    return {"status": "PREPARING", "document_id": document_id, "run_id": run_id}


@router.post("/{document_id}/reextract")
def reextract_blocks(document_id: str) -> Dict[str, Any]:
    try:
        count = reextract_document_blocks(document_id, get_settings())
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"document_id": document_id, "block_count": count, "status": "UPLOADED"}


@router.post("/{document_id}/analyze-structure")
def analyze_structure(document_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    try:
        architecture_id = analyze_document_structure(document_id, get_settings())
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.expire_all()
    architecture = session.get(DocumentArchitectureModel, architecture_id)
    return {
        "id": architecture.id,
        "status": architecture.status,
        "plan": architecture.plan_json,
        "warnings": architecture.warnings_json,
        "trace": architecture.trace_json,
    }


@router.post("/{document_id}/extract-parallel", status_code=202)
def extract_document_parallel(
    document_id: str,
    request: ParallelExtractRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    file_name, unique_block_ids, needs_analysis = svc.prepare_parallel_extraction(
        session, document_id, request.block_ids
    )
    run_id = start_document_extract_run(
        document_id,
        file_name,
        unique_block_ids,
        request.targets,
        needs_analysis,
        get_settings(),
        session=session,
        session_factory=SessionLocal,
        runner=task_runner(),
        analyze_structure_fn=analyze_document_structure,
        extract_parallel_fn=extract_parallel_for_blocks,
    )
    return {
        "status": "EXTRACTING",
        "document_id": document_id,
        "run_id": run_id,
        "block_count": len(unique_block_ids),
    }


@router.get("/{document_id}/architecture")
def get_architecture(document_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    architecture = latest_document_architecture(session, document_id)
    if not architecture:
        raise HTTPException(status_code=404, detail="No architecture analysis found")
    return {
        "id": architecture.id,
        "status": architecture.status,
        "plan": architecture.plan_json,
        "warnings": architecture.warnings_json,
        "trace": architecture.trace_json,
    }


@router.patch("/{document_id}/blocks/{block_id}/structure")
def adjust_structure(
    document_id: str,
    block_id: str,
    changes: Dict[str, Any],
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    return svc.adjust_block_structure(session, document_id, block_id, changes)


@router.get("/{document_id}/blocks/{block_id}")
def raw_block_detail(document_id: str, block_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    return svc.get_raw_block_detail(session, document_id, block_id)


__all__ = [
    "ALLOWED_BLOCK_TYPES",
    "FILE_TYPES",
    "CancelDocumentRequest",
    "ParallelExtractRequest",
    "document_summary",
    "hashlib",
    "ingest_uploaded_file",
    "router",
    "store_initial_blocks",
]
