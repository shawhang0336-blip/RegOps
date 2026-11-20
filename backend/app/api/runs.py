"""装配运行批次路由（`/api/runs/*`）：创建、查询、清理、人工审批、测试确认、重试重审与回放。"""

import json
import threading
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps import (
    ACTIVE_RUN_STATUSES,
    ApprovalRequest,
    RunRequest,
    TestConfirmationRequest,
    db_session,
    run_tables,
    run_tables_from_content,
    run_view,
)
from app.engine.oracle import OracleSandbox
from app.engine.workflow import (
    _pending_rules,
    approve_generic_rules,
    confirm_generic_test_results,
    drive,
    retry_generic_run,
    start_generic_run,
)
from app.infra.asset_models import (
    ArtifactModel,
    AuditCaseDefinitionModel,
    GenerationPlanModel,
    GeneratorDefinitionModel,
    SchemaDefinitionModel,
)
from app.infra.config import get_settings
from app.infra.db import SessionLocal
from app.infra.errors import WorkflowError
from app.infra.models import EventModel, RuleModel, RunModel, SpecificationModel
from app.kernel.blob_store import LocalBlobStore
from app.kernel.bus import global_bus
from app.kernel.channel import resolve_event_tables
from app.kernel.event_log import append_event, verify_chain
from app.policy.regops_plan import project, project_many

router = APIRouter(tags=["runs"])


# ── 历史批次清理与后台续跑辅助 ───────────────────────────────────────────────


def _history_cleanup_plan(session: Session):
    """每组报表保留最新批次和最近一次通过验证的批次，运行中批次永不清理。"""
    runs = session.query(RunModel).order_by(RunModel.created_at.desc()).all()
    states = project_many(session, runs)
    groups: Dict[tuple, List[RunModel]] = {}
    for run in runs:
        key = tuple(sorted(item["table_code"] for item in run_tables(session, run))) or (run.spec_id,)
        groups.setdefault(key, []).append(run)
    keep = set()
    for group in groups.values():
        ordered = sorted(group, key=lambda item: item.created_at or datetime.min, reverse=True)
        keep.add(ordered[0].id)
        completed = next((item for item in ordered if states[item.id].status == "COMPLETED"), None)
        if completed:
            keep.add(completed.id)
        keep.update(item.id for item in ordered if states[item.id].status in ACTIVE_RUN_STATUSES)
    deletes = [run for run in runs if run.id not in keep]
    return runs, deletes, states


def _delete_run_rows(session: Session, run: RunModel):
    result = project(session, run).result_json or {}
    oracle_tables = set()
    for key in ("oracle_tables", "oracle_target_tables", "oracle_code_set_tables"):
        oracle_tables.update((result.get(key) or {}).values())
    blob_uris = {row.blob_uri for row in session.query(ArtifactModel).filter(ArtifactModel.run_id == run.id).all()}
    for model in (
        RuleModel,
        EventModel,
        ArtifactModel,
        GenerationPlanModel,
        GeneratorDefinitionModel,
        SchemaDefinitionModel,
        AuditCaseDefinitionModel,
    ):
        session.query(model).filter(model.run_id == run.id).delete(synchronize_session=False)
    session.delete(run)
    return blob_uris, oracle_tables


def _finish_run_deletion(session: Session, blob_uris: set, oracle_tables: set) -> List[str]:
    settings = get_settings()
    session.flush()
    remaining_uris = {row[0] for row in session.query(ArtifactModel.blob_uri).distinct().all()}
    store = LocalBlobStore(settings.blob_dir)
    for uri in blob_uris - remaining_uris:
        try:
            store.delete(uri)
        except ValueError:
            pass
    session.commit()
    if not oracle_tables or not settings.oracle_sandbox_url:
        return []
    try:
        return OracleSandbox(settings.oracle_sandbox_url).drop_tables(sorted(oracle_tables))
    except Exception as exc:
        return [f"沙箱表清理失败：{type(exc).__name__}"]


def _latest_event_tables(events: List[EventModel], kind: str) -> Dict:
    blob_store = LocalBlobStore(get_settings().blob_dir)
    tables: Dict = {}
    for event in events:
        if event.kind == kind:
            tables = resolve_event_tables(event.payload_json or {}, blob_store)
    return tables


def _spawn_run_continuation(run_id: str) -> None:
    """在后台线程里继续跑造数 + Oracle 验证，让审批/重试/重审接口立即返回。"""
    settings = get_settings()

    def _run():
        try:
            with SessionLocal() as session:
                run = session.get(RunModel, run_id)
                if run and project(session, run).status in {"READY_TO_AUDIT", "RUNNING"}:
                    drive(session, run, settings)
        except Exception:
            with SessionLocal() as session:
                run = session.get(RunModel, run_id)
                if run and project(session, run).status in {"READY_TO_AUDIT", "RUNNING"}:
                    append_event(
                        session,
                        run,
                        "RUN_FAILED",
                        "background_executor",
                        {"node": "background_executor", "result": {"error": "后台续跑线程异常退出"}},
                    )
                    session.commit()

    threading.Thread(target=_run, daemon=True).start()


# ── Run 生命周期路由 ──────────────────────────────────────────────────────────


@router.post("/api/runs", status_code=201)
def create_run(request: RunRequest, session: Session = Depends(db_session)) -> Dict:
    if request.provider != "live":
        raise HTTPException(
            status_code=422,
            detail="The production path requires provider=live and programmatic GLM calls",
        )
    try:
        run = start_generic_run(session, request.spec_id, get_settings())
    except (WorkflowError, RuntimeError, ValueError) as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return run_view(session, run)


@router.get("/api/runs")
def list_runs(session: Session = Depends(db_session)) -> List[Dict]:
    runs = session.query(RunModel).order_by(RunModel.created_at.desc()).limit(50).all()
    if not runs:
        return []
    states = project_many(session, runs)
    run_ids = [run.id for run in runs]
    spec_ids = {run.spec_id for run in runs}
    rules_by_run: Dict[str, List[RuleModel]] = {}
    for rule in session.query(RuleModel).filter(RuleModel.run_id.in_(run_ids)).all():
        rules_by_run.setdefault(rule.run_id, []).append(rule)
    spec_by_id = {
        row.id: row
        for row in session.query(SpecificationModel).filter(SpecificationModel.id.in_(spec_ids)).all()
    }
    return [
        run_view(
            session,
            run,
            include_events=False,
            run_state=states[run.id],
            prefetched_rules=rules_by_run.get(run.id, []),
            prefetched_tables=run_tables_from_content(
                (spec_by_id[run.spec_id].content_json or {}) if run.spec_id in spec_by_id else {}
            ),
        )
        for run in runs
    ]


@router.get("/api/runs/history/cleanup-preview")
def history_cleanup_preview(session: Session = Depends(db_session)) -> Dict:
    runs, deletes, states = _history_cleanup_plan(session)
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
    _, deletes, _ = _history_cleanup_plan(session)
    deleted_ids = [run.id for run in deletes]
    blob_uris = set()
    oracle_tables = set()
    for run in deletes:
        run_blobs, run_tables_set = _delete_run_rows(session, run)
        blob_uris.update(run_blobs)
        oracle_tables.update(run_tables_set)
    warnings = _finish_run_deletion(session, blob_uris, oracle_tables)
    return {"deleted_count": len(deleted_ids), "deleted_ids": deleted_ids, "sandbox_cleanup": warnings}


@router.get("/api/runs/{run_id}")
def get_run(run_id: str, session: Session = Depends(db_session)) -> Dict:
    run = session.get(RunModel, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run_view(session, run)


@router.delete("/api/runs/{run_id}")
def delete_run(run_id: str, session: Session = Depends(db_session)) -> Dict:
    run = session.get(RunModel, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if project(session, run).status in ACTIVE_RUN_STATUSES:
        raise HTTPException(status_code=409, detail="Running validation batches cannot be deleted")
    blob_uris, oracle_tables = _delete_run_rows(session, run)
    warnings = _finish_run_deletion(session, blob_uris, oracle_tables)
    return {"deleted_id": run_id, "sandbox_cleanup": warnings}


@router.get("/api/runs/{run_id}/validation-detail")
def validation_detail(run_id: str, session: Session = Depends(db_session)) -> Dict:
    """把 AI 数据定义和 Oracle 审计报告整理成可浏览、可逐规则核对的结果。"""
    settings = get_settings()
    run = session.get(RunModel, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    result = project(session, run).result_json or {}
    report: Dict = {}
    report_uri = result.get("audit_report_blob")
    if report_uri:
        try:
            report = json.loads(LocalBlobStore(settings.blob_dir).get(report_uri).decode("utf-8"))
        except (ValueError, FileNotFoundError, json.JSONDecodeError):
            report = {}

    events = session.query(EventModel).filter(EventModel.run_id == run.id).order_by(EventModel.seq).all()
    generators = _latest_event_tables(events, "GENERATORS_ASSIGNED")
    plans = _latest_event_tables(events, "GENERATION_PLANNED")
    case_definitions = _latest_event_tables(events, "AUDIT_CASES_ASSIGNED")
    case_definition_map = {
        (table_code, item.get("case_id")): item
        for table_code, items in case_definitions.items()
        for item in items
    }

    approved = session.query(RuleModel).filter(RuleModel.run_id == run.id, RuleModel.status == "APPROVED").all()
    rule_map: Dict = {}
    for row in approved:
        definition = row.definition_json or {}
        rule_map[(row.table_code, row.rule_key)] = {
            "rule_hash": row.rule_hash,
            "rule_key": row.rule_key,
            "table_code": row.table_code,
            "field": definition.get("field"),
            "operator": definition.get("operator"),
            "params": definition.get("params") or {},
            "source": row.source,
            "status": row.status,
            "rationale": definition.get("rationale") or "",
            "expected_case_ids": [],
            "hit_case_ids": [],
            "missing_case_ids": [],
            "unexpected_case_ids": [],
            "co_triggered_case_ids": [],
        }

    cases = []
    for raw in report.get("cases") or []:
        table_code = raw.get("table_code")
        case_id = raw.get("case_id")
        definition = case_definition_map.get((table_code, case_id), {})
        case = {
            **raw,
            "case_kind": raw.get("case_kind") or ("GOLDEN" if "GOLDEN" in str(case_id).upper() else "MUTATION"),
            "overrides": definition.get("overrides") or {},
            "expected_rule_keys": definition.get("expected_rule_keys") or raw.get("expected_rules") or [],
        }
        cases.append(case)
        for key in case.get("expected_rules") or []:
            rule = rule_map.get((table_code, key))
            if rule:
                rule["expected_case_ids"].append(case_id)
        for key in case.get("actual_rules") or []:
            rule = rule_map.get((table_code, key))
            if rule:
                rule["hit_case_ids"].append(case_id)
        for key in case.get("missing_rules") or []:
            rule = rule_map.get((table_code, key))
            if rule:
                rule["missing_case_ids"].append(case_id)
        for key in case.get("unexpected_rules") or []:
            rule = rule_map.get((table_code, key))
            if rule:
                rule["unexpected_case_ids"].append(case_id)
        for key in case.get("co_triggered_rules") or []:
            rule = rule_map.get((table_code, key))
            if rule:
                rule["co_triggered_case_ids"].append(case_id)

    rules = []
    for rule in rule_map.values():
        covered = bool(rule["hit_case_ids"])
        failed = bool(rule["missing_case_ids"] or rule["unexpected_case_ids"])
        rules.append(
            {
                **rule,
                "covered": covered,
                "has_target_case": bool(rule["expected_case_ids"]),
                "effective": covered and not failed,
                "result": "FAILED" if failed else "EFFECTIVE" if covered else "UNCOVERED",
            }
        )
    rules.sort(key=lambda item: (item["table_code"], item["field"] or "", item["rule_key"]))

    spec = session.get(SpecificationModel, run.spec_id)
    spec_content = (spec.content_json or {}) if spec else {}
    spec_tables = spec_content.get("tables") or ([spec_content] if spec_content else [])
    table_names = {item.get("table_code"): item.get("table_name") for item in spec_tables}
    table_codes = sorted(set(result.get("oracle_tables") or {}) | set(generators) | set(table_names))
    test_data = []
    for table_code in table_codes:
        generator = generators.get(table_code) or {}
        plan = plans.get(table_code) or generator.get("generation_plan") or {}
        audit_count = sum(1 for case in cases if case.get("table_code") == table_code)
        test_data.append(
            {
                "table_code": table_code,
                "table_name": table_names.get(table_code) or table_code,
                "generation_mode": "AI_FIELD_ASSIGNMENT",
                "generator_count": len(generator.get("generators") or []),
                "golden_record": generator.get("golden_record") or {},
                "golden_count": int(plan.get("golden_count") or 1),
                "audit_count": audit_count,
                "generation_plan": plan,
                "audit_preview_url": (
                    f"/api/runs/{run.id}/oracle-preview?table_code={table_code}&dataset=audit&limit=100"
                    if (result.get("oracle_tables") or {}).get(table_code)
                    else None
                ),
                "golden_preview_url": (
                    f"/api/runs/{run.id}/oracle-preview?table_code={table_code}&dataset=golden&limit=100"
                    if (result.get("oracle_target_tables") or {}).get(table_code)
                    else None
                ),
            }
        )

    active_hashes = set(result.get("active_rule_hashes") or [])
    approved_hashes = {row.rule_hash for row in approved}
    summary = {key: value for key, value in report.items() if key != "cases"} or (result.get("audit_summary") or {})
    return {
        "run_id": run.id,
        "status": project(session, run).status,
        "ai_generated": bool(generators),
        "audit_stale": bool(active_hashes and active_hashes != approved_hashes),
        "summary": summary,
        "rules": rules,
        "cases": cases,
        "test_data": test_data,
        "coverage_statement": report.get("coverage_statement")
        or "结果仅对本次透明展示的 AI 审计参数负责，不代表监管语义完备。",
    }


@router.post("/api/runs/{run_id}/approvals")
def approve_run(run_id: str, request: ApprovalRequest, session: Session = Depends(db_session)) -> Dict:
    run = session.get(RunModel, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
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
def confirm_test_results(run_id: str, request: TestConfirmationRequest, session: Session = Depends(db_session)) -> Dict:
    run = session.get(RunModel, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    try:
        run = confirm_generic_test_results(session, run, request.approver.strip())
    except WorkflowError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run_view(session, run)


@router.post("/api/runs/{run_id}/retry")
def retry_run(run_id: str, session: Session = Depends(db_session)) -> Dict:
    run = session.get(RunModel, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
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


@router.post("/api/runs/{run_id}/reaudit")
def reaudit_run(run_id: str, session: Session = Depends(db_session)) -> Dict:
    """人工改参后的轻量重审：规则集合保持不动，只重跑造数与 Oracle 沙箱验证。"""
    run = session.get(RunModel, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if project(session, run).status in ACTIVE_RUN_STATUSES:
        raise HTTPException(status_code=409, detail="批次正在执行中，请等当前验证结束后再发起重审")
    pending = _pending_rules(session, run.id)
    if pending:
        raise HTTPException(status_code=409, detail=f"还有 {len(pending)} 条规则待确认，请先在第二步处理完再重审")
    approved = (
        session.query(RuleModel)
        .filter(RuleModel.run_id == run.id, RuleModel.status == "APPROVED")
        .count()
    )
    if not approved:
        raise HTTPException(status_code=409, detail="这个批次没有已批准的规则可验证")
    for required_kind in ("SCHEMA_ASSIGNED", "CROSS_RULES_ASSIGNED"):
        exists = (
            session.query(EventModel)
            .filter(EventModel.run_id == run.id, EventModel.kind == required_kind)
            .first()
        )
        if not exists:
            raise HTTPException(
                status_code=409,
                detail="该批次缺少生成上下文（未完成过规则绑定），请改用“重新生成并验证”",
            )
    append_event(
        session,
        run,
        "REAUDIT_REQUESTED",
        "build_audit",
        {"reason": "保留规则与人工修改，仅重跑 AI 造数与 Oracle 沙箱验证"},
    )
    session.commit()
    _spawn_run_continuation(run.id)
    return run_view(session, run)


@router.get("/api/runs/{run_id}/replay")
def replay_run(run_id: str, session: Session = Depends(db_session)) -> Dict:
    run = session.get(RunModel, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    state = project(session, run)
    return {
        "status": state.status,
        "current_node": state.current_node,
        "attempt": state.attempt,
        "result": state.result_json,
        "chain_valid": verify_chain(session, run),
    }


@router.get("/api/runs/{run_id}/bus-events")
def run_bus_events(run_id: str, topic: Optional[str] = Query(default=None)) -> Dict:
    """查看某个 Run 在全生命周期 Bus 上广播的实时事件流。"""
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
    try:
        return OracleSandbox(settings.oracle_sandbox_url).preview(physical_table, limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Oracle preview failed: {type(exc).__name__}") from exc
