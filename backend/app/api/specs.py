"""健康检查、文档目录/规范快照、Blob 读取与业务库只读抽样路由（`/api/*`）。"""

import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import ConfirmationRequest, ImportRequest, db_session, spec_summary
from app.delivery.business_db import BusinessDatabaseError, ReadOnlyBusinessDatabase
from app.infra.config import get_settings
from app.infra.models import SpecificationModel
from app.ingestion.catalog import parse_document_catalog
from app.ingestion.spec_parser import SpecParseError
from app.kernel.blob_store import LocalBlobStore

router = APIRouter(tags=["specs"])


def _store_catalog(session: Session, entries: List[Dict], source_name: str, digest: str) -> Dict:
    existing = (
        session.query(SpecificationModel)
        .filter(
            SpecificationModel.source_sha256 == digest,
            SpecificationModel.table_code == "DOCUMENT_CATALOG",
        )
        .one_or_none()
    )
    if existing:
        return spec_summary(existing)
    document_version = next((entry["spec"]["version"] for entry in entries if entry.get("spec")), "UNKNOWN")
    row = SpecificationModel(
        id=str(uuid.uuid4()),
        version=document_version,
        table_code="DOCUMENT_CATALOG",
        source_name=source_name,
        source_sha256=digest,
        content_json={
            "document_kind": "CATALOG",
            "version": document_version,
            "table_name": source_name,
            "tables": entries,
        },
    )
    session.add(row)
    session.commit()
    return spec_summary(row)


@router.get("/api/health")
def health() -> Dict:
    settings = get_settings()
    return {
        "status": "ok",
        "database": settings.database_url.split(":", 1)[0],
        "llm_provider": settings.llm_provider,
        "business_database_configured": bool(settings.business_database_url),
    }


@router.post("/api/specs/import-safe-13", status_code=201)
def import_safe_13(request: ImportRequest, session: Session = Depends(db_session)) -> Dict:
    settings = get_settings()
    try:
        entries = parse_document_catalog(settings.safe_13_spec_path)
    except SpecParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    digest = hashlib.sha256(Path(settings.safe_13_spec_path).read_bytes()).hexdigest()
    return _store_catalog(session, entries, Path(settings.safe_13_spec_path).name, digest)


@router.post("/api/documents", status_code=201)
async def upload_document(file: UploadFile = File(...), session: Session = Depends(db_session)) -> Dict:
    settings = get_settings()
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Only PDF documents are supported")
    content = await file.read()
    if len(content) > 30 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="PDF exceeds the 30 MB limit")
    digest = hashlib.sha256(content).hexdigest()
    target = Path(settings.blob_dir) / "uploads" / f"{digest}.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    try:
        entries = parse_document_catalog(str(target))
    except SpecParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _store_catalog(session, entries, file.filename, digest)


@router.get("/api/specs")
def list_specs(session: Session = Depends(db_session)) -> List[Dict]:
    rows = session.query(SpecificationModel).all()
    rows.sort(key=lambda row: row.created_at, reverse=True)
    return [spec_summary(row) for row in rows]


@router.get("/api/specs/{spec_id}")
def get_spec(spec_id: str, session: Session = Depends(db_session)) -> Dict:
    row = session.get(SpecificationModel, spec_id)
    if not row:
        raise HTTPException(status_code=404, detail="Specification not found")
    if row.content_json.get("document_kind") == "CATALOG":
        return {
            **spec_summary(row),
            "tables": [
                {key: value for key, value in table.items() if key != "spec"}
                for table in row.content_json["tables"]
            ],
            "fields": [],
        }
    if "tables" in row.content_json:
        tables = row.content_json["tables"]
        return {
            **spec_summary(row),
            "tables": [
                {"table_code": table["table_code"], "table_name": table["table_name"], "field_count": len(table["fields"])}
                for table in tables
            ],
            "fields": [
                {**field, "table_code": table["table_code"]}
                for table in tables
                for field in table["fields"]
            ],
        }
    return {**spec_summary(row), "fields": row.content_json["fields"], "terms_text": row.content_json["terms_text"]}


@router.post("/api/specs/{spec_id}/confirm", status_code=201)
def confirm_tables(spec_id: str, request: ConfirmationRequest, session: Session = Depends(db_session)) -> Dict:
    catalog = session.get(SpecificationModel, spec_id)
    if not catalog or catalog.content_json.get("document_kind") != "CATALOG":
        raise HTTPException(status_code=404, detail="Document catalog not found")
    requested = set(request.table_codes)
    entries = {entry["table_code"]: entry for entry in catalog.content_json["tables"]}
    if not requested or any(code not in entries or not entries[code]["eligible"] for code in requested):
        raise HTTPException(status_code=422, detail="Selection contains an unknown or ineligible table")
    selected = [entries[code]["spec"] for code in sorted(requested)]
    content = {
        "document_kind": "CONFIRMED_SNAPSHOT",
        "selection_confirmed": True,
        "version": catalog.version,
        "table_name": f"已确认 {len(selected)} 张表 · {request.approver.strip()}",
        "confirmation": {
            "approver": request.approver.strip(),
            "confirmed_at": datetime.utcnow().isoformat(),
            "table_codes": sorted(requested),
            "source_spec_id": spec_id,
        },
        "tables": selected,
    }
    row = SpecificationModel(
        id=str(uuid.uuid4()),
        version=catalog.version,
        table_code="CONFIRMED_SNAPSHOT",
        source_name=catalog.source_name,
        source_sha256=catalog.source_sha256,
        content_json=content,
    )
    session.add(row)
    session.commit()
    return spec_summary(row)


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
