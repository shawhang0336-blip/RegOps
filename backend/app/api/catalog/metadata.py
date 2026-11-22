"""报表与字段元数据管理路由（`/api/metadata/*`）。"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.catalog import metadata_service as svc
from app.api.catalog.metadata_service import read_import_rows as _read_import_rows
from app.api.common import db_session
from app.catalog.tables.readiness import evaluate_document
from app.catalog.tables.service import (
    apply_field_type_import,
    metadata_to_spec,
    publish_table,
    remove_table,
    table_detail,
    update_field,
)
from app.infra.config import OPERATOR_PLACEHOLDER, get_settings
from app.infra.db_tables import FieldMetadataModel, TableMetadataModel

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
    return svc.extract_metadata_for_blocks(session, request.block_ids, request.targets, get_settings())


@router.get("/tables")
def list_tables(
    status: Optional[str] = None,
    document_id: Optional[str] = None,
    session: Session = Depends(db_session),
) -> List[Dict[str, Any]]:
    return svc.list_tables_with_stats(session, status=status, document_id=document_id)


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
    """导出类型修正模板（CSV）：仅导出当前有效最新版本表（排除 MERGED），严格 RFC 4180 转义。"""
    body = svc.build_type_template_csv(session, document_id=document_id)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="field_type_template.csv"'},
    )


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
    return svc.create_table_field(
        session,
        table_id,
        expected_version=request.expected_version,
        field_payload=request.field,
        editor=request.editor,
        reason=request.reason,
    )


@router.delete("/tables/{table_id}/fields/{field_id}")
def delete_field(
    table_id: str,
    field_id: str,
    request: EditRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    return svc.delete_table_field(
        session,
        table_id,
        field_id,
        expected_version=request.expected_version,
        editor=request.editor,
        reason=request.reason,
    )


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
    return svc.clone_table_draft(
        session,
        table_id,
        expected_version=request.expected_version,
        approver=request.approver,
    )


@router.post("/tables/{table_id}/publish")
def publish(
    table_id: str,
    request: PublishRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    table = session.get(TableMetadataModel, table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Metadata table not found")
    if table.status == "MERGED":
        raise HTTPException(status_code=409, detail="MERGED table cannot be published")
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
    """批量发布：按就绪度一次推进（自动排除 MERGED 碎片表与历史旧版本）。"""
    return svc.publish_tables_batch(
        session,
        document_id=request.document_id,
        table_ids=request.table_ids,
        scope=request.scope,
        dry_run=request.dry_run,
        approver=request.approver,
    )


@router.get("/tables/{table_id}/spec")
def published_spec(table_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    table = session.get(TableMetadataModel, table_id)
    if not table:
        raise HTTPException(status_code=404, detail="Metadata table not found")
    try:
        return metadata_to_spec(session, table).model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


__all__ = [
    "DeleteRequest",
    "EditRequest",
    "ExtractRequest",
    "FieldCreateRequest",
    "PublishBatchRequest",
    "PublishRequest",
    "_read_import_rows",
    "router",
]
