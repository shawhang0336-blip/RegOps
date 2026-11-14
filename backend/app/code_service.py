import re
import uuid
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from .asset_models import CodeSetModel, CodeValueModel, FieldCodeReferenceModel, FieldMetadataModel, TableMetadataModel


def code_set_summary(session: Session, row: CodeSetModel) -> Dict[str, Any]:
    value_count = session.query(CodeValueModel).filter(CodeValueModel.code_set_id == row.id).count()
    enabled_count = session.query(CodeValueModel).filter(CodeValueModel.code_set_id == row.id, CodeValueModel.enabled.is_(True)).count()
    unresolved = session.query(FieldCodeReferenceModel).filter(
        FieldCodeReferenceModel.code_set_id == row.id,
        FieldCodeReferenceModel.status == "UNRESOLVED",
    ).count()
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "definition": row.definition,
        "source_type": row.source_type,
        "status": row.status,
        "current_version": row.current_version,
        "published_version": row.published_version,
        "value_count": value_count,
        "enabled_value_count": enabled_count,
        "unresolved_reference_count": unresolved,
    }


def refresh_reference_status(session: Session, code_set_id: str) -> None:
    session.flush()
    has_values = session.query(CodeValueModel).filter(
        CodeValueModel.code_set_id == code_set_id,
        CodeValueModel.enabled.is_(True),
    ).count() > 0
    references = session.query(FieldCodeReferenceModel).filter(FieldCodeReferenceModel.code_set_id == code_set_id).all()
    for reference in references:
        reference.status = "RESOLVED" if has_values else "UNRESOLVED"
    session.flush()


def ensure_field_reference(session: Session, field: FieldMetadataModel) -> None:
    text = field.code_reference_text.strip()
    if not text or text.upper() in {"无", "N/A", "NONE", "无引用"}:
        return
    code_set = session.query(CodeSetModel).filter(CodeSetModel.name == text).one_or_none()
    if not code_set:
        code = re.sub(r"[^A-Z0-9_]", "_", f"{field.field_en}_CODESET".upper())
        code_set = CodeSetModel(
            id=str(uuid.uuid4()),
            code=code,
            name=text,
            definition=f"由字段 {field.field_en} 的校验引用自动建立",
            source_type="DOCUMENT_REFERENCE",
            status="DRAFT",
            current_version=1,
        )
        session.add(code_set)
        session.flush()
    existing = session.query(FieldCodeReferenceModel).filter(
        FieldCodeReferenceModel.field_id == field.id,
        FieldCodeReferenceModel.code_set_id == code_set.id,
    ).one_or_none()
    if not existing:
        session.add(
            FieldCodeReferenceModel(
                id=str(uuid.uuid4()),
                field_id=field.id,
                code_set_id=code_set.id,
                reference_text=text,
                status="UNRESOLVED",
                condition_json={},
                confidence=field.confidence,
            )
        )
    refresh_reference_status(session, code_set.id)


def ensure_table_references(session: Session, table: TableMetadataModel) -> None:
    fields = session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == table.id).all()
    for field in fields:
        ensure_field_reference(session, field)


def generation_blockers(session: Session, table: TableMetadataModel) -> List[Dict[str, Any]]:
    blockers = []
    fields = session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == table.id).all()
    for field in fields:
        references = session.query(FieldCodeReferenceModel).filter(FieldCodeReferenceModel.field_id == field.id).all()
        text = field.code_reference_text.strip()
        if text and text.upper() not in {"无", "N/A", "NONE", "无引用"} and not references:
            blockers.append({"field_en": field.field_en, "reason": "CODE_REFERENCE_NOT_BOUND"})
        for reference in references:
            if reference.status == "UNRESOLVED" and field.required:
                blockers.append({"field_en": field.field_en, "reason": "REQUIRED_CODE_SET_UNRESOLVED", "code_set_id": reference.code_set_id})
    return blockers
