import uuid
from io import BytesIO
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from openpyxl import load_workbook
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..asset_models import CodeSetModel, CodeSetValidationModel, CodeValueModel, FieldCodeReferenceModel, FieldMetadataModel, RawBlockModel, TableMetadataModel
from ..code_service import (
    auto_link_all_references,
    code_set_summary,
    document_code_set_ids,
    document_code_set_ordinals,
    extract_parallel_for_blocks,
    refresh_reference_status,
)
from ..config import get_settings
from ..db import SessionLocal
from ..identity import OPERATOR_PLACEHOLDER
from ..structure_service import analyze_document_structure
from .documents import ingest_uploaded_file

router = APIRouter(prefix="/code-sets", tags=["code-sets"])


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


def detail(session: Session, row: CodeSetModel) -> Dict[str, Any]:
    values = session.query(CodeValueModel).filter(CodeValueModel.code_set_id == row.id).order_by(CodeValueModel.sort_order, CodeValueModel.value).all()
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
def list_code_sets(document_id: Optional[str] = None, include_placeholders: bool = False) -> List[Dict[str, Any]]:
    with SessionLocal() as session:
        query = session.query(CodeSetModel)
        local_ordinals: Dict[str, int] = {}
        if document_id:
            ids = document_code_set_ids(session, document_id)
            query = query.filter(CodeSetModel.id.in_(ids))
            local_ordinals = document_code_set_ordinals(session, document_id, ids)
        rows = query.all()
        # 单文件视图按"在本文件里出现的顺序"排；全部码表按各自的来源顺序排。
        # 定位不到来源的排最后（而不是排最前），避免一堆 NULL 打乱阅读顺序。
        def sort_key(row: CodeSetModel):
            ordinal = local_ordinals.get(row.id) if document_id else None
            if ordinal is None:
                ordinal = row.source_ordinal
            return (ordinal is None, ordinal if ordinal is not None else 0, row.code)

        rows.sort(key=sort_key)
        result = []
        for row in rows:
            summary = code_set_summary(session, row)
            # 默认隐藏"引用占位"（0 值的 DOCUMENT_REFERENCE），它们只是匹配失败的产物，不是可维护的码表
            if not include_placeholders and summary["source_type"] == "DOCUMENT_REFERENCE" and summary["value_count"] == 0:
                continue
            result.append(summary)
        return result


@router.post("", status_code=201)
def create_code_set(request: CodeSetRequest) -> Dict[str, Any]:
    with SessionLocal() as session:
        row = CodeSetModel(
            id=str(uuid.uuid4()),
            code=request.code.upper(),
            name=request.name,
            definition=request.definition,
            source_type=request.source_type,
            status="DRAFT",
            current_version=1,
        )
        session.add(row)
        session.commit()
        return detail(session, row)


@router.post("/upload-file", status_code=201)
async def upload_code_set_document(
    file: UploadFile = File(...),
    include_inline: bool = Form(default=True),
) -> Dict[str, Any]:
    """Upload PDF/DOCX/XLSX document directly in CodeSetsPage, run same block pipeline, and extract all code sets in parallel."""
    content = await file.read()
    document = ingest_uploaded_file(file.filename, content)
    settings = get_settings()
    if document.status == "UPLOADED":
        analyze_document_structure(document.id, settings)

    with SessionLocal() as session:
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
    with SessionLocal() as session:
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
def unresolved_references(document_id: Optional[str] = None) -> List[Dict[str, Any]]:
    with SessionLocal() as session:
        query = session.query(FieldCodeReferenceModel).filter(FieldCodeReferenceModel.status == "UNRESOLVED")
        if document_id:
            query = (
                query.join(FieldMetadataModel, FieldCodeReferenceModel.field_id == FieldMetadataModel.id)
                .join(TableMetadataModel, FieldMetadataModel.table_id == TableMetadataModel.id)
                .filter(TableMetadataModel.document_id == document_id)
            )
        rows = query.all()
        return [
            {
                "id": row.id,
                "field_id": row.field_id,
                "code_set_id": row.code_set_id,
                "reference_text": row.reference_text,
                "status": row.status,
            }
            for row in rows
        ]


@router.post("/references/relink")
def relink_references() -> Dict[str, Any]:
    """重新对齐"字段引用 → 码值表"，并把引用原文里自带的内联枚举补成码值。

    运维用：调整匹配规则或导入新码表后调用一次，无需重新解析文档。
    """
    with SessionLocal() as session:
        resolved = auto_link_all_references(session)
        session.commit()
        total = session.query(FieldCodeReferenceModel).count()
        unresolved = (
            session.query(FieldCodeReferenceModel)
            .filter(FieldCodeReferenceModel.status == "UNRESOLVED")
            .count()
        )
    return {"total": total, "resolved": resolved, "unresolved": unresolved}


@router.get("/{code_set_id}")
def get_code_set(code_set_id: str) -> Dict[str, Any]:
    with SessionLocal() as session:
        row = session.get(CodeSetModel, code_set_id)
        if not row:
            raise HTTPException(status_code=404, detail="Code set not found")
        return detail(session, row)


@router.patch("/{code_set_id}")
def edit_code_set(code_set_id: str, request: VersionRequest) -> Dict[str, Any]:
    with SessionLocal() as session:
        row = session.get(CodeSetModel, code_set_id)
        if not row:
            raise HTTPException(status_code=404, detail="Code set not found")
        if row.current_version != request.expected_version:
            raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
        for key in ["code", "name", "definition"]:
            if key in request.changes:
                setattr(row, key, request.changes[key])
        row.current_version += 1
        row.status = "DRAFT"
        session.commit()
        return detail(session, row)


@router.post("/{code_set_id}/values")
def add_value(code_set_id: str, request: CodeValueRequest) -> Dict[str, Any]:
    with SessionLocal() as session:
        row = session.get(CodeSetModel, code_set_id)
        if not row:
            raise HTTPException(status_code=404, detail="Code set not found")
        if row.current_version != request.expected_version:
            raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
        sort_order = session.query(CodeValueModel).filter(CodeValueModel.code_set_id == row.id).count()
        session.add(
            CodeValueModel(
                id=str(uuid.uuid4()),
                code_set_id=row.id,
                value=request.value,
                label_cn=request.label_cn,
                label_en=request.label_en,
                definition=request.definition,
                sort_order=sort_order,
                enabled=True,
                source_locator={"editor": request.editor},
            )
        )
        row.current_version += 1
        row.status = "DRAFT"
        refresh_reference_status(session, row.id)
        session.commit()
        return detail(session, row)


@router.patch("/{code_set_id}/values/{value_id}")
def edit_value(code_set_id: str, value_id: str, request: VersionRequest) -> Dict[str, Any]:
    with SessionLocal() as session:
        row = session.get(CodeSetModel, code_set_id)
        value = session.get(CodeValueModel, value_id)
        if not row or not value or value.code_set_id != row.id:
            raise HTTPException(status_code=404, detail="Code value not found")
        if row.current_version != request.expected_version:
            raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
        for key in ["value", "label_cn", "label_en", "definition", "enabled", "sort_order"]:
            if key in request.changes:
                setattr(value, key, request.changes[key])
        row.current_version += 1
        row.status = "DRAFT"
        refresh_reference_status(session, row.id)
        session.commit()
        return detail(session, row)


@router.post("/{code_set_id}/publish")
def publish_code_set(code_set_id: str, request: VersionRequest) -> Dict[str, Any]:
    with SessionLocal() as session:
        row = session.get(CodeSetModel, code_set_id)
        if not row:
            raise HTTPException(status_code=404, detail="Code set not found")
        if row.current_version != request.expected_version:
            raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
        row.status = "PUBLISHED"
        row.published_version = row.current_version
        auto_link_all_references(session)
        session.commit()
        return detail(session, row)


@router.post("/{code_set_id}/import")
async def import_values(code_set_id: str, expected_version: int = Form(...), file: UploadFile = File(...)) -> Dict[str, Any]:
    if not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=422, detail="Code import requires XLSX")
    with SessionLocal() as session:
        row = session.get(CodeSetModel, code_set_id)
        if not row:
            raise HTTPException(status_code=404, detail="Code set not found")
        if row.current_version != expected_version:
            raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
        workbook = load_workbook(BytesIO(await file.read()), data_only=True)
        sheet = workbook.active
        rows = list(sheet.iter_rows(values_only=True))
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
        existing = {item.value for item in session.query(CodeValueModel).filter(CodeValueModel.code_set_id == row.id).all()}
        sort_order = len(existing)
        imported = 0
        for values in rows[1:]:
            code = str(values[indexes["value"]] or "").strip()
            label = str(values[indexes["label_cn"]] or "").strip()
            if not code or code in existing:
                continue
            label_en = str(values[indexes["label_en"]] or "").strip() if "label_en" in indexes and indexes["label_en"] < len(values) else ""
            defn = str(values[indexes["definition"]] or "").strip() if "definition" in indexes and indexes["definition"] < len(values) else ""
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
                    source_locator={"file": file.filename},
                )
            )
            existing.add(code)
            sort_order += 1
            imported += 1
        row.current_version += 1
        row.status = "DRAFT"
        refresh_reference_status(session, row.id)
        session.commit()
        return {"imported": imported, "code_set": detail(session, row)}
