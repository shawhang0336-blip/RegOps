"""运行批次控制路由（`/api/runs/*`）：仅负责 HTTP 契约、状态门禁与服务委托。"""

from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.common import (
    db_session,
    run_tables,
    run_view,
    spawn_run_driver,
)
from app.api.delivery.runs_service import (
    build_validation_detail,
    delete_run_rows as _delete_run_rows,
    list_generation_runs,
)
from app.delivery.run_diff import diff_runs
from app.delivery.workflow import (
    active_generic_runs,
    approve_generic_rules,
    cancel_generic_run,
    cleanup_history_runs,
    confirm_generic_test_results,
    delete_generic_run,
    history_cleanup_plan,
    reaudit_generic_run,
    replay_generic_run,
    retry_generic_run,
    start_generic_run,
)
from app.generation.sql.oracle import OracleSandbox
from app.infra.config import OPERATOR_PLACEHOLDER, get_settings
from app.infra.db_tables import RunModel
from app.infra.errors import WorkflowError
from app.kernel.bus import global_bus
from app.kernel.event_log import project_trace
from app.policy.regops_plan import project, resolve_run_plan

router = APIRouter(tags=["runs"])


class RunRequest(BaseModel):
    spec_id: str
    provider: str = "live"


class TestConfirmationRequest(BaseModel):
    approver: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)


class Decision(BaseModel):
    rule_hash: str
    decision: str


class ApprovalRequest(BaseModel):
    approver: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)
    decisions: List[Decision]
    provider: str = "live"


class CancelRunRequest(BaseModel):
    reason: str = "Run cancelled by operator"


def _spawn_run_continuation(run_id: str) -> None:
    spawn_run_driver(run_id, get_settings(), allowed_statuses=("READY_TO_AUDIT", "RUNNING"))


def _require_run(session: Session, run_id: str) -> RunModel:
    run = session.get(RunModel, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.post("/api/runs", status_code=201)
def create_run(request: RunRequest, session: Session = Depends(db_session)) -> Dict:
    if request.provider != "live":
        raise HTTPException(status_code=422, detail="The production path requires provider=live")
    try:
        run = start_generic_run(session, request.spec_id, get_settings())
    except (WorkflowError, RuntimeError, ValueError) as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return run_view(session, run)


@router.get("/api/runs")
def list_runs(session: Session = Depends(db_session)) -> List[Dict]:
    return list_generation_runs(session)


@router.get("/api/runs/active")
def list_active_runs() -> Dict:
    ids = active_generic_runs()
    return {"count": len(ids), "run_ids": ids}


@router.get("/api/runs/history/cleanup-preview")
def history_cleanup_preview(session: Session = Depends(db_session)) -> Dict:
    runs, deletes, states = history_cleanup_plan(session)
    return {
        "total": len(runs),
        "keep_count": len(runs) - len(deletes),
        "delete_count": len(deletes),
        "runs": [
            {
                "id": run.id,
                "status": states[run.id].status,
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "tables": run_tables(session, run),
            }
            for run in deletes
        ],
    }


@router.post("/api/runs/history/cleanup")
def history_cleanup(session: Session = Depends(db_session)) -> Dict:
    return cleanup_history_runs(session, get_settings())


@router.get("/api/runs/diff")
def compare_runs(
    base_run_id: str = Query(min_length=1),
    target_run_id: str = Query(min_length=1),
    session: Session = Depends(db_session),
) -> Dict:
    try:
        return diff_runs(session, base_run_id, target_run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/runs/{run_id}")
def get_run(run_id: str, session: Session = Depends(db_session)) -> Dict:
    return run_view(session, _require_run(session, run_id))


@router.delete("/api/runs/{run_id}")
def delete_run(run_id: str, session: Session = Depends(db_session)) -> Dict:
    run = _require_run(session, run_id)
    try:
        return delete_generic_run(session, run, get_settings())
    except WorkflowError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/runs/{run_id}/validation-detail")
def validation_detail(run_id: str, session: Session = Depends(db_session)) -> Dict:
    return build_validation_detail(session, _require_run(session, run_id), get_settings())


@router.post("/api/runs/{run_id}/approvals")
def approve_run(run_id: str, request: ApprovalRequest, session: Session = Depends(db_session)) -> Dict:
    run = _require_run(session, run_id)
    try:
        run = approve_generic_rules(
            session,
            run,
            request.approver.strip(),
            {decision.rule_hash: decision.decision for decision in request.decisions},
            get_settings(),
            defer_continuation=True,
        )
    except WorkflowError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if project(session, run).status == "READY_TO_AUDIT":
        _spawn_run_continuation(run.id)
    return run_view(session, run)


@router.post("/api/runs/{run_id}/test-confirmation")
def confirm_test_results(
    run_id: str,
    request: TestConfirmationRequest,
    session: Session = Depends(db_session),
) -> Dict:
    run = _require_run(session, run_id)
    try:
        run = confirm_generic_test_results(session, run, request.approver.strip())
    except WorkflowError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run_view(session, run)


@router.post("/api/runs/{run_id}/retry")
def retry_run(run_id: str, session: Session = Depends(db_session)) -> Dict:
    run = _require_run(session, run_id)
    if project(session, run).status != "FAILED":
        raise HTTPException(status_code=409, detail="Only failed runs can be retried")
    try:
        run = retry_generic_run(session, run, get_settings(), defer_continuation=True)
    except WorkflowError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if project(session, run).status in {"RUNNING", "READY_TO_AUDIT"}:
        _spawn_run_continuation(run.id)
    return run_view(session, run)


@router.post("/api/runs/{run_id}/cancel")
def cancel_run(
    run_id: str,
    request: Optional[CancelRunRequest] = None,
    session: Session = Depends(db_session),
) -> Dict:
    run = _require_run(session, run_id)
    reason = (request.reason.strip() if request and request.reason else "") or "Run cancelled by operator"
    try:
        return cancel_generic_run(session, run, reason=reason)
    except WorkflowError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/runs/{run_id}/reaudit")
def reaudit_run(run_id: str, session: Session = Depends(db_session)) -> Dict:
    run = _require_run(session, run_id)
    try:
        run = reaudit_generic_run(session, run, get_settings())
    except WorkflowError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run_view(session, run)


@router.get("/api/runs/{run_id}/replay")
def replay_run(run_id: str, session: Session = Depends(db_session)) -> Dict:
    return replay_generic_run(session, _require_run(session, run_id))


@router.get("/api/runs/{run_id}/trace")
def trace_run(run_id: str, session: Session = Depends(db_session)) -> Dict:
    run = _require_run(session, run_id)
    return project_trace(session, run, plan=resolve_run_plan(session, run)).as_dict()


@router.get("/api/runs/{run_id}/bus-events")
def run_bus_events(run_id: str, topic: Optional[str] = Query(default=None)) -> Dict:
    events = global_bus().history(topic=topic, run_id=run_id)
    return {"run_id": run_id, "count": len(events), "events": events}


@router.get("/api/runs/{run_id}/oracle-preview")
def oracle_preview(
    run_id: str,
    table_code: str = Query(min_length=1),
    limit: int = Query(default=20, ge=1, le=100),
    dataset: str = Query(default="audit", pattern="^(audit|golden)$"),
    session: Session = Depends(db_session),
) -> List[Dict]:
    settings = get_settings()
    run = session.get(RunModel, run_id)
    result_key = "oracle_target_tables" if dataset == "golden" else "oracle_tables"
    state = project(session, run) if run else None
    if not run or not state.result_json or not state.result_json.get(result_key):
        raise HTTPException(status_code=404, detail="Oracle run artifacts not found")
    physical_table = state.result_json[result_key].get(table_code)
    if not physical_table:
        raise HTTPException(status_code=404, detail="Table is outside this run")
    if not settings.oracle_sandbox_url:
        raise HTTPException(status_code=422, detail="Oracle sandbox is not configured")
    try:
        return OracleSandbox(settings.oracle_sandbox_url).preview(physical_table, limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Oracle preview failed: {type(exc).__name__}") from exc
