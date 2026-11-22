import uuid
from io import BytesIO
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from openpyxl import load_workbook
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import db_session
from app.api.v2.documents import ingest_uploaded_file
from app.catalog.code_sets.readiness import evaluate_code_set, evaluate_code_sets, is_reference_placeholder
from app.catalog.code_sets.service import (
    auto_link_all_references,
    code_set_summaries,
    code_set_summary,
    document_code_set_ids,
    document_code_set_ordinals,
    refresh_reference_status,
    remove_code_set,
)
from app.catalog.extraction import extract_parallel_for_blocks
from app.infra.db_tables import (
    CodeSetModel,
    CodeSetValidationModel,
    CodeValueModel,
    FieldCodeReferenceModel,
    FieldMetadataModel,
    RawBlockModel,
    SourceDocumentModel,
    TableMetadataModel,
)
from app.infra.config import get_settings
from app.infra.config import OPERATOR_PLACEHOLDER
from app.ingestion.structure_service import analyze_document_structure

router = APIRouter(prefix="/code-sets", tags=["code-sets"])

_MAX_IMPORT_BYTES = 30 * 1024 * 1024


class CodeSetRequest(BaseModel):
    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    definition: str = ""
    source_type: str = "MANUAL"


class VersionRequest(BaseModel):
    expected_version: int
    editor: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)
    reason: str = ""
    changes: Dict[str, Any] = {}


class CodeValueRequest(BaseModel):
    expected_version: int
    editor: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)
    value: str = Field(min_length=1)
    label_cn: str = Field(min_length=1)
    label_en: str = ""
    definition: str = ""


class PublishBatchRequest(BaseModel):
    document_id: Optional[str] = None
    code_set_ids: Optional[List[str]] = None
    referenced_only: bool = True
    dry_run: bool = False


def detail(session: Session, row: CodeSetModel) -> Dict[str, Any]:
    values = (
        session.query(CodeValueModel)
        .filter(CodeValueModel.code_set_id == row.id)
        .order_by(CodeValueModel.sort_order, CodeValueModel.value)
        .all()
    )
    validations = (
        session.query(CodeSetValidationModel)
        .filter(CodeSetValidationModel.code_set_id == row.id)
        .order_by(CodeSetValidationModel.created_at.desc())
        .limit(20)
        .all()
    )
    return {
        **code_set_summary(session, row),
        "validations": [
            {
                "validator_kind": validation.validator_kind,
                "status": validation.status,
                "issues": validation.issues_json,
                "trace": validation.trace_json,
                "created_at": validation.created_at.isoformat(),
            }
            for validation in validations
        ],
        "values": [
            {
                "id": value.id,
                "value": value.value,
                "label_cn": value.label_cn,
                "label_en": value.label_en,
                "definition": value.definition,
                "sort_order": value.sort_order,
                "enabled": value.enabled,
                "source_locator": value.source_locator,
            }
            for value in values
        ],
    }


@router.get("")
def list_code_sets(
    document_id: Optional[str] = None,
    include_placeholders: bool = False,
    session: Session = Depends(db_session),
) -> List[Dict[str, Any]]:
    query = session.query(CodeSetModel)
    local_ordinals: Dict[str, int] = {}
    if document_id:
        ids = document_code_set_ids(session, document_id)
        query = query.filter(CodeSetModel.id.in_(ids))
        local_ordinals = document_code_set_ordinals(session, document_id, ids)
    rows = query.all()

    def sort_key(row: CodeSetModel):
        ordinal = local_ordinals.get(row.id) if document_id else None
        if ordinal is None:
            ordinal = row.source_ordinal
        return (ordinal is None, ordinal if ordinal is not None else 0, row.code)

    rows.sort(key=sort_key)
    summaries = {item["id"]: item for item in code_set_summaries(session, rows)}
    result = []
    for row in rows:
        summary = summaries[row.id]
        if not include_placeholders and is_reference_placeholder(summary):
            continue
        result.append(summary)
    return result


@router.post("", status_code=201)
def create_code_set(request: CodeSetRequest, session: Session = Depends(db_session)) -> Dict[str, Any]:
    code = request.code.strip().upper()
    name = request.name.strip()
    if not code or not name:
        raise HTTPException(status_code=422, detail="码表代码与名称不能为空")
    existing = (
        session.query(CodeSetModel.id)
        .filter(func.upper(CodeSetModel.code) == code, CodeSetModel.current_version == 1)
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail=f"码表代码 {code} 已存在")
    row = CodeSetModel(
        id=str(uuid.uuid4()),
        code=code,
        name=name,
        definition=request.definition,
        source_type=request.source_type,
        status="DRAFT",
        current_version=1,
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=f"码表代码 {code} 已存在") from exc
    return detail(session, row)


@router.post("/upload-file", status_code=201)
async def upload_code_set_document(
    file: UploadFile = File(...),
    include_inline: bool = Form(default=True),
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    """Upload PDF/DOCX/XLSX document directly in CodeSetsPage and extract all code sets in parallel."""
    content = await file.read()
    document = ingest_uploaded_file(file.filename, content)
    settings = get_settings()
    if document.status == "UPLOADED":
        analyze_document_structure(document.id, settings)

    session.expire_all()
    blocks = (
        session.query(RawBlockModel)
        .filter(RawBlockModel.document_id == document.id)
        .order_by(RawBlockModel.ordinal)
        .all()
    )
    target_block_ids = [
        b.id
        for b in blocks
        if not b.content_json.get("superseded")
        and (
            b.block_type in {"CODE_SET", "CODE_SET_CANDIDATE"}
            or b.content_json.get("candidate_role") == "CODE_SET"
            or b.content_json.get("code_set_spec")
            or (include_inline and b.content_json.get("inline_code_sets"))
        )
    ]

    if not target_block_ids:
        raise HTTPException(status_code=422, detail="未在上传文件中发现可提取的码值表或内联枚举项")

    result = extract_parallel_for_blocks(target_block_ids, settings, targets=["code_sets"])
    session.expire_all()
    extracted_sets = [
        code_set_summary(session, session.get(CodeSetModel, cid))
        for cid in result["code_set_ids"]
        if session.get(CodeSetModel, cid)
    ]
    total_values = sum(item["value_count"] for item in extracted_sets)

    return {
        "document_id": document.id,
        "file_name": document.file_name,
        "code_set_ids": result["code_set_ids"],
        "code_sets": extracted_sets,
        "code_set_count": len(extracted_sets),
        "total_value_count": total_values,
        "resolved_reference_count": result["resolved_reference_count"],
        "errors": result["errors"],
        "status": result["status"],
    }


@router.get("/references/unresolved")
def unresolved_references(
    document_id: Optional[str] = None,
    session: Session = Depends(db_session),
) -> List[Dict[str, Any]]:
    """返回尚未解析到码表的字段引用（附带所属报表与字段信息）。"""
    query = (
        session.query(FieldCodeReferenceModel, FieldMetadataModel, TableMetadataModel)
        .join(FieldMetadataModel, FieldCodeReferenceModel.field_id == FieldMetadataModel.id)
        .join(TableMetadataModel, FieldMetadataModel.table_id == TableMetadataModel.id)
        .outerjoin(SourceDocumentModel, TableMetadataModel.document_id == SourceDocumentModel.id)
        .filter(
            FieldCodeReferenceModel.status == "UNRESOLVED",
            TableMetadataModel.status != "MERGED",
            or_(SourceDocumentModel.id.is_(None), SourceDocumentModel.status != "DELETED"),
        )
    )
    if document_id:
        query = query.filter(TableMetadataModel.document_id == document_id)
    rows = query.all()
    return [
        {
            "id": reference.id,
            "field_id": reference.field_id,
            "code_set_id": reference.code_set_id,
            "reference_text": reference.reference_text,
            "status": reference.status,
            "table_id": table.id,
            "table_code": table.table_code,
            "table_name": table.table_name,
            "field_en": field.field_en,
            "field_cn": field.field_cn,
        }
        for reference, field, table in rows
    ]


@router.post("/references/relink")
def relink_references(session: Session = Depends(db_session)) -> Dict[str, Any]:
    """重新对齐“字段引用 → 码值表”，并把引用原文里自带的内联枚举补成码值。"""
    resolved = auto_link_all_references(session)
    session.commit()
    total = session.query(FieldCodeReferenceModel).count()
    unresolved = (
        session.query(FieldCodeReferenceModel)
        .filter(FieldCodeReferenceModel.status == "UNRESOLVED")
        .count()
    )
    return {"total": total, "resolved": resolved, "unresolved": unresolved}


@router.get("/readiness")
def code_set_readiness(
    document_id: Optional[str] = None,
    referenced_only: bool = False,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    """码表发布前的检查：空表、缺中文含义、码值超过字段长度。"""
    return evaluate_code_sets(session, document_id=document_id, referenced_only=referenced_only)


@router.post("/publish-batch")
def publish_code_sets_batch(
    request: PublishBatchRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    """批量发布码表（仅发布检查通过项，其余连原因一起返回）。"""
    published: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    query = session.query(CodeSetModel)
    if request.code_set_ids:
        query = query.filter(CodeSetModel.id.in_(request.code_set_ids))
    elif request.document_id:
        ids = document_code_set_ids(session, request.document_id)
        query = query.filter(CodeSetModel.id.in_(ids))
    rows = query.order_by(CodeSetModel.source_ordinal.asc(), CodeSetModel.code.asc()).all()
    for row in rows:
        result = evaluate_code_set(session, row)
        if request.referenced_only and result["reference_count"] == 0:
            skipped.append({"code": row.code, "reason": "NOT_REFERENCED"})
            continue
        if row.status == "PUBLISHED" and row.published_version == row.current_version:
            skipped.append({"code": row.code, "reason": "ALREADY_PUBLISHED"})
            continue
        if not result["ready"]:
            skipped.append(
                {
                    "code": row.code,
                    "reason": result["blockers"][0]["code"],
                    "detail": result["blockers"][0]["message"],
                }
            )
            continue
        if not request.dry_run:
            row.status = "PUBLISHED"
            row.published_version = row.current_version
        published.append({"code": row.code, "value_count": result["value_count"], "referenced": result["reference_count"]})
    if request.dry_run:
        session.rollback()
    else:
        session.commit()
    return {
        "published": len(published),
        "skipped": len(skipped),
        "details": published[:100],
        "skipped_details": skipped[:100],
        "dry_run": request.dry_run,
    }


@router.get("/{code_set_id}")
def get_code_set(code_set_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    row = session.get(CodeSetModel, code_set_id)
    if not row:
        raise HTTPException(status_code=404, detail="Code set not found")
    return detail(session, row)


@router.patch("/{code_set_id}")
def edit_code_set(
    code_set_id: str,
    request: VersionRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    row = session.get(CodeSetModel, code_set_id)
    if not row:
        raise HTTPException(status_code=404, detail="Code set not found")
    if row.current_version != request.expected_version:
        raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
    for key in ["code", "name", "definition"]:
        if key in request.changes:
            val = request.changes[key]
            if key == "code":
                cleaned = str(val or "").strip().upper()
                if not cleaned:
                    raise HTTPException(status_code=422, detail="码表代码不能为空")
                setattr(row, key, cleaned)
            elif key == "name":
                cleaned = str(val or "").strip()
                if not cleaned:
                    raise HTTPException(status_code=422, detail="码表名称不能为空")
                setattr(row, key, cleaned)
            else:
                setattr(row, key, "" if val is None else str(val))
    row.current_version += 1
    row.status = "DRAFT"
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=f"码表代码 {row.code} 与当前版本冲突") from exc
    return detail(session, row)


@router.delete("/{code_set_id}")
def delete_code_set(code_set_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    """删掉一张未被字段引用的码表。"""
    row = session.get(CodeSetModel, code_set_id)
    if not row:
        raise HTTPException(status_code=404, detail="Code set not found")
    try:
        result = remove_code_set(session, row)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "CODE_SET_IN_USE", "message": str(exc)},
        ) from exc
    session.commit()
    return result


@router.post("/{code_set_id}/values")
def add_value(
    code_set_id: str,
    request: CodeValueRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    row = session.get(CodeSetModel, code_set_id)
    if not row:
        raise HTTPException(status_code=404, detail="Code set not found")
    if row.current_version != request.expected_version:
        raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
    code_val = request.value.strip()
    label_cn = request.label_cn.strip()
    if not code_val or not label_cn:
        raise HTTPException(status_code=422, detail="码值和中文含义不能为空")
    existing_ci = {
        (val or "").strip().upper()
        for (val,) in session.query(CodeValueModel.value).filter(CodeValueModel.code_set_id == row.id).all()
    }
    if code_val.upper() in existing_ci:
        raise HTTPException(status_code=409, detail=f"码值 {code_val} 已存在（忽略大小写重复）")
    max_order = (
        session.query(func.max(CodeValueModel.sort_order))
        .filter(CodeValueModel.code_set_id == row.id)
        .scalar()
    )
    sort_order = (max_order + 1) if max_order is not None else 0
    session.add(
        CodeValueModel(
            id=str(uuid.uuid4()),
            code_set_id=row.id,
            value=code_val,
            label_cn=label_cn,
            label_en=request.label_en.strip(),
            definition=request.definition.strip(),
            sort_order=sort_order,
            enabled=True,
            source_locator={"editor": request.editor},
        )
    )
    row.current_version += 1
    row.status = "DRAFT"
    refresh_reference_status(session, row.id)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=f"码值 {code_val} 已存在") from exc
    return detail(session, row)


@router.patch("/{code_set_id}/values/{value_id}")
def edit_value(
    code_set_id: str,
    value_id: str,
    request: VersionRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    row = session.get(CodeSetModel, code_set_id)
    value = session.get(CodeValueModel, value_id)
    if not row or not value or value.code_set_id != row.id:
        raise HTTPException(status_code=404, detail="Code value not found")
    if row.current_version != request.expected_version:
        raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
    if "value" in request.changes:
        new_val = str(request.changes["value"] or "").strip()
        if not new_val:
            raise HTTPException(status_code=422, detail="码值不能为空")
        other_ci = {
            (val or "").strip().upper()
            for (vid, val) in session.query(CodeValueModel.id, CodeValueModel.value)
            .filter(CodeValueModel.code_set_id == row.id)
            .all()
            if vid != value.id
        }
        if new_val.upper() in other_ci:
            raise HTTPException(status_code=409, detail=f"码值 {new_val} 已存在（忽略大小写重复）")
        value.value = new_val
    if "label_cn" in request.changes:
        new_label = str(request.changes["label_cn"] or "").strip()
        if not new_label:
            raise HTTPException(status_code=422, detail="中文含义不能为空")
        value.label_cn = new_label
    for key in ["label_en", "definition", "enabled", "sort_order"]:
        if key in request.changes:
            setattr(value, key, request.changes[key])
    row.current_version += 1
    row.status = "DRAFT"
    refresh_reference_status(session, row.id)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=f"码值 {value.value} 已存在") from exc
    return detail(session, row)


@router.post("/{code_set_id}/publish")
def publish_code_set(
    code_set_id: str,
    request: VersionRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    row = session.get(CodeSetModel, code_set_id)
    if not row:
        raise HTTPException(status_code=404, detail="Code set not found")
    if row.current_version != request.expected_version:
        raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
    result = evaluate_code_set(session, row)
    if not result["ready"]:
        blocker = result["blockers"][0]
        raise HTTPException(
            status_code=409,
            detail={"code": blocker["code"], "message": blocker["message"]},
        )
    row.status = "PUBLISHED"
    row.published_version = row.current_version
    auto_link_all_references(session)
    session.commit()
    return detail(session, row)


def _cell_text(values: tuple, idx: Optional[int]) -> str:
    if idx is None or idx >= len(values) or values[idx] is None:
        return ""
    return str(values[idx]).strip()


@router.post("/{code_set_id}/import")
async def import_values(
    code_set_id: str,
    expected_version: int = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    filename = file.filename or ""
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=422, detail="Code import requires XLSX")
    row = session.get(CodeSetModel, code_set_id)
    if not row:
        raise HTTPException(status_code=404, detail="Code set not found")
    if row.current_version != expected_version:
        raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
    content = await file.read()
    if len(content) > _MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds the 30 MB limit")
    try:
        workbook = load_workbook(BytesIO(content), data_only=True)
        sheet = workbook.active
        rows = list(sheet.iter_rows(values_only=True)) if sheet is not None else []
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"无法解析 Excel 文件：{type(exc).__name__}") from exc
    if not rows:
        raise HTTPException(status_code=422, detail="Empty workbook")
    headers = [str(value or "").strip() for value in rows[0]]
    aliases = {
        "value": "value", "码值": "value", "代码": "value", "编码": "value",
        "label_cn": "label_cn", "含义": "label_cn", "中文含义": "label_cn", "名称": "label_cn", "中文名称": "label_cn",
        "label_en": "label_en", "英文含义": "label_en", "英文名称": "label_en",
        "definition": "definition", "说明": "definition", "备注": "definition",
    }
    indexes = {aliases.get(header): index for index, header in enumerate(headers) if aliases.get(header)}
    if "value" not in indexes or "label_cn" not in indexes:
        raise HTTPException(status_code=422, detail="Required columns: value/码值/代码 and label_cn/含义/名称")
    # MySQL 8 默认 utf8mb4_0900_ai_ci 对 uq_code_value 忽略大小写，Python 端统一按大写去重避免 1062 崩溃
    existing_ci = {
        (item.value or "").strip().upper()
        for item in session.query(CodeValueModel).filter(CodeValueModel.code_set_id == row.id).all()
    }
    max_order = (
        session.query(func.max(CodeValueModel.sort_order))
        .filter(CodeValueModel.code_set_id == row.id)
        .scalar()
    )
    sort_order = (max_order + 1) if max_order is not None else 0
    imported = 0
    skipped_duplicate = 0
    skipped_empty_label = 0
    for values in rows[1:]:
        code = _cell_text(values, indexes["value"])
        label = _cell_text(values, indexes["label_cn"])
        if not code:
            continue
        if not label:
            skipped_empty_label += 1
            continue
        code_ci = code.upper()
        if code_ci in existing_ci:
            skipped_duplicate += 1
            continue
        label_en = _cell_text(values, indexes.get("label_en"))
        defn = _cell_text(values, indexes.get("definition"))
        session.add(
            CodeValueModel(
                id=str(uuid.uuid4()),
                code_set_id=row.id,
                value=code,
                label_cn=label,
                label_en=label_en,
                definition=defn,
                sort_order=sort_order,
                enabled=True,
                source_locator={"file": filename},
            )
        )
        existing_ci.add(code_ci)
        sort_order += 1
        imported += 1
    row.current_version += 1
    row.status = "DRAFT"
    refresh_reference_status(session, row.id)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="导入包含重复码值") from exc
    return {
        "imported": imported,
        "skipped_duplicate": skipped_duplicate,
        "skipped_empty_label": skipped_empty_label,
        "code_set": detail(session, row),
    }
