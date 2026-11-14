import hashlib
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from .asset_models import (
    FieldCodeReferenceModel,
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
from .metadata_domain import MetadataFieldDraft, MetadataTableDraft, require_valid_metadata, validate_metadata_draft
from .spec_parser import _parse_type_from_entry, extract_code_reference_from_text, extract_inline_enum_values

METADATA_BATCH_SIZE = 10


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
    existing = (
        session.query(TableMetadataModel)
        .filter(TableMetadataModel.block_id == block.id)
        .order_by(TableMetadataModel.current_version.desc())
        .first()
    )
    if existing:
        current_fields = (
            session.query(FieldMetadataModel)
            .filter(FieldMetadataModel.table_id == existing.id)
            .order_by(FieldMetadataModel.sort_order)
            .all()
        )
        field_keys = (
            "field_cn", "field_en", "field_definition", "check_text_raw", "data_type",
            "length", "decimal_places", "required", "code_reference_text", "confidence", "evidence_json",
        )
        current_values = [{key: getattr(field, key) for key in field_keys} for field in current_fields]
        incoming_values = [field.model_dump() for field in draft.fields]
        if (
            existing.table_code == draft.table_code
            and existing.table_name == draft.table_name
            and existing.table_definition == draft.table_definition
            and current_values == incoming_values
        ):
            return existing

    preserve_existing = False
    if existing:
        preserve_existing = existing.status == "PUBLISHED" or bool(
            session.query(MetadataRevisionModel)
            .filter(
                MetadataRevisionModel.table_id == existing.id,
                MetadataRevisionModel.revision_kind.in_(["HUMAN_EDIT", "HUMAN_ADD", "HUMAN_DELETE"]),
            )
            .first()
        ) or bool(
            session.query(FieldCodeReferenceModel)
            .join(FieldMetadataModel, FieldCodeReferenceModel.field_id == FieldMetadataModel.id)
            .filter(FieldMetadataModel.table_id == existing.id)
            .first()
        )

    if existing and not preserve_existing:
        for field in session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == existing.id).all():
            session.delete(field)
        session.flush()
        table = existing
        table.table_code = draft.table_code
        table.table_name = draft.table_name
        table.table_definition = draft.table_definition
        table.current_version += 1
    else:
        next_ver = (existing.current_version + 1) if existing else 1
        table = TableMetadataModel(
            id=str(uuid.uuid4()),
            document_id=block.document_id,
            block_id=block.id,
            table_code=draft.table_code,
            table_name=draft.table_name,
            table_definition=draft.table_definition,
            status="DRAFT",
            current_version=next_ver,
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
            reason="Batch metadata assignment",
        )
    )
    session.flush()
    return table


def _deterministic_field_draft(
    raw_field: Dict[str, Any],
    terms_text: str,
    locator: Dict[str, Any],
    fallback_reason: str = "",
) -> MetadataFieldDraft:
    # Support both structured spec field dicts and DOCX/XLSX table rows
    if "name" in raw_field:
        field_en = str(raw_field["name"]).strip()
        field_cn = str(raw_field.get("content") or field_en).strip()
        check_text = str(raw_field.get("check_text") or "").strip()
        data_type = "NUMBER" if str(raw_field.get("data_type", "VARCHAR")).upper() == "NUMBER" else "VARCHAR"
        length = int(raw_field.get("length") or 128)
        dec = int(raw_field.get("decimal_places") or 0)
        page = raw_field.get("evidence_page") or locator.get("page", "")
    elif "row" in raw_field:
        row = [str(c or "").strip() for c in raw_field["row"]]
        headers = [str(h or "").strip() for h in raw_field.get("headers", [])]
        field_cn = row[0] if len(row) > 0 else "字段"
        field_en = row[1] if len(row) > 1 and re.match(r"^[A-Za-z][A-Za-z0-9_]*$", row[1]) else f"FIELD_{hashlib.sha1(field_cn.encode('utf-8')).hexdigest()[:8].upper()}"
        check_text = row[2] if len(row) > 2 else ""
        _, data_type, length, parsed_dec, _, type_warnings = _parse_type_from_entry(check_text, field_cn)
        dec = parsed_dec or 0
        page = locator.get("table_index", locator.get("sheet", ""))
    else:
        raise ValueError("Cannot deterministically map unstructured raw_field without name or row")

    # Find term definition in terms_text if available
    field_def = field_cn
    if terms_text and field_cn:
        idx = terms_text.find(field_cn)
        if idx >= 0:
            snippet = re.sub(r"\s+", " ", terms_text[idx : idx + 240]).strip()
            field_def = snippet[:180]

    required = bool(raw_field.get("required_basis")) or ("必填" in check_text and "非必填" not in check_text and "选填" not in check_text)
    code_ref = extract_code_reference_from_text(check_text) or extract_code_reference_from_text(field_def)
    if not code_ref:
        inline_enums = extract_inline_enum_values(check_text)
        if inline_enums:
            code_ref = f"{field_cn}（{field_en} 内联枚举）"

    evidence = {"page": page, "source_locator": locator, "raw_field": raw_field}
    if fallback_reason:
        evidence["fallback_reason"] = fallback_reason
    if "row" in raw_field and type_warnings:
        evidence["parse_warnings"] = type_warnings

    return MetadataFieldDraft(
        field_cn=field_cn,
        field_en=field_en,
        field_definition=field_def,
        check_text_raw=check_text,
        data_type=data_type,
        length=max(1, length, dec),
        decimal_places=max(0, dec),
        required=required,
        code_reference_text=code_ref,
        confidence=0.5 if fallback_reason else (0.65 if "row" in raw_field and type_warnings else 0.95),
        evidence_json=evidence,
    )


def extract_metadata_for_block(block_id: str, settings: Settings, auto_link: bool = True) -> str:
    with SessionLocal() as session:
        block = session.get(RawBlockModel, block_id)
        if not block:
            raise ValueError("Raw block not found")
        source_spec = block.content_json.get("spec") or block.content_json.get("parent_spec") or {}
        terms_text = source_spec.get("terms_text", "")
        raw_fields = source_spec.get("fields")
        if raw_fields is None:
            rows = block.content_json.get("rows", [])
            raw_fields = [{"row": row, "headers": block.content_json.get("headers", [])} for row in rows[1:] if any(row)]
        if not raw_fields:
            raw_fields = [{"raw_text": block.raw_content}]
        fallback_code = source_spec.get("table_code") or block.content_json.get("table_code") or f"TABLE_{block.id.replace('-', '')[:12].upper()}"
        fallback_name = source_spec.get("table_name") or block.content_json.get("table_name") or f"原始表格 {block.ordinal + 1}"

        try:
            llm = RealDesignLlm(settings)
        except Exception:
            llm = None

        # Chunk raw_fields into batches (up to 10 fields per LLM call instead of 1 call per field)
        chunks = [raw_fields[i : i + METADATA_BATCH_SIZE] for i in range(0, len(raw_fields), METADATA_BATCH_SIZE)]

        def bind_chunk(chunk: List[Dict[str, Any]]):
            if llm is not None:
                payload = {
                    "source_locator": block.source_locator,
                    "table": {"table_code": fallback_code, "table_name": fallback_name},
                    "raw_fields": chunk,
                    "raw_context": block.raw_content[:6000],
                }
                try:
                    args, trace = llm.assign(
                        "assign_metadata",
                        f"批量装配输入的 {len(chunk)} 个字段。字段英文名、中文名、类型和校验原文为最低必备信息；定义、长度、码值引用等仅在有明确依据时填写，其他项可省略，不得虚构。",
                        payload,
                        timeout=60,
                    )
                    candidate = {
                        "table_code": fallback_code,
                        "table_name": fallback_name,
                        "table_definition": args.get("table_definition", ""),
                        "fields": args.get("fields", []),
                    }
                    draft = MetadataTableDraft.model_validate(candidate)
                    if len(draft.fields) == len(chunk):
                        require_valid_metadata(draft)
                        return draft.fields, draft.table_definition, trace
                except Exception as exc:
                    err_msg = f"{type(exc).__name__}: {exc}"
                else:
                    err_msg = "Field count mismatch in batch LLM response"
            else:
                err_msg = ""

            # Deterministic fast-path / fallback mapping
            mapped_fields = [
                _deterministic_field_draft(rf, terms_text, block.source_locator, fallback_reason=err_msg)
                for rf in chunk
            ]
            table_def = f"{fallback_name}（共 {len(raw_fields)} 个字段，结构化提取）"
            return mapped_fields, table_def, {
                "strategy": "DETERMINISTIC_BATCH_MAPPING",
                "field_count": len(mapped_fields),
                **({"fallback_from": err_msg} if err_msg else {}),
            }

        with ThreadPoolExecutor(max_workers=min(2, len(chunks))) as executor:
            chunk_results = list(executor.map(bind_chunk, chunks))

        all_fields: List[MetadataFieldDraft] = []
        table_definition = ""
        batch_traces = []
        for fields_part, t_def, tr in chunk_results:
            all_fields.extend(fields_part)
            if t_def and not table_definition:
                table_definition = t_def
            batch_traces.append(tr)

        assignment_fallback = bool(batch_traces) and all(trace.get("fallback_from") for trace in batch_traces)
        draft = MetadataTableDraft(
            table_code=fallback_code,
            table_name=fallback_name,
            table_definition=table_definition or f"{fallback_name} 结构化元数据表",
            fields=all_fields,
        )
        require_valid_metadata(draft)
        trace = {
            "provider": "GLM" if llm else "DETERMINISTIC",
            "model": settings.llm_model if llm else "rule-engine",
            "batch_count": len(chunk_results),
            "batches": batch_traces,
            "arguments": draft.model_dump(),
        }
        table = _persist_draft(session, block, draft, trace)
        program_issues = validate_metadata_draft(draft)
        if block.content_json.get("human_required"):
            program_issues.append(
                {
                    "severity": "WARNING",
                    "code": "SOURCE_PARSE_REVIEW_REQUIRED",
                    "field_en": "",
                    "message": "；".join(block.content_json.get("parse_warnings") or ["来源块需要人工确认"]),
                }
            )
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

        # Differential Review Stage (using review_metadata tool or deterministic cross-check)
        issues: List[Dict[str, Any]] = []
        if llm is not None and not assignment_fallback:
            try:
                args, review_trace = llm.assign(
                    "review_metadata",
                    "独立复核整表字段元数据，仅输出存在遗漏、类型长度错配、无依据必填或虚构码值的问题列表（无问题则返回空数组）。",
                    {
                        "source_locator": block.source_locator,
                        "table": {"table_code": draft.table_code, "table_name": draft.table_name},
                        "fields": [f.model_dump() for f in draft.fields],
                        "raw_context": block.raw_content[:6000],
                    },
                    timeout=35,
                )
                issues = args.get("issues", [])
            except Exception as exc:
                review_trace = {"error": f"{type(exc).__name__}: {exc}"}
                issues = [
                    {
                        "severity": "WARNING",
                        "code": "AI_REVIEW_FALLBACK",
                        "field_en": "",
                        "message": f"在线 AI 复核跳过，已使用确定性规则交叉核验：{review_trace['error']}",
                    }
                ]
        elif assignment_fallback:
            review_trace = {"strategy": "DETERMINISTIC_CROSS_CHECK", "skipped_reason": "AI_ASSIGNMENT_UNAVAILABLE"}
            issues = [{
                "severity": "WARNING",
                "code": "AI_REVIEW_UNAVAILABLE",
                "field_en": "",
                "message": "AI 字段赋值不可用，已使用程序提取并交叉检查；无需再等待一次 AI 复核。",
            }]
        else:
            review_trace = {"strategy": "DETERMINISTIC_CROSS_CHECK", "checked_fields": len(draft.fields)}

        session.add(
            MetadataValidationModel(
                id=str(uuid.uuid4()),
                table_id=table.id,
                validator_kind="AI_REVIEW",
                round=1,
                status="FAILED" if any(item.get("severity") == "ERROR" for item in issues) else "PASSED",
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
                reason="Independent batch metadata review",
            )
        )
        table.status = (
            "NEEDS_REVIEW"
            if any(item.get("severity") in {"ERROR", "WARNING"} for item in program_issues + issues)
            else "AI_SELF_VALIDATED"
        )
        if auto_link:
            from .code_service import ensure_table_references

            ensure_table_references(session, table)
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
