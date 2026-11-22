"""规则工作台产物路由（`/api/artifacts/*`）：薄路由，业务聚合委托给 `artifacts_service.py`。"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.common import db_session
from app.api.generation.artifacts_service import (
    get_table_artifacts_detail,
    list_all_table_rules,
    list_artifacts_status,
    update_rule_params,
)
from app.infra.config import OPERATOR_PLACEHOLDER, get_settings

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


class RuleEditRequest(BaseModel):
    params: Dict[str, Any] = Field(default_factory=dict)
    editor: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)
    reason: str = ""


@router.get("/rules")
def all_table_rules(
    document_id: Optional[str] = None,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    return list_all_table_rules(session, document_id=document_id)


@router.get("")
def artifacts_status(
    document_id: Optional[str] = None,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    return list_artifacts_status(session, document_id=document_id)


@router.patch("/rules/{rule_row_id}")
def edit_rule(
    rule_row_id: str,
    request: RuleEditRequest,
    session: Session = Depends(db_session),
) -> Dict[str, Any]:
    return update_rule_params(
        session,
        rule_row_id,
        params=request.params,
        editor=request.editor,
        reason=request.reason,
    )


@router.get("/{table_id}")
def table_artifacts(table_id: str, session: Session = Depends(db_session)) -> Dict[str, Any]:
    return get_table_artifacts_detail(session, table_id, get_settings())
