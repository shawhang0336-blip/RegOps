import hashlib
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..asset_models import DocumentArchitectureModel, RawBlockModel, SourceDocumentModel
from ..code_service import auto_link_all_references, extract_code_sets_for_block, extract_parallel_for_blocks
from ..config import get_settings
from ..db import SessionLocal
from ..extractors import extract_document
from ..structure_service import (
    analyze_document_structure,
    inspect_document_overview,
    latest_document_architecture,
    reextract_document_blocks,
)

router = APIRouter(prefix="/documents", tags=["documents"])

FILE_TYPES = {"pdf": "pdf", "docx": "docx", "xlsx": "xlsx"}


class ParallelExtractRequest(BaseModel):
    block_ids: List[str]
    targets: Optional[List[str]] = None


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
        try:
            version, blocks = extract_document(source_path, document.file_type)
            if not blocks:
                raise ValueError("No raw blocks were discovered")
        except Exception as exc:
            document.status = "BLOCK_EXTRACTION_FAILED"
            session.commit()
            raise HTTPException(status_code=422, detail=f"Cannot extract document: {type(exc).__name__}: {exc}") from exc
        document.spec_version = version
        document.status = "UPLOADED"
        document.block_count = len(blocks)
        for block in blocks:
            session.add(RawBlockModel(id=str(uuid.uuid4()), document_id=document.id, **block))
        session.commit()
        _auto_extract_code_sets(document_id, get_settings())
        return document


def ingest_uploaded_file(filename: str, content: bytes) -> SourceDocumentModel:
    suffix = Path(filename).suffix.lower().lstrip(".")
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
    target.write_bytes(content)
    reimport_deleted = False
    with SessionLocal() as session:
        existing = session.query(SourceDocumentModel).filter(SourceDocumentModel.sha256 == digest).one_or_none()
        if existing:
            if existing.status != "DELETED":
                return existing
            reimport_deleted = True
            existing.status = "OVERVIEW_REQUIRED"
            existing.spec_version = "UNKNOWN"
            session.commit()
            document_id = existing.id
        else:
            document = SourceDocumentModel(
                id=str(uuid.uuid4()),
                file_name=Path(filename).name,
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
    inspect_document_overview(document_id, settings, force=reimport_deleted)
    with SessionLocal() as session:
        document = session.get(SourceDocumentModel, document_id)
        if document.status == "OVERVIEW_FAILED":
            return document
    if reimport_deleted:
        reextract_document_blocks(document_id, settings)
        with SessionLocal() as session:
            return session.get(SourceDocumentModel, document_id)
    return store_initial_blocks(document_id)


@router.post("", status_code=201)
async def upload_document(file: UploadFile = File(...)) -> Dict[str, Any]:
    content = await file.read()
    document = ingest_uploaded_file(file.filename, content)
    return document_summary(document)


@router.get("")
def list_documents() -> list:
    with SessionLocal() as session:
        rows = (
            session.query(SourceDocumentModel)
            .filter(SourceDocumentModel.status != "DELETED")
            .order_by(SourceDocumentModel.created_at.desc())
            .all()
        )
        return [document_summary(row) for row in rows]


@router.delete("/{document_id}")
def delete_document(document_id: str) -> Dict[str, Any]:
    with SessionLocal() as session:
        document = session.get(SourceDocumentModel, document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
        document.status = "DELETED"
        session.commit()
        return {"id": document.id, "status": document.status, "assets_preserved": True}


@router.get("/{document_id}")
def document_detail(document_id: str) -> Dict[str, Any]:
    with SessionLocal() as session:
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
                }
                for block in blocks
            ],
        }


@router.post("/{document_id}/prepare")
def prepare_document(document_id: str) -> Dict[str, Any]:
    inspect_document_overview(document_id, get_settings(), force=True)
    with SessionLocal() as session:
        document = session.get(SourceDocumentModel, document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
        if document.status == "OVERVIEW_FAILED":
            return document_summary(document)
    return document_summary(store_initial_blocks(document_id))


@router.post("/{document_id}/reextract")
def reextract_blocks(document_id: str) -> Dict[str, Any]:
    try:
        count = reextract_document_blocks(document_id, get_settings())
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"document_id": document_id, "block_count": count, "status": "UPLOADED"}


@router.post("/{document_id}/analyze-structure")
def analyze_structure(document_id: str) -> Dict[str, Any]:
    try:
        architecture_id = analyze_document_structure(document_id, get_settings())
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with SessionLocal() as session:
        architecture = session.get(DocumentArchitectureModel, architecture_id)
        return {
            "id": architecture.id,
            "status": architecture.status,
            "plan": architecture.plan_json,
            "warnings": architecture.warnings_json,
            "trace": architecture.trace_json,
        }


@router.post("/{document_id}/extract-parallel", status_code=202)
def extract_document_parallel(document_id: str, request: ParallelExtractRequest) -> Dict[str, Any]:
    if not request.block_ids:
        raise HTTPException(status_code=422, detail="Select at least one raw block")
    settings = get_settings()
    with SessionLocal() as session:
        document = session.get(SourceDocumentModel, document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
        needs_analysis = document.status == "UPLOADED"
        document.status = "EXTRACTING"
        session.commit()
    try:
        with SessionLocal() as session:
            if needs_analysis:
                analyze_document_structure(document_id, settings)
        result = extract_parallel_for_blocks(request.block_ids, settings, request.targets)
        final_status = "EXTRACTION_PARTIAL" if result.get("errors") else "EXTRACTED"
        if result.get("errors") and not result.get("table_ids") and not result.get("code_set_ids"):
            final_status = "EXTRACTION_FAILED"
    except Exception as exc:
        result = {"table_ids": [], "code_set_ids": [], "errors": [{"error": f"{type(exc).__name__}: {exc}"}]}
        final_status = "EXTRACTION_FAILED"
    with SessionLocal() as session:
        document = session.get(SourceDocumentModel, document_id)
        document.status = final_status
        session.add(
            DocumentArchitectureModel(
                id=str(uuid.uuid4()),
                document_id=document_id,
                status=final_status,
                plan_json={"stage": "EXTRACTION", "result": result},
                warnings_json=[item.get("error", "解析未完成") for item in result.get("errors", [])],
                trace_json={"table_count": len(result.get("table_ids", [])), "code_set_count": len(result.get("code_set_ids", []))},
            )
        )
        session.commit()
    if final_status == "EXTRACTION_FAILED":
        raise HTTPException(status_code=422, detail=result["errors"][0]["error"])
    return result


@router.get("/{document_id}/architecture")
def get_architecture(document_id: str) -> Dict[str, Any]:
    with SessionLocal() as session:
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
def adjust_structure(document_id: str, block_id: str, changes: Dict[str, Any]) -> Dict[str, Any]:
    with SessionLocal() as session:
        block = session.get(RawBlockModel, block_id)
        document = session.get(SourceDocumentModel, document_id)
        if not block or block.document_id != document_id or not document:
            raise HTTPException(status_code=404, detail="Raw block not found")
        for key in ["block_type", "column_mapping", "human_required"]:
            if key in changes:
                if key == "block_type":
                    setattr(block, key, changes[key])
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
def raw_block_detail(document_id: str, block_id: str) -> Dict[str, Any]:
    with SessionLocal() as session:
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
