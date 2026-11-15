import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..asset_models import (
    FieldMetadataModel,
    MetadataRevisionModel,
    MetadataValidationModel,
    RawBlockModel,
    SourceDocumentModel,
    TableMetadataModel,
)
from ..code_service import extract_parallel_for_blocks
from ..config import get_settings
from ..db import SessionLocal
from ..metadata_domain import MetadataFieldDraft
from ..metadata_service import (
    metadata_to_spec,
    publish_table,
    table_detail,
    table_summary,
    update_field,
)
from ..structure_service import analyze_document_structure

router = APIRouter(prefix="/metadata", tags=["metadata"])


class ExtractRequest(BaseModel):
    block_ids: List[str]
    targets: Optional[List[str]] = None


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
    with SessionLocal() as session:
        blocks = session.query(RawBlockModel).filter(RawBlockModel.id.in_(request.block_ids)).all()
        if any(block.content_json.get("superseded") for block in blocks):
            raise HTTPException(status_code=409, detail="Superseded raw blocks cannot be extracted")
        doc_ids_to_analyze = set()
        for block in blocks:
            document = session.get(SourceDocumentModel, block.document_id)
            if document and document.status == "UPLOADED":
                doc_ids_to_analyze.add(document.id)
    for doc_id in doc_ids_to_analyze:
        analyze_document_structure(doc_id, settings)

    result = extract_parallel_for_blocks(request.block_ids, settings, request.targets or ["metadata", "code_sets"])
    return {
        "table_ids": result["table_ids"],
        "code_set_ids": result["code_set_ids"],
        "resolved_reference_count": result["resolved_reference_count"],
        "errors": result["errors"],
        "status": result["status"],
    }


@router.get("/tables")
def list_tables(status: Optional[str] = None, document_id: Optional[str] = None) -> List[Dict[str, Any]]:
    with SessionLocal() as session:
        query = session.query(TableMetadataModel)
        if status:
            query = query.filter(TableMetadataModel.status == status)
        if document_id:
            query = query.filter(TableMetadataModel.document_id == document_id)
        rows = query.order_by(TableMetadataModel.updated_at.desc()).all()
        # 表级状态是"任意一条告警就整表待复核"，列表里必须把问题落到条数和字段上，
        # 否则用户只看到一个红色的"需要核对"，无法判断到底有几处、要不要动手。
        stats: Dict[str, Dict[str, Any]] = {}
        for validation in session.query(MetadataValidationModel).all():
            entry = stats.setdefault(validation.table_id, {"issue_count": 0, "fields": set(), "codes": {}})
            for issue in validation.issues_json or []:
                entry["issue_count"] += 1
                code = str(issue.get("code") or "").strip()
                if code:
                    entry["codes"][code] = entry["codes"].get(code, 0) + 1
                field_en = str(issue.get("field_en") or "").strip()
                if field_en:
                    entry["fields"].add(field_en)
        result = []
        for row in rows:
            field_count = session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == row.id).count()
            entry = stats.get(row.id) or {}
            top_codes = sorted(
                (entry.get("codes") or {}).items(),
                key=lambda item: item[1],
                reverse=True,
            )[:3]
            result.append(
                {
                    **table_summary(row),
                    "field_count": field_count,
                    "review_issue_count": entry.get("issue_count", 0),
                    "review_field_count": len(entry.get("fields") or set()),
                    "review_top_codes": [code for code, _ in top_codes],
                }
            )
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
