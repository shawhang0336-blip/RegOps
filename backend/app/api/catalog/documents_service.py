"""文档目录与原始块应用服务（供 catalog/documents.py 与 catalog/code_sets_service.py 复用）。"""

import hashlib
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog.code_sets.service import (
    auto_link_all_references,
    extract_code_sets_for_block,
)
from app.catalog.extraction import extract_rule_evidences_from_block_snapshot, persist_rule_evidences
from app.catalog.pipeline_run import latest_document_run
from app.catalog.tables.service import snapshot_raw_block
from app.infra.config import get_settings
from app.infra.db import SessionLocal
from app.infra.db_tables import (
    DocumentArchitectureModel,
    RawBlockModel,
    SourceDocumentModel,
)
from app.ingestion.import_progress import update as progress_update
from app.ingestion.parsers.extractors import (
    convert_legacy_office_bytes,
    extract_document,
    infer_document_role,
)
from app.ingestion.parsers.quality import inspect_block
from app.ingestion.structure_service import (
    inspect_document_overview,
    invoke_extract_document,
    latest_chunking_plan,
    reextract_document_blocks,
)
from app.kernel.run import run_registry
from app.policy.regops_plan import ACTIVE_INGESTION_STATUSES, project_ingestion

FILE_TYPES = {"pdf": "pdf", "docx": "docx", "xlsx": "xlsx"}
_UPLOAD_LOCKS_GUARD = threading.Lock()
_UPLOAD_LOCKS: Dict[Tuple[str, str], threading.Lock] = {}


def _upload_mutex(package_id: Optional[str], digest: str) -> threading.Lock:
    key = (package_id or "__standalone__", digest)
    with _UPLOAD_LOCKS_GUARD:
        lock = _UPLOAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _UPLOAD_LOCKS[key] = lock
        return lock
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
        "RULE_EVIDENCE",
        "REVISION_DIFF",
        "CATALOG_INDEX",
        "MATRIX_REPORT",
        "NARRATIVE",
        "PROSE",
        "TEXT",
        "GLOSSARY",
        "HEADER",
        "IGNORE",
    }
)


def document_summary(row: SourceDocumentModel) -> Dict[str, Any]:
    return {
        "id": row.id,
        "package_id": row.package_id,
        "doc_role": getattr(row, "doc_role", "AUTO") or "AUTO",
        "file_name": row.file_name,
        "file_type": row.file_type,
        "sha256": row.sha256,
        "spec_version": row.spec_version,
        "status": row.status,
        "block_count": row.block_count,
        "created_at": row.created_at.isoformat(),
    }


def auto_extract_code_sets(
    document_id: str,
    settings: Any,
    *,
    session_factory: Optional[Callable[[], Any]] = None,
) -> int:
    """切块后自动把 CODE_SET 块的标准码表入库（纯程序，不做 AI 复核），避免码表块被漏提。"""
    sf = session_factory or SessionLocal
    with sf() as session:
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
        with sf() as session:
            auto_link_all_references(session)
            session.commit()
    return created


def auto_extract_rule_evidences(
    document_id: str,
    *,
    session_factory: Optional[Callable[[], Any]] = None,
) -> int:
    """切块后确定性抽取 RULE_EVIDENCE 块与 Excel 检核规则清单入库（0 LLM 调用）。"""
    sf = session_factory or SessionLocal
    created = 0
    with sf() as session:
        doc = session.get(SourceDocumentModel, document_id)
        pkg_id = doc.package_id if doc is not None else None
        blocks = (
            session.query(RawBlockModel)
            .filter(RawBlockModel.document_id == document_id)
            .order_by(RawBlockModel.ordinal)
            .all()
        )
        for blk in blocks:
            cj = blk.content_json or {}
            if cj.get("superseded"):
                continue
            if not (blk.block_type == "RULE_EVIDENCE" or cj.get("candidate_role") == "RULE_EVIDENCE" or cj.get("rule_rows")):
                continue
            items = extract_rule_evidences_from_block_snapshot(snapshot_raw_block(blk))
            created += len(persist_rule_evidences(session, blk, items, package_id=pkg_id))
        if created:
            session.commit()
    return created


def store_initial_blocks(
    document_id: str,
    *,
    session_factory: Optional[Callable[[], Any]] = None,
    get_settings_fn: Optional[Callable[[], Any]] = None,
    extract_document_fn: Optional[Callable[..., Any]] = None,
) -> SourceDocumentModel:
    sf = session_factory or SessionLocal
    gs = get_settings_fn or get_settings
    ef = extract_document_fn or extract_document
    with sf() as session:
        document = session.get(SourceDocumentModel, document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
        existing_blocks = session.query(RawBlockModel).filter(RawBlockModel.document_id == document.id).count()
        if existing_blocks:
            auto_extract_code_sets(document_id, gs(), session_factory=sf)
            auto_extract_rule_evidences(document_id, session_factory=sf)
            return document
        settings = gs()
        source_path = Path(settings.blob_dir) / "uploads" / f"{document.sha256}.{document.file_type}"
        chunking_plan = latest_chunking_plan(session, document.id)
        try:
            version, blocks = invoke_extract_document(ef, source_path, document.file_type, chunking_plan)
            if not blocks:
                raise ValueError("No raw blocks were discovered")
        except Exception as exc:
            document.status = "BLOCK_EXTRACTION_FAILED"
            session.commit()
            raise HTTPException(status_code=422, detail=f"Cannot extract document: {type(exc).__name__}: {exc}") from exc
        document.spec_version = version
        document.status = "UPLOADED"
        document.block_count = len(blocks)
        if getattr(document, "doc_role", "AUTO") in {"", "AUTO", None}:
            document.doc_role = infer_document_role(document.file_name, blocks)
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
        auto_extract_code_sets(document_id, gs(), session_factory=sf)
        auto_extract_rule_evidences(document_id, session_factory=sf)
        return document


def document_has_active_run(session: Session, document: SourceDocumentModel) -> bool:
    if document.status in {"PREPARING", "EXTRACTING"}:
        return True
    latest = latest_document_run(session, document.id)
    if not latest:
        return False
    if run_registry().is_active(latest.id):
        return True
    return project_ingestion(session, latest).status in ACTIVE_INGESTION_STATUSES


def ingest_uploaded_file_with_flag(
    filename: Optional[str],
    content: bytes,
    *,
    prepare: bool = True,
    package_id: Optional[str] = None,
    doc_role: str = "AUTO",
    session_factory: Optional[Callable[[], Any]] = None,
    get_settings_fn: Optional[Callable[[], Any]] = None,
    extract_document_fn: Optional[Callable[..., Any]] = None,
) -> Tuple[SourceDocumentModel, bool]:
    sf = session_factory or SessionLocal
    gs = get_settings_fn or get_settings
    ef = extract_document_fn or extract_document

    clean_name = (filename or "").strip()
    if not clean_name:
        raise HTTPException(status_code=422, detail="File name is required")
    raw_suffix = Path(clean_name).suffix.lower().lstrip(".")
    if raw_suffix in {"doc", "xls"}:
        try:
            clean_name, content = convert_legacy_office_bytes(clean_name, content)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    settings = gs()
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
    clean_role = (doc_role or "AUTO").strip().upper() or "AUTO"
    pkg_filter = (
        (SourceDocumentModel.package_id == package_id)
        if package_id is not None
        else SourceDocumentModel.package_id.is_(None)
    )
    with _upload_mutex(package_id, digest), sf() as session:
        existing = (
            session.query(SourceDocumentModel)
            .filter(pkg_filter, SourceDocumentModel.sha256 == digest)
            .order_by(SourceDocumentModel.created_at, SourceDocumentModel.id)
            .first()
        )
        if existing:
            if existing.status != "DELETED":
                return existing, False
            reimport_deleted = True
            existing.status = "OVERVIEW_REQUIRED"
            existing.spec_version = "UNKNOWN"
            if clean_role != "AUTO":
                existing.doc_role = clean_role
            session.commit()
            document_id = existing.id
        else:
            document = SourceDocumentModel(
                id=str(uuid.uuid4()),
                package_id=package_id,
                doc_role=clean_role,
                file_name=Path(clean_name).name,
                file_type=suffix,
                sha256=digest,
                blob_uri=f"blob://uploads/{digest}.{suffix}",
                spec_version="UNKNOWN",
                status="OVERVIEW_REQUIRED",
                block_count=0,
            )
            session.add(document)
            try:
                session.commit()
                document_id = document.id
            except IntegrityError:
                session.rollback()
                existing = (
                    session.query(SourceDocumentModel)
                    .filter(pkg_filter, SourceDocumentModel.sha256 == digest)
                    .order_by(SourceDocumentModel.created_at, SourceDocumentModel.id)
                    .first()
                )
                if existing is None:
                    raise
                if existing.status != "DELETED":
                    return existing, False
                reimport_deleted = True
                existing.status = "OVERVIEW_REQUIRED"
                existing.spec_version = "UNKNOWN"
                session.commit()
                document_id = existing.id

    if not prepare:
        with sf() as session:
            return session.get(SourceDocumentModel, document_id), reimport_deleted

    inspect_document_overview(document_id, settings, force=reimport_deleted)
    with sf() as session:
        document = session.get(SourceDocumentModel, document_id)
        if document.status == "OVERVIEW_FAILED":
            return document, reimport_deleted
    if reimport_deleted:
        reextract_document_blocks(document_id, settings)
        with sf() as session:
            return session.get(SourceDocumentModel, document_id), reimport_deleted
    return (
        store_initial_blocks(
            document_id,
            session_factory=sf,
            get_settings_fn=gs,
            extract_document_fn=ef,
        ),
        reimport_deleted,
    )


def ingest_uploaded_file(
    filename: str,
    content: bytes,
    *,
    prepare: bool = True,
    package_id: Optional[str] = None,
    doc_role: str = "AUTO",
    session_factory: Optional[Callable[[], Any]] = None,
    get_settings_fn: Optional[Callable[[], Any]] = None,
    extract_document_fn: Optional[Callable[..., Any]] = None,
) -> SourceDocumentModel:
    document, _ = ingest_uploaded_file_with_flag(
        filename,
        content,
        prepare=prepare,
        package_id=package_id,
        doc_role=doc_role,
        session_factory=session_factory,
        get_settings_fn=get_settings_fn,
        extract_document_fn=extract_document_fn,
    )
    return document


def list_active_documents(session: Session) -> List[Dict[str, Any]]:
    rows = (
        session.query(SourceDocumentModel)
        .filter(SourceDocumentModel.status != "DELETED")
        .order_by(SourceDocumentModel.created_at.desc())
        .all()
    )
    return [document_summary(row) for row in rows]


def soft_delete_document(session: Session, document_id: str) -> Dict[str, Any]:
    document = session.get(SourceDocumentModel, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    if document_has_active_run(session, document):
        raise HTTPException(status_code=409, detail="文档正在解析或切块中，请先取消当前任务后再删除")
    if document.status == "DELETED":
        return {"id": document.id, "status": document.status, "assets_preserved": True}
    updated = (
        session.query(SourceDocumentModel)
        .filter(
            SourceDocumentModel.id == document_id,
            SourceDocumentModel.status.notin_(["PREPARING", "EXTRACTING"]),
        )
        .update({SourceDocumentModel.status: "DELETED"}, synchronize_session="fetch")
    )
    if updated != 1:
        session.rollback()
        latest = session.get(SourceDocumentModel, document_id)
        if latest and latest.status == "DELETED":
            return {"id": latest.id, "status": latest.status, "assets_preserved": True}
        raise HTTPException(status_code=409, detail="文档正在解析或切块中，请先取消当前任务后再删除")
    session.commit()
    return {"id": document_id, "status": "DELETED", "assets_preserved": True}


def get_document_detail(session: Session, document_id: str) -> Dict[str, Any]:
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


def prepare_parallel_extraction(
    session: Session,
    document_id: str,
    block_ids: List[str],
) -> Tuple[str, List[str], bool]:
    unique_block_ids = list(dict.fromkeys(bid for bid in block_ids if bid))
    if not unique_block_ids:
        raise HTTPException(status_code=422, detail="Select at least one raw block")
    document = session.get(SourceDocumentModel, document_id)
    if not document:
        raise HTTPException(status_code=404, detail="Document not found")
    if document.status == "DELETED":
        raise HTTPException(status_code=409, detail="已删除文档不能发起解析")
    if document_has_active_run(session, document):
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
    claimed = (
        session.query(SourceDocumentModel)
        .filter(
            SourceDocumentModel.id == document_id,
            SourceDocumentModel.status.notin_(["DELETED", "PREPARING", "EXTRACTING"]),
        )
        .update({SourceDocumentModel.status: "EXTRACTING"}, synchronize_session="fetch")
    )
    if claimed != 1:
        session.rollback()
        raise HTTPException(status_code=409, detail="当前文档已有正在运行的解析或切块任务")
    session.commit()
    return file_name, unique_block_ids, needs_analysis


def adjust_block_structure(
    session: Session,
    document_id: str,
    block_id: str,
    changes: Dict[str, Any],
) -> Dict[str, Any]:
    block = session.get(RawBlockModel, block_id)
    document = session.get(SourceDocumentModel, document_id)
    if not block or block.document_id != document_id or not document:
        raise HTTPException(status_code=404, detail="Raw block not found")
    if "block_type" in changes:
        new_type = str(changes["block_type"] or "").strip()
        if new_type not in ALLOWED_BLOCK_TYPES:
            raise HTTPException(status_code=422, detail=f"不支持的 block_type: {new_type}")
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


def get_raw_block_detail(session: Session, document_id: str, block_id: str) -> Dict[str, Any]:
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
