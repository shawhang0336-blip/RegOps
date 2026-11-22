"""公共与内联码表资产管理路由（`/api/code-sets/*`）。"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.catalog import code_sets_service as svc
from app.api.common import db_session
from app.catalog.code_sets.readiness import evaluate_code_sets
from app.catalog.code_sets.service import remove_code_set
from app.infra.config import OPERATOR_PLACEHOLDER, get_settings
from app.infra.db_tables import CodeSetModel

router = APIRouter(prefix="/code-sets", tags=["code-sets"])


class CodeSetRequest(BaseModel):
    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    definition: str = ""
    source_type: str = "MANUAL"


class VersionRequest(BaseModel):
    expected_version: int
    editor: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)
    reason: str = ""
    changes: Dict[str, Any] = {}


class CodeValueRequest(BaseModel):
    expected_version: int
    editor: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)
    value: str = Field(min_length=1)
    label_cn: str = Field(min_length=1)
    label_en: str = ""
    definition: str = ""


class PublishBatchRequest(BaseModel):
    document_id: Optional[str] = None
    code_set_ids: Optional[List[str]] = None
    referenced_only: bool = True
    dry_run: bool = False


@router.get("")
def list_code_sets(
    document_id: Optional[str] = None,
    include_placeholders: bool = False,
    session: Session = Depends(db_session),
) -> List[Dict[str, Any]]:
    return svc.list_code_sets_view(session, document_id=document_id, include_placeholders=include_placeholders)


@router.post("", status_code=201)
def create_code_set(request: CodeSetRequest, session: Session = Depends(db_session)) -> Dict[str, Any]:
    return svc.create_code_set_record(
        session,
        code=request.code,
        name=request.name,
        definition=request.definition,
        source_type=request.source_type,
    )


@router.post("/upload-file", status_code=201)
async def upload_code_set_document(
    file: UploadFile = File(...),
    include_inline: bool = Form(default=True),
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    """在码表管理页直接上传文档并并行提取码值表与内联枚举项。"""
    return svc.upload_and_extract_code_sets(
        session,
        filename=file.filename or "",
        content=await file.read(),
        include_inline=include_inline,
        settings=get_settings(),
    )


@router.get("/references/unresolved")
def unresolved_references(
    document_id: Optional[str] = None,
    session: Session = Depends(db_session),
) -> List[Dict[str, Any]]:
    """返回尚未解析到码表的字段引用（附带所属报表与字段信息）。"""
    return svc.list_unresolved_code_references(session, document_id=document_id)


@router.post("/references/relink")
def relink_references(session: Session = Depends(db_session)) -> Dict[str, Any]:
    """重新对齐“字段引用 → 码值表”，并把引用原文里自带的内联枚举补成码值。"""
    return svc.relink_all_code_references(session)


@router.get("/readiness")
def code_set_readiness(
    document_id: Optional[str] = None,
    referenced_only: bool = False,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    """码表发布前的检查：空表、缺中文含义、码值超过字段长度。"""
    return evaluate_code_sets(session, document_id=document_id, referenced_only=referenced_only)


@router.post("/publish-batch")
def publish_code_sets_batch(
    request: PublishBatchRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    """批量发布码表（仅发布检查通过项，其余连原因一起返回）。"""
    return svc.publish_code_sets_batch(
        session,
        document_id=request.document_id,
        code_set_ids=request.code_set_ids,
        referenced_only=request.referenced_only,
        dry_run=request.dry_run,
    )


@router.get("/{code_set_id}")
def get_code_set(code_set_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    row = session.get(CodeSetModel, code_set_id)
    if not row:
        raise HTTPException(status_code=404, detail="Code set not found")
    return svc.code_set_detail(session, row)


@router.patch("/{code_set_id}")
def edit_code_set(
    code_set_id: str,
    request: VersionRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    return svc.edit_code_set_record(
        session,
        code_set_id,
        expected_version=request.expected_version,
        changes=request.changes,
    )


@router.delete("/{code_set_id}")
def delete_code_set(code_set_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    """删掉一张未被字段引用的码表。"""
    row = session.get(CodeSetModel, code_set_id)
    if not row:
        raise HTTPException(status_code=404, detail="Code set not found")
    try:
        result = remove_code_set(session, row)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "CODE_SET_IN_USE", "message": str(exc)},
        ) from exc
    session.commit()
    return result


@router.post("/{code_set_id}/values")
def add_value(
    code_set_id: str,
    request: CodeValueRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    return svc.add_code_value_record(
        session,
        code_set_id,
        expected_version=request.expected_version,
        editor=request.editor,
        value=request.value,
        label_cn=request.label_cn,
        label_en=request.label_en,
        definition=request.definition,
    )


@router.patch("/{code_set_id}/values/{value_id}")
def edit_value(
    code_set_id: str,
    value_id: str,
    request: VersionRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    return svc.edit_code_value_record(
        session,
        code_set_id,
        value_id,
        expected_version=request.expected_version,
        changes=request.changes,
    )


@router.post("/{code_set_id}/publish")
def publish_code_set(
    code_set_id: str,
    request: VersionRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    return svc.publish_code_set_record(session, code_set_id, expected_version=request.expected_version)


@router.post("/{code_set_id}/import")
async def import_values(
    code_set_id: str,
    expected_version: int = Form(...),
    file: UploadFile = File(...),
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    return svc.import_code_values_xlsx(
        session,
        code_set_id,
        expected_version=expected_version,
        filename=file.filename or "",
        content=await file.read(),
    )
