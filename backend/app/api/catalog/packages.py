"""多文件监管规范包路由（`/api/packages/*`）。"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.catalog import packages_service as svc
from app.api.common import db_session
from app.catalog.extraction import reconcile_package_assets

router = APIRouter(prefix="/packages", tags=["packages"])


class PackageCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    regulator: str = "SAFE"
    spec_version: str = "UNKNOWN"
    parent_package_id: Optional[str] = None
    description: str = ""


class FileRoleRequest(BaseModel):
    doc_role: str = Field(min_length=1, max_length=32)


class PackageExtractRequest(BaseModel):
    targets: Optional[List[str]] = None
    document_ids: Optional[List[str]] = None
    reconcile: bool = True


@router.post("", status_code=201)
def create_package(request: PackageCreateRequest, session: Session = Depends(db_session)) -> Dict[str, Any]:
    return svc.create_package(
        session,
        name=request.name,
        regulator=request.regulator,
        spec_version=request.spec_version,
        parent_package_id=request.parent_package_id,
        description=request.description,
    )


@router.get("")
def list_packages(session: Session = Depends(db_session)) -> List[Dict[str, Any]]:
    return svc.list_packages(session)


@router.get("/diff")
def diff_packages_endpoint(
    base_id: str,
    target_id: str,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    return svc.compare_packages_view(session, base_id, target_id)


@router.get("/{package_id}")
def get_package(package_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    return svc.get_package_detail(session, package_id)


@router.delete("/{package_id}")
def delete_package(package_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    return svc.delete_package(session, package_id)


@router.post("/{package_id}/files", status_code=201)
async def upload_package_files(
    package_id: str,
    files: List[UploadFile] = File(...),
    doc_role: str = Form(default="AUTO"),
    prepare: bool = Form(default=True),
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    payload = [(f.filename or "", await f.read()) for f in files]
    return svc.upload_files_to_package(session, package_id, payload, doc_role=doc_role, prepare=prepare)


@router.patch("/{package_id}/files/{document_id}")
def update_package_file_role(
    package_id: str,
    document_id: str,
    request: FileRoleRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    return svc.update_file_role(session, package_id, document_id, request.doc_role)


@router.put("/{package_id}/files/{document_id}")
async def replace_package_file(
    package_id: str,
    document_id: str,
    file: UploadFile = File(...),
    doc_role: str = Form(default="AUTO"),
    prepare: bool = Form(default=True),
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    return svc.replace_file_in_package(
        session,
        package_id,
        document_id,
        file.filename or "",
        await file.read(),
        doc_role=doc_role,
        prepare=prepare,
    )


@router.delete("/{package_id}/files/{document_id}")
def delete_package_file(
    package_id: str,
    document_id: str,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    return svc.remove_file_from_package(session, package_id, document_id)


@router.post("/{package_id}/extract")
def extract_package(
    package_id: str,
    request: Optional[PackageExtractRequest] = None,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    req = request or PackageExtractRequest()
    return svc.extract_and_reconcile_package(
        session,
        package_id,
        targets=req.targets,
        document_ids=req.document_ids,
        reconcile=req.reconcile,
    )


@router.post("/{package_id}/reconcile")
def reconcile_package(package_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    result = reconcile_package_assets(session, package_id)
    session.commit()
    return result


@router.get("/{package_id}/rule-evidences")
def list_rule_evidences(
    package_id: str,
    table_code: Optional[str] = None,
    session: Session = Depends(db_session),
) -> List[Dict[str, Any]]:
    return svc.list_package_rule_evidences(session, package_id, table_code=table_code)
