import csv
import io
import uuid
from io import BytesIO
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from openpyxl import load_workbook
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import db_session
from app.assets.code_sets.service import extract_parallel_for_blocks
from app.assets.metadata_domain import MetadataFieldDraft
from app.assets.metadata_service import (
    apply_field_type_import,
    metadata_to_spec,
    publish_table,
    remove_table,
    table_detail,
    table_summary,
    update_field,
)
from app.assets.readiness import evaluate_document, evaluate_table, latest_table_rows
from app.infra.models import (
    FieldMetadataModel,
    MetadataRevisionModel,
    MetadataValidationModel,
    RawBlockModel,
    SourceDocumentModel,
    TableMetadataModel,
)
from app.infra.config import get_settings
from app.infra.config import OPERATOR_PLACEHOLDER
from app.ingestion.structure_service import analyze_document_structure

router = APIRouter(prefix="/metadata", tags=["metadata"])


class ExtractRequest(BaseModel):
    block_ids: List[str]
    targets: Optional[List[str]] = None


class EditRequest(BaseModel):
    expected_version: int
    editor: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)
    reason: str = ""
    changes: Dict[str, Any]


class FieldCreateRequest(BaseModel):
    expected_version: int
    editor: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)
    reason: str = ""
    field: Dict[str, Any]


class PublishRequest(BaseModel):
    expected_version: int
    approver: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)


class DeleteRequest(BaseModel):
    expected_version: Optional[int] = None
    editor: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)
    reason: str = ""


class PublishBatchRequest(BaseModel):
    document_id: Optional[str] = None
    table_ids: Optional[List[str]] = None
    # all_ready = 三项都就绪才发；ddl_only = 只要 DDL 能落地就发（规则/码值问题写进快照）
    scope: str = "all_ready"
    dry_run: bool = False
    approver: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)


@router.post("/extract", status_code=202)
def extract_metadata(request: ExtractRequest, session: Session = Depends(db_session)) -> Dict[str, Any]:
    if not request.block_ids:
        raise HTTPException(status_code=422, detail="Select at least one raw block")
    settings = get_settings()
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
def list_tables(
    status: Optional[str] = None,
    document_id: Optional[str] = None,
    session: Session = Depends(db_session),
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


@router.get("/tables/{table_id}")
def get_table(table_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    table = session.get(TableMetadataModel, table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Metadata table not found")
    return table_detail(session, table)


@router.patch("/tables/{table_id}/fields/{field_id}")
def edit_field(
    table_id: str,
    field_id: str,
    request: EditRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
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


@router.get("/types/template")
def download_type_template(
    document_id: Optional[str] = None,
    session: Session = Depends(db_session),
) -> Response:
    """导出类型修正模板（CSV）：把当前字段和类型一起给出来，用户只改要改的那几列。"""
    lines = ["报表代码,字段英文名,字段中文名,当前类型,长度,小数位,目标类型,目标长度,目标小数位,说明"]
    query = (
        session.query(FieldMetadataModel, TableMetadataModel)
        .join(TableMetadataModel, FieldMetadataModel.table_id == TableMetadataModel.id)
    )
    if document_id:
        query = query.filter(TableMetadataModel.document_id == document_id)
    for field, table in query.order_by(TableMetadataModel.source_ordinal, FieldMetadataModel.sort_order).all():
        lines.append(
            ",".join(
                [
                    table.table_code,
                    field.field_en or "",
                    (field.field_cn or "").replace(",", "，"),
                    field.data_type or "",
                    "" if field.length is None else str(field.length),
                    "" if field.decimal_places is None else str(field.decimal_places),
                    "",
                    "",
                    "",
                    "",
                ]
            )
        )
    body = "\ufeff" + "\n".join(lines) + "\n"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="field_type_template.csv"'},
    )


def _read_import_rows(filename: str, content: bytes) -> List[List[str]]:
    lowered = (filename or "").lower()
    if lowered.endswith((".xlsx", ".xlsm")):
        workbook = load_workbook(BytesIO(content), data_only=True)
        sheet = workbook.active
        return [[("" if cell is None else str(cell).strip()) for cell in row] for row in sheet.iter_rows(values_only=True)]
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


@router.post("/types/import")
async def import_field_types(
    file: UploadFile = File(...),
    editor: str = Form(default=OPERATOR_PLACEHOLDER),
    reason: str = Form(default="批量导入类型修正"),
    document_id: Optional[str] = Form(default=None),
    dry_run: bool = Form(default=False),
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    """批量修正字段类型。一行一个字段，只写要改的列；导入的行标成 MANUAL。"""
    rows = _read_import_rows(file.filename or "", await file.read())
    try:
        return apply_field_type_import(
            session,
            rows,
            editor=editor,
            reason=reason,
            document_id=document_id or None,
            dry_run=dry_run,
        )
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/tables/{table_id}/fields")
def add_field(
    table_id: str,
    request: FieldCreateRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    table = session.get(TableMetadataModel, table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Metadata table not found")
    if table.current_version != request.expected_version:
        raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
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
def delete_field(
    table_id: str,
    field_id: str,
    request: EditRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    table = session.get(TableMetadataModel, table_id)
    field = session.get(FieldMetadataModel, field_id)
    if not table or not field or field.table_id != table_id:
        raise HTTPException(status_code=404, detail="Field not found")
    if table.current_version != request.expected_version:
        raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
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


@router.delete("/tables/{table_id}")
def delete_table(
    table_id: str,
    request: DeleteRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    """删掉一张不想要的报表（硬删除，历史 run 快照不受影响）。"""
    table = session.get(TableMetadataModel, table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Metadata table not found")
    if request.expected_version is not None and table.current_version != request.expected_version:
        raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
    result = remove_table(session, table)
    session.commit()
    return {**result, "editor": request.editor, "reason": request.reason}


@router.post("/tables/{table_id}/draft")
def create_draft(
    table_id: str,
    request: PublishRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
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
def publish(
    table_id: str,
    request: PublishRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
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


@router.get("/readiness")
def metadata_readiness(
    document_id: Optional[str] = None,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    """每张表能出哪些产物：DDL / 规则 / 码值，以及卡在哪一项。"""
    return evaluate_document(session, document_id)


@router.post("/tables/publish-batch")
def publish_batch(request: PublishBatchRequest, session: Session = Depends(db_session)) -> Dict[str, Any]:
    """批量发布：按就绪度一次推进。"""
    if request.scope not in {"all_ready", "ddl_only"}:
        raise HTTPException(status_code=422, detail="scope 只支持 all_ready / ddl_only")
    published: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    query = session.query(TableMetadataModel)
    if request.table_ids:
        query = query.filter(TableMetadataModel.id.in_(request.table_ids))
    elif request.document_id:
        query = query.filter(TableMetadataModel.document_id == request.document_id)
    tables = query.order_by(TableMetadataModel.source_ordinal.asc()).all()
    for table in tables:
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
        if request.scope == "all_ready" and pending:
            skipped.append({"table_code": table.table_code, "reason": "RULE_OR_CODE_NOT_READY", "detail": "、".join(pending[:3])})
            continue
        if request.dry_run:
            published.append({"table_code": table.table_code, "warnings": pending})
            continue
        try:
            publish_table(session, table, request.approver, table.current_version)
            published.append({"table_code": table.table_code, "warnings": pending})
        except ValueError as exc:
            skipped.append({"table_code": table.table_code, "reason": "PUBLISH_FAILED", "detail": str(exc)})
    if request.dry_run:
        session.rollback()
    else:
        session.commit()
    return {
        "published": len(published),
        "skipped": len(skipped),
        "details": published,
        "skipped_details": skipped[:50],
        "dry_run": request.dry_run,
    }


@router.get("/tables/{table_id}/spec")
def published_spec(table_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    table = session.get(TableMetadataModel, table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Metadata table not found")
    try:
        return metadata_to_spec(session, table).model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
