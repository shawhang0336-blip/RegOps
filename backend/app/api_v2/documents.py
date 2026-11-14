import hashlib
import uuid
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..asset_models import DocumentArchitectureModel, RawBlockModel, SourceDocumentModel
from ..config import get_settings
from ..db import SessionLocal
from ..extractors import extract_document
from ..structure_service import analyze_document_structure, reextract_document_blocks

router = APIRouter(prefix="/documents", tags=["documents"])

FILE_TYPES = {"pdf": "pdf", "docx": "docx", "xlsx": "xlsx"}


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


@router.post("", status_code=201)
async def upload_document(file: UploadFile = File(...)) -> Dict[str, Any]:
    suffix = Path(file.filename).suffix.lower().lstrip(".")
    if suffix not in FILE_TYPES:
        raise HTTPException(status_code=422, detail="Only PDF, DOCX and XLSX are supported")
    content = await file.read()
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
    try:
        version, blocks = extract_document(target, suffix)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Cannot extract document: {type(exc).__name__}: {exc}") from exc
    if not blocks:
        raise HTTPException(status_code=422, detail="No raw blocks were discovered")
    with SessionLocal() as session:
        existing = session.query(SourceDocumentModel).filter(SourceDocumentModel.sha256 == digest).one_or_none()
        if existing:
            return document_summary(existing)
        document = SourceDocumentModel(
            id=str(uuid.uuid4()),
            file_name=Path(file.filename).name,
            file_type=suffix,
            sha256=digest,
            blob_uri=f"blob://uploads/{digest}.{suffix}",
            spec_version=version,
            status="UPLOADED",
            block_count=len(blocks),
        )
        session.add(document)
        session.flush()
        for block in blocks:
            session.add(RawBlockModel(id=str(uuid.uuid4()), document_id=document.id, **block))
        session.commit()
        return document_summary(document)


@router.get("")
def list_documents() -> list:
    with SessionLocal() as session:
        rows = session.query(SourceDocumentModel).order_by(SourceDocumentModel.created_at.desc()).all()
        return [document_summary(row) for row in rows]


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
                    "source_locator": block.source_locator,
                    "raw_hash": block.raw_hash,
                    "eligible": block.content_json.get("eligible", True),
                    "reason": block.content_json.get("reason", ""),
                    "field_count": block.content_json.get("field_count"),
                    "superseded": block.content_json.get("superseded", False),
                    "column_mapping": block.content_json.get("column_mapping", {}),
                    "human_required": block.content_json.get("human_required", False),
                    "structure_warnings": block.content_json.get("structure_warnings", []),
                }
                for block in blocks
            ],
        }


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


@router.get("/{document_id}/architecture")
def get_architecture(document_id: str) -> Dict[str, Any]:
    with SessionLocal() as session:
        architecture = (
            session.query(DocumentArchitectureModel)
            .filter(DocumentArchitectureModel.document_id == document_id)
            .order_by(DocumentArchitectureModel.created_at.desc())
            .first()
        )
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
            warnings_json=block.content_json.get("structure_warnings", []),
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
