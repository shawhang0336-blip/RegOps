"""多文件监管规范包（SpecPackage）应用编排服务（供 `api/catalog/packages.py` 调用）。"""

import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.catalog.documents_service import (
    document_has_active_run,
    document_summary,
    ingest_uploaded_file_with_flag,
)
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
    TableMetadataModel,
    TableRuleEvidenceModel,
)
from app.ingestion.parsers.extractors import unpack_zip_archive
from app.ingestion.structure_service import analyze_document_structure

ALLOWED_DOC_ROLES = frozenset(
    {"AUTO", "MAIN_SPEC", "SCHEMA_APPENDIX", "CODE_SET_APPENDIX", "RULE_SUPPLEMENT", "REVISION_NOTE"}
)
ALLOWED_REGULATORS = frozenset(
    {
        "NFRA",
        "YBT",
        "EAST",
        "1104",
        "NFRA_CRS",
        "NFRA_SPECIAL",
        "PBOC",
        "PBOC_FBD",
        "PBOC_STAT",
        "PBOC_AML",
        "PBOC_CREDIT",
        "PBOC_RCPMIS",
        "SAFE",
        "SAFE_BIZ",
        "SAFE_FAL",
        "SAFE_BOP",
        "MOF_FIN",
        "CDDC_WM",
        "CSRC_CUSTODY",
        "CSRC",
        "CUSTOM",
    }
)


def _normalize_regulator(regulator: Optional[str]) -> str:
    clean = (regulator or "SAFE").strip()[:32]
    if not clean:
        return "SAFE"
    return clean.upper() if clean.isascii() else clean


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
    reg = _normalize_regulator(regulator)
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

    session.commit()
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


def remove_file_from_package(session: Session, package_id: str, document_id: str) -> Dict[str, Any]:
    pkg = session.get(SpecPackageModel, package_id)
    if not pkg or pkg.status == "DELETED":
        raise HTTPException(status_code=404, detail="Package not found")
    doc = session.get(SourceDocumentModel, document_id)
    if not doc or doc.package_id != package_id or doc.status == "DELETED":
        raise HTTPException(status_code=404, detail="Document not found in package")
    if document_has_active_run(session, doc):
        raise HTTPException(status_code=409, detail="该文档当前有后台解析任务正在运行，请先等待或中止后再移除")
    doc.status = "DELETED"
    session.commit()
    session.expire_all()
    pkg = session.get(SpecPackageModel, package_id)
    return {"package": package_summary(session, pkg), "removed_document_id": document_id}


def replace_file_in_package(
    session: Session,
    package_id: str,
    document_id: str,
    filename: Optional[str],
    content: bytes,
    *,
    doc_role: str = "AUTO",
    prepare: bool = True,
    session_factory: Optional[Callable[[], Any]] = None,
    get_settings_fn: Optional[Callable[[], Any]] = None,
    extract_document_fn: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """在规范包内替换更新某一份文档（保留原文档角色，安全软删旧文档并导入新版）。"""
    pkg = session.get(SpecPackageModel, package_id)
    if not pkg or pkg.status == "DELETED":
        raise HTTPException(status_code=404, detail="Package not found")
    old_doc = session.get(SourceDocumentModel, document_id)
    if not old_doc or old_doc.package_id != package_id or old_doc.status == "DELETED":
        raise HTTPException(status_code=404, detail="Document not found in package")
    if document_has_active_run(session, old_doc):
        raise HTTPException(status_code=409, detail="旧文档当前有后台解析任务正在运行，请先等待或中止后再替换")

    effective_role = (doc_role or "AUTO").strip().upper() or "AUTO"
    if effective_role == "AUTO" and old_doc.doc_role and old_doc.doc_role != "AUTO":
        effective_role = old_doc.doc_role

    import hashlib
    new_digest = hashlib.sha256(content).hexdigest()
    if new_digest != old_doc.sha256:
        old_doc.status = "DELETED"
        session.commit()

    new_doc, _ = ingest_uploaded_file_with_flag(
        filename,
        content,
        prepare=prepare,
        package_id=package_id,
        doc_role=effective_role,
        session_factory=session_factory or SessionLocal,
        get_settings_fn=get_settings_fn or get_settings,
        extract_document_fn=extract_document_fn,
    )
    session.commit()
    session.expire_all()
    pkg = session.get(SpecPackageModel, package_id)
    fresh_doc = session.get(SourceDocumentModel, new_doc.id) or new_doc
    return {
        "package": package_summary(session, pkg),
        "replaced_document_id": document_id,
        "document": document_summary(fresh_doc),
    }


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
    # 结束读事务快照，确保 MySQL REPEATABLE READ 下立即可见子会话提交的新块与表元数据
    session.commit()
    session.expire_all()

    # 修订说明/对照文档默认仅留痕，不将其文本混入 TableRuleEvidenceModel 核查规则库
    rule_excluded_doc_ids = {d.id for d in docs if (d.doc_role or "").upper() == "REVISION_NOTE"}
    active_doc_ids = [d.id for d in docs]
    blocks = (
        session.query(RawBlockModel)
        .filter(RawBlockModel.document_id.in_(active_doc_ids))
        .order_by(RawBlockModel.document_id, RawBlockModel.ordinal)
        .all()
    )
    extract_block_ids = [
        b.id
        for b in blocks
        if not (b.content_json or {}).get("superseded")
        and b.block_type != "IGNORE"
        and not (
            b.document_id in rule_excluded_doc_ids
            and (b.block_type == "RULE_EVIDENCE" or (b.content_json or {}).get("candidate_role") == "RULE_EVIDENCE")
        )
    ]
    if not extract_block_ids:
        raise HTTPException(status_code=422, detail="规范包内未发现可提取的内容块")

    result = extract_parallel_for_blocks(
        extract_block_ids,
        active_settings,
        targets or ["metadata", "code_sets", "rule_evidence"],
    )
    session.commit()
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


def _infer_regulator_from_filename(file_name: str) -> str:
    low = (file_name or "").lower()
    if any(k in low for k in ("一表通", "ybt")):
        return "YBT"
    if "east" in low:
        return "EAST"
    if "1104" in low or "非现场" in low:
        return "1104"
    if any(k in low for k in ("客户风险", "大额风险", "授信客户")):
        return "NFRA_CRS"
    if any(k in low for k in ("反洗钱", "aml", "大额和可疑", "可疑交易")):
        return "PBOC_AML"
    if "征信" in low:
        return "PBOC_CREDIT"
    if any(k in low for k in ("跨境人民币", "rcpmis")):
        return "PBOC_RCPMIS"
    if any(k in low for k in ("金融基础数据", "金数", "384号")):
        return "PBOC_FBD"
    if any(k in low for k in ("人民银行", "人行", "pbc", "pboc", "银发", "大集中", "全科目")):
        return "PBOC_STAT"
    if any(k in low for k in ("对外金融", "资产负债及交易", "fal")):
        return "SAFE_FAL"
    if any(k in low for k in ("国际收支", "bop", "涉外收支")):
        return "SAFE_BOP"
    if any(k in low for k in ("理财登记", "银登")):
        return "CDDC_WM"
    if any(k in low for k in ("财政部", "财务快报", "财务决算")):
        return "MOF_FIN"
    if any(k in low for k in ("证监", "csrc", "基金托管", "公募代销")):
        return "CSRC_CUSTODY"
    return "SAFE_BIZ"


def adopt_legacy_documents(session: Session) -> int:
    """将早期无 package_id 的历史遗留文档自动收编为正式规范包，并细化既有自动纳管包的监管报表归属。"""
    changed = 0
    # 1. 将早期自动纳管时归入粗粒度 SAFE/PBOC 的包细化为具体监管报表体系（幂等）
    generic_pkgs = (
        session.query(SpecPackageModel)
        .filter(
            SpecPackageModel.status != "DELETED",
            SpecPackageModel.description == "由既有历史文档自动纳管生成",
            SpecPackageModel.regulator.in_(["SAFE", "PBOC"]),
        )
        .all()
    )
    for pkg in generic_pkgs:
        inferred = _infer_regulator_from_filename(pkg.name or "")
        if inferred and inferred != pkg.regulator:
            pkg.regulator = inferred
            changed += 1

    # 2. 收编尚未归属任何规范包的历史遗留文档
    orphans = (
        session.query(SourceDocumentModel)
        .filter(SourceDocumentModel.package_id.is_(None), SourceDocumentModel.status != "DELETED")
        .order_by(SourceDocumentModel.created_at.asc())
        .all()
    )
    for doc in orphans:
        stem = Path(doc.file_name or "历史规范").stem[:255] or "历史规范"
        reg = _infer_regulator_from_filename(doc.file_name or "")
        pkg = SpecPackageModel(
            id=str(uuid.uuid4()),
            name=stem,
            regulator=reg,
            spec_version=(doc.spec_version or "1.0")[:64],
            status="ACTIVE",
            description="由既有历史文档自动纳管生成",
        )
        session.add(pkg)
        session.flush()
        doc.package_id = pkg.id
        session.query(TableMetadataModel).filter(
            TableMetadataModel.document_id == doc.id,
            TableMetadataModel.package_id.is_(None),
        ).update({TableMetadataModel.package_id: pkg.id}, synchronize_session=False)
        session.query(TableRuleEvidenceModel).filter(
            TableRuleEvidenceModel.document_id == doc.id,
            TableRuleEvidenceModel.package_id.is_(None),
        ).update({TableRuleEvidenceModel.package_id: pkg.id}, synchronize_session=False)
        changed += 1
    if changed:
        session.commit()
    return changed
