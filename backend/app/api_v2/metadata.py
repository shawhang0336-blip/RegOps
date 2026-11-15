import csv
import io
import uuid
from io import BytesIO
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile
from openpyxl import load_workbook
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
from ..identity import OPERATOR_PLACEHOLDER
from ..metadata_domain import MetadataFieldDraft
from ..readiness import evaluate_document, evaluate_table
from ..metadata_service import (
    apply_field_type_import,
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
        # 按文件中的出现顺序（来源块 ordinal）排，符合阅读习惯；没有来源的排最后
        rows = query.order_by(TableMetadataModel.source_ordinal.asc(), TableMetadataModel.table_code.asc()).all()
        # 同一张表重新提取会生成新的版本行（旧行保留用于追溯），列表里只留最新版本
        latest: Dict[tuple, Any] = {}
        for row in rows:
            key = (row.document_id, row.table_code)
            current = latest.get(key)
            if current is None or (row.current_version or 0) >= (current.current_version or 0):
                latest[key] = row
        live_ids = {row.id for row in latest.values()}
        rows = [row for row in rows if row.id in live_ids]
        # 表级状态是"任意一条告警就整表待复核"，列表里必须把问题落到条数和字段上，
        # 否则用户只看到一个红色的"需要核对"，无法判断到底有几处、要不要动手。
        stats: Dict[str, Dict[str, Any]] = {}
        # 每次重新提取都会追加一轮校验记录，历史轮次的问题不能继续算在"待处理"里，
        # 否则修好之后列表仍然显示旧问题，用户看到的永远是过时状态。
        latest_by_kind: Dict[tuple, Any] = {}
        for validation in session.query(MetadataValidationModel).order_by(MetadataValidationModel.created_at).all():
            latest_by_kind[(validation.table_id, validation.validator_kind)] = validation
        for (table_id, _kind), validation in latest_by_kind.items():
            entry = stats.setdefault(
                table_id,
                {"issue_count": 0, "blocking": 0, "advisory": 0, "fields": set(), "codes": {}},
            )
            for issue in validation.issues_json or []:
                severity = str(issue.get("severity") or "WARNING").upper()
                if severity == "INFO":
                    continue  # 已修正/说明类条目不再计入待处理
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
        for field in session.query(FieldMetadataModel).all():
            entry = basis_stats.setdefault(field.table_id, {"source": 0, "inferred": 0, "manual": 0})
            basis = (field.type_basis or "UNKNOWN").upper()
            if basis == "SOURCE":
                entry["source"] += 1
            elif basis == "INFERRED":
                entry["inferred"] += 1
            elif basis == "MANUAL":
                entry["manual"] += 1
        result = []
        for row in rows:
            field_count = session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == row.id).count()
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
                    # 整表没有一个字段在原文里有类型声明，说明类型全靠推断，需要单独提示
                    "type_all_inferred": field_count > 0 and basis["source"] == 0 and basis["manual"] == 0,
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


@router.get("/types/template")
def download_type_template(document_id: Optional[str] = None) -> Response:
    """导出类型修正模板（CSV）：把当前字段和类型一起给出来，用户只改要改的那几列。"""
    lines = ["报表代码,字段英文名,字段中文名,当前类型,长度,小数位,目标类型,目标长度,目标小数位,说明"]
    with SessionLocal() as session:
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
) -> Dict[str, Any]:
    """批量修正字段类型。一行一个字段，只写要改的列；导入的行标成 MANUAL。"""
    rows = _read_import_rows(file.filename or "", await file.read())
    with SessionLocal() as session:
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


class PublishBatchRequest(BaseModel):
    document_id: Optional[str] = None
    table_ids: Optional[List[str]] = None
    # all_ready = 三项都就绪才发；ddl_only = 只要 DDL 能落地就发（规则/码值问题写进快照）
    scope: str = "all_ready"
    dry_run: bool = False
    approver: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)


@router.get("/readiness")
def metadata_readiness(document_id: Optional[str] = None) -> Dict[str, Any]:
    """每张表能出哪些产物：DDL / 规则 / 码值，以及卡在哪一项。"""
    with SessionLocal() as session:
        return evaluate_document(session, document_id)


@router.post("/tables/publish-batch")
def publish_batch(request: PublishBatchRequest) -> Dict[str, Any]:
    """批量发布：150 张表逐张点不现实，按就绪度一次推进。"""
    if request.scope not in {"all_ready", "ddl_only"}:
        raise HTTPException(status_code=422, detail="scope 只支持 all_ready / ddl_only")
    published: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    with SessionLocal() as session:
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
            # publish_table 会顺手把检查通过的码表一起发掉，所以「码表待发布」
            # 不该拦发布，只有真缺陷（空码表、引用解析不到）才拦
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
def published_spec(table_id: str) -> Dict[str, Any]:
    with SessionLocal() as session:
        table = session.get(TableMetadataModel, table_id)
        if not table:
            raise HTTPException(status_code=404, detail="Metadata table not found")
        try:
            return metadata_to_spec(session, table).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
