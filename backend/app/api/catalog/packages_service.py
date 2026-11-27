"""多文件监管规范包（SpecPackage）应用编排服务（供 `api/catalog/packages.py` 调用）。"""

import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.catalog.documents_service import document_summary, ingest_uploaded_file_with_flag
from app.catalog.code_sets.linking import package_code_set_ids
from app.catalog.extraction import extract_parallel_for_blocks, reconcile_package_assets
from app.catalog.tables.readiness import latest_table_rows
from app.catalog.version_diff import diff_packages
from app.infra.config import get_settings
from app.infra.db import SessionLocal
from app.infra.db_tables import (
    RawBlockModel,
    SourceDocumentModel,
    SpecPackageModel,
    TableRuleEvidenceModel,
)
from app.ingestion.parsers.extractors import unpack_zip_archive
from app.ingestion.structure_service import analyze_document_structure

ALLOWED_DOC_ROLES = frozenset(
    {"AUTO", "MAIN_SPEC", "SCHEMA_APPENDIX", "CODE_SET_APPENDIX", "RULE_SUPPLEMENT", "REVISION_NOTE"}
)
ALLOWED_REGULATORS = frozenset({"SAFE", "YBT", "EAST", "PBOC", "1104", "CUSTOM"})


def package_summary(session: Session, pkg: SpecPackageModel) -> Dict[str, Any]:
    docs = (
        session.query(SourceDocumentModel)
        .filter(SourceDocumentModel.package_id == pkg.id, SourceDocumentModel.status != "DELETED")
        .order_by(SourceDocumentModel.created_at.asc())
        .all()
    )
    tables = latest_table_rows(session, package_id=pkg.id)
    cs_ids = package_code_set_ids(session, pkg.id)
    rule_count = (
        session.query(TableRuleEvidenceModel)
        .filter(TableRuleEvidenceModel.package_id == pkg.id)
        .count()
    )
    doc_ids = [d.id for d in docs]
    matrix_count = (
        session.query(RawBlockModel)
        .filter(RawBlockModel.document_id.in_(doc_ids), RawBlockModel.block_type == "MATRIX_REPORT")
        .count()
        if doc_ids
        else 0
    )
    return {
        "id": pkg.id,
        "name": pkg.name,
        "regulator": pkg.regulator,
        "spec_version": pkg.spec_version,
        "parent_package_id": pkg.parent_package_id,
        "status": pkg.status,
        "description": pkg.description,
        "document_count": len(docs),
        "table_count": len(tables),
        "code_set_count": len(cs_ids),
        "rule_evidence_count": rule_count,
        "matrix_report_count": matrix_count,
        "created_at": pkg.created_at.isoformat(),
        "updated_at": pkg.updated_at.isoformat(),
    }


def create_package(
    session: Session,
    *,
    name: str,
    regulator: str = "SAFE",
    spec_version: str = "UNKNOWN",
    parent_package_id: Optional[str] = None,
    description: str = "",
) -> Dict[str, Any]:
    clean_name = (name or "").strip()
    if not clean_name:
        raise HTTPException(status_code=422, detail="规范包名称不能为空")
    reg = (regulator or "SAFE").strip().upper()
    if reg not in ALLOWED_REGULATORS:
        reg = "CUSTOM"
    if parent_package_id and not session.get(SpecPackageModel, parent_package_id):
        raise HTTPException(status_code=404, detail="Parent package not found")
    pkg = SpecPackageModel(
        id=str(uuid.uuid4()),
        name=clean_name[:255],
        regulator=reg,
        spec_version=(spec_version or "UNKNOWN").strip()[:64] or "UNKNOWN",
        parent_package_id=parent_package_id,
        status="ACTIVE",
        description=(description or "").strip(),
    )
    session.add(pkg)
    session.commit()
    return package_summary(session, pkg)


def list_packages(session: Session) -> List[Dict[str, Any]]:
    rows = (
        session.query(SpecPackageModel)
        .filter(SpecPackageModel.status != "DELETED")
        .order_by(SpecPackageModel.created_at.desc())
        .all()
    )
    return [package_summary(session, row) for row in rows]


def get_package_detail(session: Session, package_id: str) -> Dict[str, Any]:
    pkg = session.get(SpecPackageModel, package_id)
    if not pkg or pkg.status == "DELETED":
        raise HTTPException(status_code=404, detail="Package not found")
    docs = (
        session.query(SourceDocumentModel)
        .filter(SourceDocumentModel.package_id == package_id, SourceDocumentModel.status != "DELETED")
        .order_by(SourceDocumentModel.created_at.asc())
        .all()
    )
    return {
        **package_summary(session, pkg),
        "documents": [document_summary(doc) for doc in docs],
    }


def delete_package(session: Session, package_id: str) -> Dict[str, Any]:
    pkg = session.get(SpecPackageModel, package_id)
    if not pkg:
        raise HTTPException(status_code=404, detail="Package not found")
    pkg.status = "DELETED"
    session.commit()
    return {"id": package_id, "status": "DELETED"}


def upload_files_to_package(
    session: Session,
    package_id: str,
    files: List[Tuple[str, bytes]],
    *,
    doc_role: str = "AUTO",
    prepare: bool = True,
    session_factory: Optional[Callable[[], Any]] = None,
    get_settings_fn: Optional[Callable[[], Any]] = None,
    extract_document_fn: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    pkg = session.get(SpecPackageModel, package_id)
    if not pkg or pkg.status == "DELETED":
        raise HTTPException(status_code=404, detail="Package not found")
    if not files:
        raise HTTPException(status_code=422, detail="At least one file is required")

    expanded: List[Tuple[str, bytes]] = []
    for fname, data in files:
        ext = Path(fname or "").suffix.lower().lstrip(".")
        if ext == "zip":
            try:
                inner = unpack_zip_archive(data)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            expanded.extend((Path(inner_name).name, inner_bytes) for inner_name, inner_bytes in inner)
        else:
            expanded.append((fname, data))

    ingested: List[Dict[str, Any]] = []
    for fname, data in expanded:
        doc, _ = ingest_uploaded_file_with_flag(
            fname,
            data,
            prepare=prepare,
            package_id=package_id,
            doc_role=doc_role,
            session_factory=session_factory or SessionLocal,
            get_settings_fn=get_settings_fn or get_settings,
            extract_document_fn=extract_document_fn,
        )
        ingested.append(document_summary(doc))

    session.expire_all()
    pkg = session.get(SpecPackageModel, package_id)
    return {"package": package_summary(session, pkg), "uploaded": ingested}


def update_file_role(
    session: Session,
    package_id: str,
    document_id: str,
    doc_role: str,
) -> Dict[str, Any]:
    role = (doc_role or "").strip().upper()
    if role not in ALLOWED_DOC_ROLES:
        raise HTTPException(status_code=422, detail=f"不支持的文档角色：{doc_role}")
    doc = session.get(SourceDocumentModel, document_id)
    if not doc or doc.package_id != package_id or doc.status == "DELETED":
        raise HTTPException(status_code=404, detail="Document not found in package")
    doc.doc_role = role
    session.commit()
    return document_summary(doc)


def extract_and_reconcile_package(
    session: Session,
    package_id: str,
    *,
    targets: Optional[List[str]] = None,
    document_ids: Optional[List[str]] = None,
    reconcile: bool = True,
    settings: Any = None,
) -> Dict[str, Any]:
    pkg = session.get(SpecPackageModel, package_id)
    if not pkg or pkg.status == "DELETED":
        raise HTTPException(status_code=404, detail="Package not found")
    active_settings = settings or get_settings()

    doc_query = session.query(SourceDocumentModel).filter(
        SourceDocumentModel.package_id == package_id,
        SourceDocumentModel.status != "DELETED",
    )
    if document_ids:
        doc_query = doc_query.filter(SourceDocumentModel.id.in_(document_ids))
    docs = doc_query.order_by(SourceDocumentModel.created_at.asc()).all()
    if not docs:
        raise HTTPException(status_code=422, detail="规范包内尚无可提取的有效文档")

    for doc in docs:
        if doc.status == "UPLOADED":
            try:
                analyze_document_structure(doc.id, active_settings)
            except Exception:
                pass
    session.expire_all()

    active_doc_ids = [d.id for d in docs]
    blocks = (
        session.query(RawBlockModel)
        .filter(RawBlockModel.document_id.in_(active_doc_ids))
        .order_by(RawBlockModel.document_id, RawBlockModel.ordinal)
        .all()
    )
    extract_block_ids = [
        b.id for b in blocks if not (b.content_json or {}).get("superseded") and b.block_type != "IGNORE"
    ]
    if not extract_block_ids:
        raise HTTPException(status_code=422, detail="规范包内未发现可提取的内容块")

    result = extract_parallel_for_blocks(
        extract_block_ids,
        active_settings,
        targets or ["metadata", "code_sets", "rule_evidence"],
    )
    session.expire_all()
    reconcile_summary = reconcile_package_assets(session, package_id) if reconcile else None
    session.commit()

    pkg = session.get(SpecPackageModel, package_id)
    return {
        "package": package_summary(session, pkg),
        "extract": result,
        "reconcile": reconcile_summary,
    }


def list_package_rule_evidences(
    session: Session,
    package_id: str,
    *,
    table_code: Optional[str] = None,
) -> List[Dict[str, Any]]:
    pkg = session.get(SpecPackageModel, package_id)
    if not pkg or pkg.status == "DELETED":
        raise HTTPException(status_code=404, detail="Package not found")
    query = session.query(TableRuleEvidenceModel).filter(TableRuleEvidenceModel.package_id == package_id)
    if table_code:
        query = query.filter(TableRuleEvidenceModel.table_code == table_code.strip().upper())
    rows = query.order_by(TableRuleEvidenceModel.table_code.asc(), TableRuleEvidenceModel.rule_code.asc()).all()
    return [
        {
            "id": r.id,
            "package_id": r.package_id,
            "document_id": r.document_id,
            "block_id": r.block_id,
            "table_code": r.table_code,
            "table_name": r.table_name,
            "rule_code": r.rule_code,
            "rule_name": r.rule_name,
            "severity": r.severity,
            "scope": r.scope,
            "target_fields": list(r.target_fields_json or []),
            "rule_text": r.rule_text,
            "reference_sql": r.reference_sql or "",
            "source_locator": dict(r.source_locator or {}),
        }
        for r in rows
    ]


def compare_packages_view(session: Session, base_id: str, target_id: str) -> Dict[str, Any]:
    try:
        return diff_packages(session, base_id, target_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
