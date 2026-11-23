"""报表与字段元数据查询、批量类型导入、草稿克隆与批量发布应用服务。"""

import csv
import io
import uuid
from io import BytesIO
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from openpyxl import load_workbook
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog.extraction import extract_parallel_for_blocks
from app.catalog.tables.domain import MetadataFieldDraft
from app.catalog.tables.mutations import claim_table_version_bump
from app.catalog.tables.readiness import evaluate_table, latest_table_rows
from app.catalog.tables.service import publish_table, table_detail, table_summary
from app.infra.db_tables import (
    FieldCodeReferenceModel,
    FieldMetadataModel,
    MetadataRevisionModel,
    MetadataValidationModel,
    RawBlockModel,
    SourceDocumentModel,
    TableMetadataModel,
)
from app.ingestion.structure_service import analyze_document_structure

MAX_IMPORT_BYTES = 30 * 1024 * 1024


def extract_metadata_for_blocks(
    session: Session,
    block_ids: List[str],
    targets: Optional[List[str]],
    settings: Any,
) -> Dict[str, Any]:
    if not block_ids:
        raise HTTPException(status_code=422, detail="Select at least one raw block")
    blocks = session.query(RawBlockModel).filter(RawBlockModel.id.in_(block_ids)).all()
    if any(block.content_json.get("superseded") for block in blocks):
        raise HTTPException(status_code=409, detail="Superseded raw blocks cannot be extracted")
    doc_ids_to_analyze = set()
    for block in blocks:
        document = session.get(SourceDocumentModel, block.document_id)
        if document and document.status == "UPLOADED":
            doc_ids_to_analyze.add(document.id)
    for doc_id in doc_ids_to_analyze:
        analyze_document_structure(doc_id, settings)

    result = extract_parallel_for_blocks(block_ids, settings, targets or ["metadata", "code_sets"])
    return {
        "table_ids": result["table_ids"],
        "code_set_ids": result["code_set_ids"],
        "resolved_reference_count": result["resolved_reference_count"],
        "errors": result["errors"],
        "status": result["status"],
    }


def list_tables_with_stats(
    session: Session,
    *,
    status: Optional[str] = None,
    document_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    rows = latest_table_rows(session, document_id=document_id, status=status)
    table_ids = [row.id for row in rows]
    stats: Dict[str, Dict[str, Any]] = {}
    latest_by_kind: Dict[tuple, Any] = {}
    validations = (
        session.query(MetadataValidationModel)
        .filter(MetadataValidationModel.table_id.in_(table_ids))
        .order_by(MetadataValidationModel.created_at)
        .all()
        if table_ids
        else []
    )
    for validation in validations:
        latest_by_kind[(validation.table_id, validation.validator_kind)] = validation
    for (table_id, _kind), validation in latest_by_kind.items():
        entry = stats.setdefault(
            table_id,
            {"issue_count": 0, "blocking": 0, "advisory": 0, "fields": set(), "codes": {}},
        )
        for issue in validation.issues_json or []:
            severity = str(issue.get("severity") or "WARNING").upper()
            if severity == "INFO":
                continue
            entry["issue_count"] += 1
            if severity == "ERROR":
                entry["blocking"] += 1
            else:
                entry["advisory"] += 1
            code = str(issue.get("code") or "").strip()
            if code:
                entry["codes"][code] = entry["codes"].get(code, 0) + 1
            if severity == "ERROR":
                field_en = str(issue.get("field_en") or "").strip()
                if field_en:
                    entry["fields"].add(field_en)
    basis_stats: Dict[str, Dict[str, int]] = {}
    field_counts: Dict[str, int] = {}
    field_rows = (
        session.query(FieldMetadataModel.table_id, FieldMetadataModel.type_basis)
        .filter(FieldMetadataModel.table_id.in_(table_ids))
        .all()
        if table_ids
        else []
    )
    for table_id, type_basis in field_rows:
        field_counts[table_id] = field_counts.get(table_id, 0) + 1
        entry = basis_stats.setdefault(table_id, {"source": 0, "inferred": 0, "manual": 0})
        basis = (type_basis or "UNKNOWN").upper()
        if basis == "SOURCE":
            entry["source"] += 1
        elif basis == "INFERRED":
            entry["inferred"] += 1
        elif basis == "MANUAL":
            entry["manual"] += 1
    result = []
    for row in rows:
        field_count = field_counts.get(row.id, 0)
        basis = basis_stats.get(row.id, {"source": 0, "inferred": 0, "manual": 0})
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
                "review_blocking_count": entry.get("blocking", 0),
                "review_advisory_count": entry.get("advisory", 0),
                "review_field_count": len(entry.get("fields") or set()),
                "review_top_codes": [code for code, _ in top_codes],
                "type_source_count": basis["source"],
                "type_inferred_count": basis["inferred"],
                "type_manual_count": basis["manual"],
                "type_all_inferred": field_count > 0 and basis["source"] == 0 and basis["manual"] == 0,
            }
        )
    return result


def build_type_template_csv(session: Session, *, document_id: Optional[str] = None) -> str:
    """导出类型修正模板（CSV）：仅导出当前有效最新版本表（排除 MERGED），严格 RFC 4180 转义。"""
    tables = latest_table_rows(session, document_id=document_id)
    table_ids = [table.id for table in tables]
    fields_by_table: Dict[str, List[FieldMetadataModel]] = {}
    if table_ids:
        for field in (
            session.query(FieldMetadataModel)
            .filter(FieldMetadataModel.table_id.in_(table_ids))
            .order_by(FieldMetadataModel.sort_order)
            .all()
        ):
            fields_by_table.setdefault(field.table_id, []).append(field)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["报表代码", "字段英文名", "字段中文名", "当前类型", "长度", "小数位", "目标类型", "目标长度", "目标小数位", "说明"])
    for table in tables:
        for field in fields_by_table.get(table.id, []):
            writer.writerow(
                [
                    table.table_code,
                    field.field_en or "",
                    field.field_cn or "",
                    field.data_type or "",
                    "" if field.length is None else str(field.length),
                    "" if field.decimal_places is None else str(field.decimal_places),
                    "",
                    "",
                    "",
                    "",
                ]
            )
    return "\ufeff" + buf.getvalue()


def read_import_rows(filename: str, content: bytes) -> List[List[str]]:
    if len(content) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds the 30 MB limit")
    lowered = (filename or "").lower()
    if lowered.endswith((".xlsx", ".xlsm")):
        try:
            workbook = load_workbook(BytesIO(content), data_only=True)
            sheet = workbook.active
            if sheet is None:
                return []
            return [[("" if cell is None else str(cell).strip()) for cell in row] for row in sheet.iter_rows(values_only=True)]
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"无法解析 Excel 文件：{type(exc).__name__}") from exc
    text = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise HTTPException(status_code=422, detail="无法识别文件编码，请另存为 UTF-8 或 xlsx")
    return [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text))]


def create_table_field(
    session: Session,
    table_id: str,
    *,
    expected_version: int,
    field_payload: Dict[str, Any],
    editor: str,
    reason: str,
) -> Dict[str, Any]:
    table = session.get(TableMetadataModel, table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Metadata table not found")
    if table.status == "MERGED":
        raise HTTPException(status_code=409, detail="MERGED table cannot be edited")
    if table.current_version != expected_version:
        raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
    try:
        draft = MetadataFieldDraft.model_validate(field_payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    existing_ens = {
        (name or "").strip().upper()
        for (name,) in session.query(FieldMetadataModel.field_en)
        .filter(FieldMetadataModel.table_id == table.id)
        .all()
    }
    if draft.field_en.strip().upper() in existing_ens:
        raise HTTPException(status_code=409, detail=f"字段 {draft.field_en} 已存在")
    max_order = (
        session.query(func.max(FieldMetadataModel.sort_order))
        .filter(FieldMetadataModel.table_id == table.id)
        .scalar()
    )
    sort_order = (max_order + 1) if max_order is not None else 0
    field = FieldMetadataModel(
        id=str(uuid.uuid4()),
        table_id=table.id,
        sort_order=sort_order,
        type_basis="MANUAL",
        ai_value_json=draft.model_dump(),
        **draft.model_dump(),
    )
    try:
        claim_table_version_bump(session, table, expected_version, new_status="DRAFT")
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.add(field)
    session.add(
        MetadataRevisionModel(
            id=str(uuid.uuid4()),
            table_id=table.id,
            field_id=field.id,
            revision_kind="HUMAN_ADD",
            old_value_json=None,
            new_value_json=draft.model_dump(),
            editor=editor,
            reason=reason,
        )
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=f"字段 {draft.field_en} 已存在") from exc
    return table_detail(session, table)


def delete_table_field(
    session: Session,
    table_id: str,
    field_id: str,
    *,
    expected_version: int,
    editor: str,
    reason: str,
) -> Dict[str, Any]:
    table = session.get(TableMetadataModel, table_id)
    field = session.get(FieldMetadataModel, field_id)
    if not table or not field or field.table_id != table_id:
        raise HTTPException(status_code=404, detail="Field not found")
    if table.status == "MERGED":
        raise HTTPException(status_code=409, detail="MERGED table cannot be edited")
    if table.current_version != expected_version:
        raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
    try:
        claim_table_version_bump(session, table, expected_version, new_status="DRAFT")
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    old = {"field_id": field.id, "field_en": field.field_en, "field_cn": field.field_cn}
    # MySQL 8 强外键顺序：先清子表引用与历史修订的 field_id 外键，再删字段本身，最后落一条 field_id=None 的删除审计记录
    session.query(FieldCodeReferenceModel).filter(
        FieldCodeReferenceModel.field_id == field.id
    ).delete(synchronize_session=False)
    session.query(MetadataRevisionModel).filter(
        MetadataRevisionModel.field_id == field.id
    ).update({MetadataRevisionModel.field_id: None}, synchronize_session=False)
    session.flush()
    session.delete(field)
    session.flush()
    session.add(
        MetadataRevisionModel(
            id=str(uuid.uuid4()),
            table_id=table.id,
            field_id=None,
            revision_kind="HUMAN_DELETE",
            old_value_json=old,
            new_value_json={"deleted": True},
            editor=editor,
            reason=reason,
        )
    )
    session.commit()
    return table_detail(session, table)


def clone_table_draft(
    session: Session,
    table_id: str,
    *,
    expected_version: int,
    approver: str,
) -> Dict[str, Any]:
    source = session.get(TableMetadataModel, table_id)
    if not source:
        raise HTTPException(status_code=404, detail="Metadata table not found")
    if source.status == "MERGED":
        raise HTTPException(status_code=409, detail="MERGED table cannot create draft")
    if source.current_version != expected_version:
        raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
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
        source_ordinal=source.source_ordinal,
    )
    session.add(target)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="该版本草稿已存在，请刷新后重试") from exc
    fields = (
        session.query(FieldMetadataModel)
        .filter(FieldMetadataModel.table_id == source.id)
        .order_by(FieldMetadataModel.sort_order)
        .all()
    )
    field_id_map: Dict[str, str] = {}
    for field in fields:
        values = {
            key: getattr(field, key)
            for key in [
                "field_cn",
                "field_en",
                "field_definition",
                "check_text_raw",
                "type_basis",
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
        new_field_id = str(uuid.uuid4())
        field_id_map[field.id] = new_field_id
        session.add(FieldMetadataModel(id=new_field_id, table_id=target.id, **values))
    session.flush()
    if field_id_map:
        refs = (
            session.query(FieldCodeReferenceModel)
            .filter(FieldCodeReferenceModel.field_id.in_(list(field_id_map.keys())))
            .all()
        )
        for ref in refs:
            session.add(
                FieldCodeReferenceModel(
                    id=str(uuid.uuid4()),
                    field_id=field_id_map[ref.field_id],
                    code_set_id=ref.code_set_id,
                    reference_text=ref.reference_text,
                    status=ref.status,
                    condition_json=dict(ref.condition_json or {}),
                    confidence=ref.confidence,
                )
            )
    session.add(
        MetadataRevisionModel(
            id=str(uuid.uuid4()),
            table_id=target.id,
            field_id=None,
            revision_kind="DRAFT_CREATED",
            old_value_json={"source_table_id": source.id},
            new_value_json={"version": target.current_version},
            editor=approver,
            reason="Create editable draft from published version",
        )
    )
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="该版本草稿已存在，请刷新后重试") from exc
    return table_detail(session, target)


def publish_tables_batch(
    session: Session,
    *,
    document_id: Optional[str],
    table_ids: Optional[List[str]],
    scope: str,
    dry_run: bool,
    approver: str,
) -> Dict[str, Any]:
    if scope not in {"all_ready", "ddl_only"}:
        raise HTTPException(status_code=422, detail="scope 只支持 all_ready / ddl_only")
    published: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    if table_ids:
        tables = (
            session.query(TableMetadataModel)
            .filter(TableMetadataModel.id.in_(list(dict.fromkeys(table_ids))))
            .order_by(TableMetadataModel.source_ordinal.asc())
            .all()
        )
    else:
        tables = latest_table_rows(session, document_id=document_id)
    for table in tables:
        if table.status == "MERGED":
            skipped.append({"table_code": table.table_code, "reason": "MERGED_TABLE"})
            continue
        if table.status == "PUBLISHED" and table.published_version == table.current_version:
            skipped.append({"table_code": table.table_code, "reason": "ALREADY_PUBLISHED"})
            continue
        result = evaluate_table(session, table)
        if not result["ddl"]["ready"]:
            skipped.append(
                {
                    "table_code": table.table_code,
                    "reason": result["ddl"]["blockers"][0]["code"],
                    "detail": result["ddl"]["blockers"][0]["message"],
                }
            )
            continue
        pending = [
            item["code"]
            for item in result["rule_materials"]["blockers"] + result["code_sets"]["defects"]
        ]
        if scope == "all_ready" and pending:
            skipped.append({"table_code": table.table_code, "reason": "RULE_OR_CODE_NOT_READY", "detail": "、".join(pending[:3])})
            continue
        if dry_run:
            published.append({"table_code": table.table_code, "warnings": pending})
            continue
        try:
            publish_table(session, table, approver, table.current_version)
            published.append({"table_code": table.table_code, "warnings": pending})
        except ValueError as exc:
            skipped.append({"table_code": table.table_code, "reason": "PUBLISH_FAILED", "detail": str(exc)})
    if dry_run:
        session.rollback()
    else:
        session.commit()
    return {
        "published": len(published),
        "skipped": len(skipped),
        "details": published,
        "skipped_details": skipped[:50],
        "dry_run": dry_run,
    }
