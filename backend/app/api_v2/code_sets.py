import uuid
from io import BytesIO
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from openpyxl import load_workbook
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..asset_models import CodeSetModel, CodeValueModel, FieldCodeReferenceModel
from ..code_service import code_set_summary, refresh_reference_status
from ..db import SessionLocal

router = APIRouter(prefix="/code-sets", tags=["code-sets"])


class CodeSetRequest(BaseModel):
    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    definition: str = ""
    source_type: str = "MANUAL"


class VersionRequest(BaseModel):
    expected_version: int
    editor: str = Field(min_length=1, max_length=80)
    reason: str = ""
    changes: Dict[str, Any] = {}


class CodeValueRequest(BaseModel):
    expected_version: int
    editor: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1)
    label_cn: str = Field(min_length=1)
    label_en: str = ""
    definition: str = ""


def detail(session: Session, row: CodeSetModel) -> Dict[str, Any]:
    values = session.query(CodeValueModel).filter(CodeValueModel.code_set_id == row.id).order_by(CodeValueModel.sort_order, CodeValueModel.value).all()
    return {
        **code_set_summary(session, row),
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
def list_code_sets() -> List[Dict[str, Any]]:
    with SessionLocal() as session:
        rows = session.query(CodeSetModel).order_by(CodeSetModel.updated_at.desc()).all()
        return [code_set_summary(session, row) for row in rows]


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


@router.get("/references/unresolved")
def unresolved_references() -> List[Dict[str, Any]]:
    with SessionLocal() as session:
        rows = session.query(FieldCodeReferenceModel).filter(FieldCodeReferenceModel.status == "UNRESOLVED").all()
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
        refresh_reference_status(session, row.id)
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
        aliases = {"value": "value", "码值": "value", "label_cn": "label_cn", "含义": "label_cn", "中文含义": "label_cn"}
        indexes = {aliases.get(header): index for index, header in enumerate(headers) if aliases.get(header)}
        if "value" not in indexes or "label_cn" not in indexes:
            raise HTTPException(status_code=422, detail="Required columns: value/码值 and label_cn/含义")
        existing = {item.value for item in session.query(CodeValueModel).filter(CodeValueModel.code_set_id == row.id).all()}
        sort_order = len(existing)
        imported = 0
        for values in rows[1:]:
            code = str(values[indexes["value"]] or "").strip()
            label = str(values[indexes["label_cn"]] or "").strip()
            if not code or code in existing:
                continue
            session.add(
                CodeValueModel(
                    id=str(uuid.uuid4()),
                    code_set_id=row.id,
                    value=code,
                    label_cn=label,
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
