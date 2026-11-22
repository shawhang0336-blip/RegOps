import hashlib
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import db_session
from app.catalog.code_sets.service import (
    auto_link_all_references,
    extract_code_sets_for_block,
)
from app.catalog.extraction import extract_parallel_for_blocks
from app.catalog.version_diff import diff_documents
from app.catalog.pipeline_run import (
    cancel_document_run,
    document_progress_view,
    latest_document_run,
    list_document_runs,
    start_document_extract_run,
    start_document_prepare_run,
)
from app.infra.db_tables import (
    DocumentArchitectureModel,
    RawBlockModel,
    SourceDocumentModel,
)
from app.infra.config import get_settings
from app.infra.db import SessionLocal
from app.infra.errors import WorkflowError
from app.infra.task_runner import task_runner
from app.ingestion.parsers.extractors import extract_document
from app.ingestion.import_progress import update as progress_update
from app.ingestion.parsers.quality import inspect_block
from app.ingestion.structure_service import (
    analyze_document_structure,
    inspect_document_overview,
    invoke_extract_document,
    latest_chunking_plan,
    latest_document_architecture,
    reextract_document_blocks,
)
from app.kernel.run import run_registry
from app.policy.regops_plan import ACTIVE_INGESTION_STATUSES, project_ingestion

router = APIRouter(prefix="/documents", tags=["documents"])

FILE_TYPES = {"pdf": "pdf", "docx": "docx", "xlsx": "xlsx"}
ALLOWED_BLOCK_TYPES = frozenset(
    {
        "TABLE_SPEC",
        "TABLE_METADATA",
        "TABLE_CANDIDATE",
        "CODE_SET",
        "CODE_SET_CANDIDATE",
        "DOCUMENT_TABLE",
        "DOCX_TABLE",
        "XLSX_SHEET",
        "PDF_SECTION",
        "NARRATIVE",
        "PROSE",
        "TEXT",
        "GLOSSARY",
        "HEADER",
        "IGNORE",
    }
)


class ParallelExtractRequest(BaseModel):
    block_ids: List[str]
    targets: Optional[List[str]] = None


class CancelDocumentRequest(BaseModel):
    reason: Optional[str] = None


def document_summary(row: SourceDocumentModel) -> Dict[str, Any]:
    return {
        "id": row.id,
        "file_name": row.file_name,
        "file_type": row.file_type,
        "sha256": row.sha256,
        "spec_version": row.spec_version,
        "status": row.status,
        "block_count": row.block_count,
        "created_at": row.created_at.isoformat(),
    }


def _auto_extract_code_sets(document_id: str, settings) -> int:
    """切块后自动把 CODE_SET 块的标准码表入库（纯程序，不做 AI 复核），避免码表块被漏提。"""
    with SessionLocal() as session:
        block_ids = [
            block_id
            for (block_id,) in session.query(RawBlockModel.id)
            .filter(
                RawBlockModel.document_id == document_id,
                RawBlockModel.block_type.in_(["CODE_SET", "CODE_SET_CANDIDATE"]),
            )
            .order_by(RawBlockModel.ordinal)
            .all()
        ]
    created = 0
    for block_id in block_ids:
        try:
            created += len(extract_code_sets_for_block(block_id, settings, auto_link=False, review_with_ai=False))
        except Exception:
            continue
    if block_ids:
        with SessionLocal() as session:
            auto_link_all_references(session)
            session.commit()
    return created


def store_initial_blocks(document_id: str) -> SourceDocumentModel:
    with SessionLocal() as session:
        document = session.get(SourceDocumentModel, document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
        existing_blocks = session.query(RawBlockModel).filter(RawBlockModel.document_id == document.id).count()
        if existing_blocks:
            _auto_extract_code_sets(document_id, get_settings())
            return document
        settings = get_settings()
        source_path = Path(settings.blob_dir) / "uploads" / f"{document.sha256}.{document.file_type}"
        chunking_plan = latest_chunking_plan(session, document.id)
        try:
            version, blocks = invoke_extract_document(extract_document, source_path, document.file_type, chunking_plan)
            if not blocks:
                raise ValueError("No raw blocks were discovered")
        except Exception as exc:
            document.status = "BLOCK_EXTRACTION_FAILED"
            session.commit()
            raise HTTPException(status_code=422, detail=f"Cannot extract document: {type(exc).__name__}: {exc}") from exc
        document.spec_version = version
        document.status = "UPLOADED"
        document.block_count = len(blocks)
        flagged = 0
        for block in blocks:
            issues = inspect_block(block)
            if issues:
                flagged += 1
                block["content_json"] = {
                    **(block.get("content_json") or {}),
                    "quality_issues": issues,
                }
            session.add(RawBlockModel(id=str(uuid.uuid4()), document_id=document.id, **block))
        if flagged:
            progress_update(
                document_id,
                log=f"切块自检：{flagged}/{len(blocks)} 个块存在疑似解析异常，已标记待核对",
            )
        session.commit()
        _auto_extract_code_sets(document_id, get_settings())
        return document


def _document_has_active_run(session: Session, document: SourceDocumentModel) -> bool:
    if document.status in {"PREPARING", "EXTRACTING"}:
        return True
    latest = latest_document_run(session, document.id)
    if not latest:
        return False
    if run_registry().is_active(latest.id):
        return True
    return project_ingestion(session, latest).status in ACTIVE_INGESTION_STATUSES


def _ingest_uploaded_file(
    filename: Optional[str],
    content: bytes,
    *,
    prepare: bool = True,
) -> Tuple[SourceDocumentModel, bool]:
    clean_name = (filename or "").strip()
    suffix = Path(clean_name).suffix.lower().lstrip(".")
    if suffix not in FILE_TYPES:
        raise HTTPException(status_code=422, detail="Only PDF, DOCX and XLSX are supported")
    if len(content) > 30 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File exceeds the 30 MB limit")
    if suffix == "pdf" and not content.startswith(b"%PDF"):
        raise HTTPException(status_code=422, detail="Invalid PDF signature")
    if suffix in {"docx", "xlsx"} and not content.startswith(b"PK"):
        raise HTTPException(status_code=422, detail="Invalid Office file signature")
    digest = hashlib.sha256(content).hexdigest()
    settings = get_settings()
    target = Path(settings.blob_dir) / "uploads" / f"{digest}.{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_bytes(content)
        os.replace(tmp_path, target)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    reimport_deleted = False
    with SessionLocal() as session:
        existing = session.query(SourceDocumentModel).filter(SourceDocumentModel.sha256 == digest).one_or_none()
        if existing:
            if existing.status != "DELETED":
                return existing, False
            reimport_deleted = True
            existing.status = "OVERVIEW_REQUIRED"
            existing.spec_version = "UNKNOWN"
            session.commit()
            document_id = existing.id
        else:
            document = SourceDocumentModel(
                id=str(uuid.uuid4()),
                file_name=Path(clean_name).name,
                file_type=suffix,
                sha256=digest,
                blob_uri=f"blob://uploads/{digest}.{suffix}",
                spec_version="UNKNOWN",
                status="OVERVIEW_REQUIRED",
                block_count=0,
            )
            session.add(document)
            session.commit()
            document_id = document.id
    if not prepare:
        with SessionLocal() as session:
            return session.get(SourceDocumentModel, document_id), reimport_deleted
    inspect_document_overview(document_id, settings, force=reimport_deleted)
    with SessionLocal() as session:
        document = session.get(SourceDocumentModel, document_id)
        if document.status == "OVERVIEW_FAILED":
            return document, reimport_deleted
    if reimport_deleted:
        reextract_document_blocks(document_id, settings)
        with SessionLocal() as session:
            return session.get(SourceDocumentModel, document_id), reimport_deleted
    return store_initial_blocks(document_id), reimport_deleted


def ingest_uploaded_file(filename: str, content: bytes, *, prepare: bool = True) -> SourceDocumentModel:
    document, _ = _ingest_uploaded_file(filename, content, prepare=prepare)
    return document


@router.post("", status_code=201)
async def upload_document(file: UploadFile = File(...)) -> Dict[str, Any]:
    """保存文件后立即返回，结构检查与切块交由统一的 Step 1 Ingestion Run（kernel.run）驱动。"""
    content = await file.read()
    document, reimport_deleted = _ingest_uploaded_file(file.filename, content, prepare=False)
    already_ready = (
        not reimport_deleted
        and document.status in {"UPLOADED", "EXTRACTED", "EXTRACTION_PARTIAL", "STRUCTURE_REVIEWED"}
        and bool(document.block_count)
    )
    if already_ready:
        return {**document_summary(document), "processing": False}
    settings = get_settings()
    run_id = start_document_prepare_run(
        document.id,
        document.file_name,
        settings,
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
    rows = (
        session.query(SourceDocumentModel)
        .filter(SourceDocumentModel.status != "DELETED")
        .order_by(SourceDocumentModel.created_at.desc())
        .all()
    )
    return [document_summary(row) for row in rows]


@router.delete("/{document_id}")
def delete_document(document_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    document = session.get(SourceDocumentModel, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    if _document_has_active_run(session, document):
        raise HTTPException(status_code=409, detail="文档正在解析或切块中，请先取消当前任务后再删除")
    document.status = "DELETED"
    session.commit()
    return {"id": document.id, "status": document.status, "assets_preserved": True}


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
    row = session.get(SourceDocumentModel, document_id)
    if not row:
        raise HTTPException(status_code=404, detail="Document not found")
    blocks = (
        session.query(RawBlockModel)
        .filter(RawBlockModel.document_id == document_id)
        .order_by(RawBlockModel.ordinal)
        .all()
    )
    return {
        **document_summary(row),
        "blocks": [
            {
                "id": block.id,
                "ordinal": block.ordinal,
                "block_type": block.block_type,
                "candidate_role": block.content_json.get(
                    "candidate_role",
                    "CODE_SET" if block.block_type in {"CODE_SET", "CODE_SET_CANDIDATE"} else "TABLE_METADATA",
                ),
                "table_name": block.content_json.get("table_name", ""),
                "source_locator": block.source_locator,
                "raw_hash": block.raw_hash,
                "eligible": block.content_json.get("eligible", True),
                "reason": block.content_json.get("reason", ""),
                "field_count": block.content_json.get("field_count"),
                "inline_code_count": len(block.content_json.get("inline_code_sets", [])),
                "superseded": block.content_json.get("superseded", False),
                "column_mapping": block.content_json.get("column_mapping", {}),
                "human_required": block.content_json.get("human_required", False),
                "structure_warnings": block.content_json.get("structure_warnings", block.content_json.get("parse_warnings", [])),
                "quality_issues": block.content_json.get("quality_issues", []),
            }
            for block in blocks
        ],
    }


@router.get("/{document_id}/progress")
def document_progress(document_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    """前端进度视图：优先实时内存态，重启后从不可变 EventLog 折叠态无损还原。"""
    try:
        return document_progress_view(session, document_id)
    except WorkflowError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{document_id}/runs")
def document_runs(document_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    """查看指定文档历次 Step 1 Ingestion Run 审计记录（含 BudgetGuard 成本与哈希链校验）。"""
    document = session.get(SourceDocumentModel, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    items = list_document_runs(session, document_id)
    return {"document_id": document_id, "count": len(items), "runs": items}


@router.post("/{document_id}/cancel")
def cancel_document(
    document_id: str,
    request: Optional[CancelDocumentRequest] = None,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    """协作式中止正在运行的 Step 1 文档解析任务（经由 kernel.run 取消令牌）。"""
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
    file_name = document.file_name
    settings = get_settings()
    run_id = start_document_prepare_run(
        document_id,
        file_name,
        settings,
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
    unique_block_ids = list(dict.fromkeys(bid for bid in request.block_ids if bid))
    if not unique_block_ids:
        raise HTTPException(status_code=422, detail="Select at least one raw block")
    document = session.get(SourceDocumentModel, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    if document.status == "DELETED":
        raise HTTPException(status_code=409, detail="已删除文档不能发起解析")
    if _document_has_active_run(session, document):
        raise HTTPException(status_code=409, detail="当前文档已有正在运行的解析或切块任务")
    blocks = (
        session.query(RawBlockModel)
        .filter(RawBlockModel.document_id == document_id, RawBlockModel.id.in_(unique_block_ids))
        .all()
    )
    if len(blocks) != len(unique_block_ids):
        raise HTTPException(status_code=422, detail="所选内容块不存在或不属于当前文档")
    if any((block.content_json or {}).get("superseded") for block in blocks):
        raise HTTPException(status_code=409, detail="Superseded raw blocks cannot be extracted")
    needs_analysis = document.status == "UPLOADED"
    file_name = document.file_name
    document.status = "EXTRACTING"
    session.commit()
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
    block = session.get(RawBlockModel, block_id)
    document = session.get(SourceDocumentModel, document_id)
    if not block or block.document_id != document_id or not document:
        raise HTTPException(status_code=404, detail="Raw block not found")
    if "block_type" in changes:
        new_type = str(changes["block_type"] or "").strip()
        if new_type not in ALLOWED_BLOCK_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"不支持的 block_type: {new_type}",
            )
    for key in ["block_type", "column_mapping", "human_required"]:
        if key in changes:
            if key == "block_type":
                setattr(block, key, str(changes[key]).strip())
            else:
                block.content_json = {**block.content_json, key: changes[key]}
    document.status = "STRUCTURE_REVIEWED"
    architecture = DocumentArchitectureModel(
        id=str(uuid.uuid4()),
        document_id=document.id,
        status="HUMAN_ADJUSTED",
        plan_json={"block_id": block_id, "changes": changes},
        warnings_json=[] if changes.get("human_required") is False else block.content_json.get("structure_warnings", []),
        trace_json={"editor": changes.get("editor", "HUMAN")},
    )
    session.add(architecture)
    session.commit()
    return {"id": block.id, "block_type": block.block_type, "content_json": block.content_json}


@router.get("/{document_id}/blocks/{block_id}")
def raw_block_detail(document_id: str, block_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    block = session.get(RawBlockModel, block_id)
    if not block or block.document_id != document_id:
        raise HTTPException(status_code=404, detail="Raw block not found")
    return {
        "id": block.id,
        "block_type": block.block_type,
        "raw_content": block.raw_content,
        "source_locator": block.source_locator,
        "content_json": block.content_json,
        "raw_hash": block.raw_hash,
    }
