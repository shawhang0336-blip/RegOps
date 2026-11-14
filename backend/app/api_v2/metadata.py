import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..asset_models import FieldMetadataModel, MetadataRevisionModel, TableMetadataModel
from ..config import get_settings
from ..db import SessionLocal
from ..metadata_domain import MetadataFieldDraft, validate_metadata_draft
from ..metadata_service import (
    extract_metadata_for_block,
    metadata_to_spec,
    publish_table,
    table_detail,
    table_summary,
    update_field,
)

router = APIRouter(prefix="/metadata", tags=["metadata"])


class ExtractRequest(BaseModel):
    block_ids: List[str]


class EditRequest(BaseModel):
    expected_version: int
    editor: str = Field(min_length=1, max_length=80)
    reason: str = ""
    changes: Dict[str, Any]


class FieldCreateRequest(BaseModel):
    expected_version: int
    editor: str = Field(min_length=1, max_length=80)
    reason: str = ""
    field: Dict[str, Any]


class PublishRequest(BaseModel):
    expected_version: int
    approver: str = Field(min_length=1, max_length=80)


@router.post("/extract", status_code=202)
def extract_metadata(request: ExtractRequest) -> Dict[str, Any]:
    if not request.block_ids:
        raise HTTPException(status_code=422, detail="Select at least one raw block")
    settings = get_settings()
    with ThreadPoolExecutor(max_workers=min(4, len(request.block_ids)) ) as executor:
        table_ids = list(executor.map(lambda block_id: extract_metadata_for_block(block_id, settings), request.block_ids))
    return {"table_ids": table_ids, "status": "AI_SELF_VALIDATION_COMPLETE"}


@router.get("/tables")
def list_tables(status: Optional[str] = None) -> List[Dict[str, Any]]:
    with SessionLocal() as session:
        query = session.query(TableMetadataModel)
        if status:
            query = query.filter(TableMetadataModel.status == status)
        rows = query.order_by(TableMetadataModel.updated_at.desc()).all()
        result = []
        for row in rows:
            field_count = session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == row.id).count()
            result.append({**table_summary(row), "field_count": field_count})
        return result


@router.get("/tables/{table_id}")
def get_table(table_id: str) -> Dict[str, Any]:
    with SessionLocal() as session:
        table = session.get(TableMetadataModel, table_id)
        if not table:
            raise HTTPException(status_code=404, detail="Metadata table not found")
        return table_detail(session, table)


@router.patch("/tables/{table_id}/fields/{field_id}")
def edit_field(table_id: str, field_id: str, request: EditRequest) -> Dict[str, Any]:
    with SessionLocal() as session:
        table = session.get(TableMetadataModel, table_id)
        field = session.get(FieldMetadataModel, field_id)
        if not table or not field or field.table_id != table_id:
            raise HTTPException(status_code=404, detail="Field not found")
        try:
            update_field(session, table, field, request.changes, request.expected_version, request.editor, request.reason)
            session.commit()
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return table_detail(session, table)


@router.post("/tables/{table_id}/fields")
def add_field(table_id: str, request: FieldCreateRequest) -> Dict[str, Any]:
    with SessionLocal() as session:
        table = session.get(TableMetadataModel, table_id)
        if not table:
            raise HTTPException(status_code=404, detail="Metadata table not found")
        if table.current_version != request.expected_version:
            raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
        if table.status == "PUBLISHED":
            raise HTTPException(status_code=409, detail="PUBLISHED_TABLE_IS_IMMUTABLE")
        draft = MetadataFieldDraft.model_validate(request.field)
        field = FieldMetadataModel(
            id=str(uuid.uuid4()),
            table_id=table.id,
            sort_order=session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == table.id).count(),
            ai_value_json=draft.model_dump(),
            **draft.model_dump(),
        )
        session.add(field)
        table.current_version += 1
        table.status = "DRAFT"
        session.add(
            MetadataRevisionModel(
                id=str(uuid.uuid4()),
                table_id=table.id,
                field_id=field.id,
                revision_kind="HUMAN_ADD",
                old_value_json=None,
                new_value_json=draft.model_dump(),
                editor=request.editor,
                reason=request.reason,
            )
        )
        session.commit()
        return table_detail(session, table)


@router.delete("/tables/{table_id}/fields/{field_id}")
def delete_field(table_id: str, field_id: str, request: EditRequest) -> Dict[str, Any]:
    with SessionLocal() as session:
        table = session.get(TableMetadataModel, table_id)
        field = session.get(FieldMetadataModel, field_id)
        if not table or not field or field.table_id != table_id:
            raise HTTPException(status_code=404, detail="Field not found")
        if table.current_version != request.expected_version:
            raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
        if table.status == "PUBLISHED":
            raise HTTPException(status_code=409, detail="PUBLISHED_TABLE_IS_IMMUTABLE")
        old = {"field_en": field.field_en, "field_cn": field.field_cn}
        session.delete(field)
        table.current_version += 1
        table.status = "DRAFT"
        session.add(
            MetadataRevisionModel(
                id=str(uuid.uuid4()),
                table_id=table.id,
                field_id=field_id,
                revision_kind="HUMAN_DELETE",
                old_value_json=old,
                new_value_json={"deleted": True},
                editor=request.editor,
                reason=request.reason,
            )
        )
        session.commit()
        return table_detail(session, table)


@router.post("/tables/{table_id}/draft")
def create_draft(table_id: str, request: PublishRequest) -> Dict[str, Any]:
    with SessionLocal() as session:
        source = session.get(TableMetadataModel, table_id)
        if not source:
            raise HTTPException(status_code=404, detail="Metadata table not found")
        target = TableMetadataModel(
            id=str(uuid.uuid4()),
            document_id=source.document_id,
            block_id=source.block_id,
            table_code=source.table_code,
            table_name=source.table_name,
            table_definition=source.table_definition,
            status="DRAFT",
            current_version=source.current_version + 1,
            published_version=source.published_version,
        )
        session.add(target)
        session.flush()
        fields = session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == source.id).all()
        for field in fields:
            values = {
                key: getattr(field, key)
                for key in [
                    "field_cn",
                    "field_en",
                    "field_definition",
                    "check_text_raw",
                    "data_type",
                    "length",
                    "decimal_places",
                    "required",
                    "code_reference_text",
                    "confidence",
                    "evidence_json",
                    "sort_order",
                    "ai_value_json",
                ]
            }
            session.add(FieldMetadataModel(id=str(uuid.uuid4()), table_id=target.id, **values))
        session.add(
            MetadataRevisionModel(
                id=str(uuid.uuid4()),
                table_id=target.id,
                field_id=None,
                revision_kind="DRAFT_CREATED",
                old_value_json={"source_table_id": source.id},
                new_value_json={"version": target.current_version},
                editor=request.approver,
                reason="Create editable draft from published version",
            )
        )
        session.commit()
        return table_detail(session, target)


@router.post("/tables/{table_id}/publish")
def publish(table_id: str, request: PublishRequest) -> Dict[str, Any]:
    with SessionLocal() as session:
        table = session.get(TableMetadataModel, table_id)
        if not table:
            raise HTTPException(status_code=404, detail="Metadata table not found")
        try:
            publish_table(session, table, request.approver, request.expected_version)
            session.commit()
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return table_detail(session, table)


@router.get("/tables/{table_id}/spec")
def published_spec(table_id: str) -> Dict[str, Any]:
    with SessionLocal() as session:
        table = session.get(TableMetadataModel, table_id)
        if not table:
            raise HTTPException(status_code=404, detail="Metadata table not found")
        try:
            return metadata_to_spec(session, table).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
