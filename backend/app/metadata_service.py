import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from .asset_models import (
    FieldMetadataModel,
    MetadataRevisionModel,
    MetadataValidationModel,
    RawBlockModel,
    SourceDocumentModel,
    TableMetadataModel,
)
from .config import Settings
from .db import SessionLocal
from .domain import FieldSpec, SpecDocument
from .generic_ai import RealDesignLlm
from .generic_workflow import _call_validated
from .metadata_domain import MetadataTableDraft, require_valid_metadata, validate_metadata_draft


def _field_view(field: FieldMetadataModel) -> Dict[str, Any]:
    return {
        "id": field.id,
        "field_cn": field.field_cn,
        "field_en": field.field_en,
        "field_definition": field.field_definition,
        "check_text_raw": field.check_text_raw,
        "data_type": field.data_type,
        "length": field.length,
        "decimal_places": field.decimal_places,
        "required": field.required,
        "code_reference_text": field.code_reference_text,
        "confidence": field.confidence,
        "evidence_json": field.evidence_json,
        "sort_order": field.sort_order,
        "ai_value_json": field.ai_value_json,
    }


def table_summary(row: TableMetadataModel) -> Dict[str, Any]:
    return {
        "id": row.id,
        "document_id": row.document_id,
        "block_id": row.block_id,
        "table_code": row.table_code,
        "table_name": row.table_name,
        "table_definition": row.table_definition,
        "status": row.status,
        "current_version": row.current_version,
        "published_version": row.published_version,
        "updated_at": row.updated_at.isoformat(),
    }


def table_detail(session: Session, table: TableMetadataModel) -> Dict[str, Any]:
    fields = (
        session.query(FieldMetadataModel)
        .filter(FieldMetadataModel.table_id == table.id)
        .order_by(FieldMetadataModel.sort_order)
        .all()
    )
    validations = (
        session.query(MetadataValidationModel)
        .filter(MetadataValidationModel.table_id == table.id)
        .order_by(MetadataValidationModel.created_at)
        .all()
    )
    revisions = (
        session.query(MetadataRevisionModel)
        .filter(MetadataRevisionModel.table_id == table.id)
        .order_by(MetadataRevisionModel.created_at.desc())
        .limit(30)
        .all()
    )
    return {
        **table_summary(table),
        "fields": [_field_view(field) for field in fields],
        "validations": [
            {
                "validator_kind": item.validator_kind,
                "round": item.round,
                "status": item.status,
                "issues": item.issues_json,
                "created_at": item.created_at.isoformat(),
            }
            for item in validations
        ],
        "revisions": [
            {
                "revision_kind": item.revision_kind,
                "field_id": item.field_id,
                "old_value": item.old_value_json,
                "new_value": item.new_value_json,
                "editor": item.editor,
                "reason": item.reason,
                "created_at": item.created_at.isoformat(),
            }
            for item in revisions
        ],
    }


def _persist_draft(session: Session, block: RawBlockModel, draft: MetadataTableDraft, trace: Dict[str, Any]) -> TableMetadataModel:
    table = TableMetadataModel(
        id=str(uuid.uuid4()),
        document_id=block.document_id,
        block_id=block.id,
        table_code=draft.table_code,
        table_name=draft.table_name,
        table_definition=draft.table_definition,
        status="DRAFT",
        current_version=1,
    )
    session.add(table)
    session.flush()
    for index, field in enumerate(draft.fields):
        values = field.model_dump()
        if not values["evidence_json"]:
            values["evidence_json"] = block.source_locator
        session.add(FieldMetadataModel(id=str(uuid.uuid4()), table_id=table.id, sort_order=index, ai_value_json=values, **values))
    session.add(
        MetadataRevisionModel(
            id=str(uuid.uuid4()),
            table_id=table.id,
            field_id=None,
            revision_kind="AI_EXTRACTION",
            old_value_json=None,
            new_value_json={"draft": draft.model_dump(), "trace": trace},
            editor="AI",
            reason="AI metadata assignment",
        )
    )
    session.flush()
    return table


def extract_metadata_for_block(block_id: str, settings: Settings) -> str:
    with SessionLocal() as session:
        block = session.get(RawBlockModel, block_id)
        if not block:
            raise ValueError("Raw block not found")
        llm = RealDesignLlm(settings)
        source_spec = block.content_json.get("spec") or {}
        raw_fields = source_spec.get("fields")
        if raw_fields is None:
            rows = block.content_json.get("rows", [])
            raw_fields = [{"row": row, "headers": block.content_json.get("headers", [])} for row in rows[1:]]
        if not raw_fields:
            raise ValueError("No metadata field candidates were found")
        fallback_code = source_spec.get("table_code") or f"TABLE_{block.id.replace('-', '')[:12].upper()}"
        fallback_name = source_spec.get("table_name") or f"原始表格 {block.ordinal + 1}"

        def bind_field(raw_field):
            payload = {
                "source_locator": block.source_locator,
                "table": {"table_code": fallback_code, "table_name": fallback_name},
                "raw_field": raw_field,
                "raw_context": block.raw_content[:6000],
            }

            def validate(args):
                candidate = {
                    "table_code": fallback_code,
                    "table_name": fallback_name,
                    "table_definition": args.get("table_definition", ""),
                    "fields": args.get("fields", []),
                }
                draft = MetadataTableDraft.model_validate(candidate)
                if len(draft.fields) != 1:
                    raise ValueError("Each metadata call must return exactly one field")
                require_valid_metadata(draft)
                return draft

            return _call_validated(
                llm,
                "assign_metadata",
                "只装配输入的一个字段，并给出表定义。字段中文名、英文名、定义、校验原文、类型和长度必须来自证据，不得虚构码值。",
                payload,
                validate,
            )

        with ThreadPoolExecutor(max_workers=min(4, len(raw_fields))) as executor:
            results = list(executor.map(bind_field, raw_fields))
        fields = [draft.fields[0] for draft, _ in results]
        table_definition = next((draft.table_definition for draft, _ in results if draft.table_definition), "")
        draft = MetadataTableDraft(
            table_code=fallback_code,
            table_name=fallback_name,
            table_definition=table_definition,
            fields=fields,
        )
        require_valid_metadata(draft)
        trace = {
            "provider": "GLM",
            "model": settings.llm_model,
            "batch_count": len(results),
            "batches": [trace for _, trace in results],
            "arguments": draft.model_dump(),
        }
        table = _persist_draft(session, block, draft, trace)
        program_issues = validate_metadata_draft(draft)
        session.add(
            MetadataValidationModel(
                id=str(uuid.uuid4()),
                table_id=table.id,
                validator_kind="PROGRAM",
                round=1,
                status="FAILED" if any(item["severity"] == "ERROR" for item in program_issues) else "PASSED",
                issues_json=program_issues,
            )
        )
        try:
            def review_field(field_draft):
                payload = {
                    "source_locator": block.source_locator,
                    "table": {"table_code": draft.table_code, "table_name": draft.table_name},
                    "current_field": field_draft.model_dump(),
                    "raw_context": block.raw_content[:6000],
                }

                def validate_review(args):
                    candidate = {
                        "table_code": draft.table_code,
                        "table_name": draft.table_name,
                        "table_definition": args.get("table_definition", draft.table_definition),
                        "fields": args.get("fields", []),
                    }
                    reviewed = MetadataTableDraft.model_validate(candidate)
                    if len(reviewed.fields) != 1:
                        raise ValueError("Each review call must return exactly one field")
                    require_valid_metadata(reviewed)
                    return reviewed

                return _call_validated(
                    llm,
                    "assign_metadata",
                    "独立复核当前字段，检查遗漏、错配、无依据必填和码值虚构，返回复核后的一个字段及表定义。",
                    payload,
                    validate_review,
                )

            with ThreadPoolExecutor(max_workers=min(4, len(draft.fields))) as executor:
                review_results = list(executor.map(review_field, draft.fields))
            issues = []
            for original, (reviewed, _) in zip(draft.fields, review_results):
                revised = reviewed.fields[0]
                if original.model_dump() != revised.model_dump():
                    issues.append(
                        {
                            "severity": "WARNING",
                            "code": "AI_REVIEW_DIFF",
                            "field_en": original.field_en,
                            "message": f"AI 初稿与复核稿不同：初稿={original.model_dump()}；复核={revised.model_dump()}",
                        }
                    )
            review_trace = {
                "provider": "GLM",
                "model": settings.llm_model,
                "batch_count": len(review_results),
                "batches": [trace for _, trace in review_results],
            }
        except Exception as exc:
            review_trace = {"error": f"{type(exc).__name__}: {exc}"}
            issues = [
                {
                    "severity": "ERROR",
                    "code": "AI_REVIEW_UNAVAILABLE",
                    "field_en": "",
                    "message": review_trace["error"],
                }
            ]
        session.add(
            MetadataValidationModel(
                id=str(uuid.uuid4()),
                table_id=table.id,
                validator_kind="AI_REVIEW",
                round=1,
                status="FAILED" if any(item["severity"] == "ERROR" for item in issues) else "PASSED",
                issues_json=issues,
            )
        )
        session.add(
            MetadataRevisionModel(
                id=str(uuid.uuid4()),
                table_id=table.id,
                field_id=None,
                revision_kind="AI_REVIEW",
                old_value_json=None,
                new_value_json={"issues": issues, "trace": review_trace},
                editor="AI_REVIEWER",
                reason="Independent metadata review",
            )
        )
        table.status = "NEEDS_REVIEW" if any(item["severity"] == "ERROR" for item in program_issues + issues) else "AI_SELF_VALIDATED"
        session.commit()
        return table.id


def update_field(
    session: Session,
    table: TableMetadataModel,
    field: FieldMetadataModel,
    changes: Dict[str, Any],
    expected_version: int,
    editor: str,
    reason: str,
) -> None:
    if table.current_version != expected_version:
        raise ValueError("VERSION_CONFLICT")
    if table.status == "PUBLISHED":
        raise ValueError("PUBLISHED_TABLE_IS_IMMUTABLE")
    allowed = {
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
    }
    if any(key not in allowed for key in changes):
        raise ValueError("INVALID_FIELD_CHANGE")
    old = _field_view(field)
    for key, value in changes.items():
        setattr(field, key, value)
    table.current_version += 1
    table.status = "DRAFT"
    session.add(
        MetadataRevisionModel(
            id=str(uuid.uuid4()),
            table_id=table.id,
            field_id=field.id,
            revision_kind="HUMAN_EDIT",
            old_value_json=old,
            new_value_json=_field_view(field),
            editor=editor,
            reason=reason,
        )
    )
    session.flush()


def publish_table(session: Session, table: TableMetadataModel, approver: str, expected_version: int) -> None:
    if table.current_version != expected_version:
        raise ValueError("VERSION_CONFLICT")
    fields = session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == table.id).order_by(FieldMetadataModel.sort_order).all()
    draft = MetadataTableDraft(
        table_code=table.table_code,
        table_name=table.table_name,
        table_definition=table.table_definition,
        fields=[
            {
                key: value
                for key, value in _field_view(field).items()
                if key not in {"id", "sort_order", "ai_value_json"}
            }
            for field in fields
        ],
    )
    require_valid_metadata(draft)
    from .code_service import ensure_table_references

    ensure_table_references(session, table)
    table.status = "PUBLISHED"
    table.published_version = table.current_version
    session.add(
        MetadataRevisionModel(
            id=str(uuid.uuid4()),
            table_id=table.id,
            field_id=None,
            revision_kind="PUBLISHED",
            old_value_json=None,
            new_value_json={"version": table.current_version},
            editor=approver,
            reason="Human published immutable metadata",
        )
    )
    session.flush()


def metadata_to_spec(session: Session, table: TableMetadataModel) -> SpecDocument:
    if table.status != "PUBLISHED" or table.published_version != table.current_version:
        raise ValueError("TABLE_NOT_PUBLISHED")
    document = session.get(SourceDocumentModel, table.document_id)
    block = session.get(RawBlockModel, table.block_id)
    fields = session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == table.id).order_by(FieldMetadataModel.sort_order).all()
    return SpecDocument(
        version=document.spec_version,
        table_code=table.table_code,
        table_name=table.table_name,
        source_name=document.file_name,
        source_sha256=document.sha256,
        section=str(block.source_locator.get("section", table.table_code)),
        fields=[
            FieldSpec(
                name=field.field_en,
                content=field.field_cn,
                data_type=field.data_type,
                length=field.length,
                decimal_places=field.decimal_places,
                check_text=field.check_text_raw,
                evidence_page=str(field.evidence_json.get("page", field.evidence_json)),
            )
            for field in fields
        ],
        terms_text=block.raw_content,
        dictionary_text=block.raw_content,
    )
