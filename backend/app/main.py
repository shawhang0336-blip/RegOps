import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .api_v2 import artifacts as artifacts_v2
from .api_v2 import code_sets as code_sets_v2
from .api_v2 import documents as documents_v2
from .api_v2 import generation as generation_v2
from .api_v2 import metadata as metadata_v2
from .api_v2 import settings as settings_v2
from .blobs import LocalBlobStore
from .business import BusinessDatabaseError, ReadOnlyBusinessDatabase
from .catalog import parse_document_catalog
from .config import get_settings
from .identity import OPERATOR_PLACEHOLDER
from .db import SessionLocal, init_db
from .events import append_event, fold_events
from .generic_workflow import approve_generic_rules, confirm_generic_test_results, retry_generic_run, start_generic_run
from .asset_models import (
    ArtifactModel,
    AuditCaseDefinitionModel,
    GenerationPlanModel,
    GeneratorDefinitionModel,
    SchemaDefinitionModel,
)
from .models import EventModel, RuleModel, RunModel, SpecificationModel
from .oracle_sandbox import OracleSandbox
from .spec_parser import SpecParseError
from .errors import WorkflowError


def _mark_interrupted_runs():
    with SessionLocal() as session:
        rows = (
            session.query(RunModel)
            .filter(RunModel.status.in_(["RUNNING", "AUDITING", "READY_TO_AUDIT"]))
            .all()
        )
        for run in rows:
            append_event(
                session,
                run,
                "RUN_FAILED",
                "startup",
                {"node": "startup", "result": {"error": "Run interrupted by process restart"}},
            )
        if rows:
            session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _mark_interrupted_runs()
    yield


app = FastAPI(title="监管规则装配与审计台", version="0.1.0", lifespan=lifespan)
settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(settings_v2.router, prefix="/api/v2")
app.include_router(documents_v2.router, prefix="/api/v2")
app.include_router(metadata_v2.router, prefix="/api/v2")
app.include_router(code_sets_v2.router, prefix="/api/v2")
app.include_router(generation_v2.router, prefix="/api/v2")
app.include_router(artifacts_v2.router, prefix="/api/v2")


def db_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


class ImportRequest(BaseModel):
    pass


class RunRequest(BaseModel):
    spec_id: str
    provider: str = "live"


class ConfirmationRequest(BaseModel):
    table_codes: List[str]
    approver: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)


class TestConfirmationRequest(BaseModel):
    approver: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)


class Decision(BaseModel):
    rule_hash: str
    decision: str


class ApprovalRequest(BaseModel):
    approver: str = Field(default=OPERATOR_PLACEHOLDER, max_length=80)
    decisions: List[Decision]
    provider: str = "live"


def spec_summary(row: SpecificationModel) -> Dict:
    content = row.content_json
    is_catalog = content.get("document_kind") == "CATALOG"
    if is_catalog:
        field_count = sum(table.get("field_count", 0) for table in content["tables"])
        eligible_count = sum(1 for table in content["tables"] if table["eligible"])
    elif "tables" in content:
        field_count = sum(len(table["fields"]) for table in content["tables"])
        eligible_count = len(content["tables"])
    else:
        field_count = len(content["fields"])
        eligible_count = 1
    return {
        "id": row.id,
        "version": row.version,
        "table_code": row.table_code,
        "table_name": content["table_name"],
        "source_name": row.source_name,
        "source_sha256": row.source_sha256,
        "field_count": field_count,
        "table_count": len(content["tables"]) if "tables" in content else 1,
        "eligible_count": eligible_count,
        "document_kind": content.get("document_kind", "CONFIRMED_SNAPSHOT"),
        "selection_confirmed": bool(content.get("selection_confirmed")),
        "created_at": row.created_at.isoformat(),
    }


def rule_view(row: RuleModel) -> Dict:
    return {
        "rule_hash": row.rule_hash,
        "table_code": row.table_code,
        "rule_key": row.rule_key,
        "source": row.source,
        "status": row.status,
        "definition": row.definition_json,
    }


def _run_tables(session: Session, run: RunModel) -> List[Dict]:
    spec = session.get(SpecificationModel, run.spec_id)
    content = (spec.content_json or {}) if spec else {}
    tables = content.get("tables") or ([content] if content else [])
    return [
        {"table_code": item.get("table_code"), "table_name": item.get("table_name")}
        for item in tables
        if item.get("table_code")
    ]


def run_view(session: Session, run: RunModel, include_events: bool = True) -> Dict:
    rules = session.query(RuleModel).filter(RuleModel.run_id == run.id).all()
    events = session.query(EventModel).filter(EventModel.run_id == run.id).order_by(EventModel.seq).all()
    tables = _run_tables(session, run)
    result = {
        "id": run.id,
        "spec_id": run.spec_id,
        "status": run.status,
        "current_node": run.current_node,
        "attempt": run.attempt,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        "tables": tables,
        "table_count": len(tables),
        "result": run.result_json,
        "rules": [rule_view(rule) for rule in sorted(rules, key=lambda item: item.rule_key)],
    }
    if include_events:
        result["events"] = [
            {
                "seq": event.seq,
                "kind": event.kind,
                "node": event.node,
                "payload": event.payload_json,
                "event_hash": event.event_hash,
                "created_at": event.created_at.isoformat(),
            }
            for event in events
        ]
    return result


@app.get("/api/health")
def health() -> Dict:
    return {
        "status": "ok",
        "database": settings.database_url.split(":", 1)[0],
        "llm_provider": settings.llm_provider,
        "business_database_configured": bool(settings.business_database_url),
    }


def _store_catalog(session: Session, entries: List[Dict], source_name: str, digest: str) -> Dict:
    existing = (
        session.query(SpecificationModel)
        .filter(
            SpecificationModel.source_sha256 == digest,
            SpecificationModel.table_code == "DOCUMENT_CATALOG",
        )
        .one_or_none()
    )
    if existing:
        return spec_summary(existing)
    document_version = next((entry["spec"]["version"] for entry in entries if entry.get("spec")), "UNKNOWN")
    row = SpecificationModel(
        id=str(uuid.uuid4()),
        version=document_version,
        table_code="DOCUMENT_CATALOG",
        source_name=source_name,
        source_sha256=digest,
        content_json={
            "document_kind": "CATALOG",
            "version": document_version,
            "table_name": source_name,
            "tables": entries,
        },
    )
    session.add(row)
    session.commit()
    return spec_summary(row)


@app.post("/api/specs/import-safe-13", status_code=201)
def import_safe_13(request: ImportRequest, session: Session = Depends(db_session)) -> Dict:
    try:
        entries = parse_document_catalog(settings.safe_13_spec_path)
    except SpecParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    digest = hashlib.sha256(Path(settings.safe_13_spec_path).read_bytes()).hexdigest()
    return _store_catalog(session, entries, Path(settings.safe_13_spec_path).name, digest)


@app.post("/api/documents", status_code=201)
async def upload_document(file: UploadFile = File(...), session: Session = Depends(db_session)) -> Dict:
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Only PDF documents are supported")
    content = await file.read()
    if len(content) > 30 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="PDF exceeds the 30 MB limit")
    digest = hashlib.sha256(content).hexdigest()
    target = Path(settings.blob_dir) / "uploads" / f"{digest}.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    try:
        entries = parse_document_catalog(str(target))
    except SpecParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _store_catalog(session, entries, file.filename, digest)


@app.get("/api/specs")
def list_specs(session: Session = Depends(db_session)) -> List[Dict]:
    rows = session.query(SpecificationModel).all()
    rows.sort(key=lambda row: row.created_at, reverse=True)
    return [spec_summary(row) for row in rows]


@app.get("/api/specs/{spec_id}")
def get_spec(spec_id: str, session: Session = Depends(db_session)) -> Dict:
    row = session.get(SpecificationModel, spec_id)
    if not row:
        raise HTTPException(status_code=404, detail="Specification not found")
    if row.content_json.get("document_kind") == "CATALOG":
        return {
            **spec_summary(row),
            "tables": [
                {key: value for key, value in table.items() if key != "spec"}
                for table in row.content_json["tables"]
            ],
            "fields": [],
        }
    if "tables" in row.content_json:
        tables = row.content_json["tables"]
        return {
            **spec_summary(row),
            "tables": [
                {"table_code": table["table_code"], "table_name": table["table_name"], "field_count": len(table["fields"])}
                for table in tables
            ],
            "fields": [
                {**field, "table_code": table["table_code"]}
                for table in tables
                for field in table["fields"]
            ],
        }
    return {**spec_summary(row), "fields": row.content_json["fields"], "terms_text": row.content_json["terms_text"]}


@app.post("/api/specs/{spec_id}/confirm", status_code=201)
def confirm_tables(spec_id: str, request: ConfirmationRequest, session: Session = Depends(db_session)) -> Dict:
    catalog = session.get(SpecificationModel, spec_id)
    if not catalog or catalog.content_json.get("document_kind") != "CATALOG":
        raise HTTPException(status_code=404, detail="Document catalog not found")
    requested = set(request.table_codes)
    entries = {entry["table_code"]: entry for entry in catalog.content_json["tables"]}
    if not requested or any(code not in entries or not entries[code]["eligible"] for code in requested):
        raise HTTPException(status_code=422, detail="Selection contains an unknown or ineligible table")
    selected = [entries[code]["spec"] for code in sorted(requested)]
    content = {
        "document_kind": "CONFIRMED_SNAPSHOT",
        "selection_confirmed": True,
        "version": catalog.version,
        "table_name": f"已确认 {len(selected)} 张表 · {request.approver.strip()}",
        "confirmation": {
            "approver": request.approver.strip(),
            "confirmed_at": datetime.utcnow().isoformat(),
            "table_codes": sorted(requested),
            "source_spec_id": spec_id,
        },
        "tables": selected,
    }
    row = SpecificationModel(
        id=str(uuid.uuid4()),
        version=catalog.version,
        table_code="CONFIRMED_SNAPSHOT",
        source_name=catalog.source_name,
        source_sha256=catalog.source_sha256,
        content_json=content,
    )
    session.add(row)
    session.commit()
    return spec_summary(row)


@app.post("/api/runs", status_code=201)
def create_run(request: RunRequest, session: Session = Depends(db_session)) -> Dict:
    if request.provider != "live":
        raise HTTPException(status_code=422, detail="The production path requires provider=live and programmatic GLM calls")
    try:
        run = start_generic_run(session, request.spec_id, settings)
    except (WorkflowError, RuntimeError, ValueError) as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return run_view(session, run)


@app.get("/api/runs")
def list_runs(session: Session = Depends(db_session)) -> List[Dict]:
    runs = session.query(RunModel).order_by(RunModel.created_at.desc()).limit(50).all()
    return [run_view(session, run, include_events=False) for run in runs]


ACTIVE_RUN_STATUSES = {"RUNNING", "AUDITING", "READY_TO_AUDIT"}


def _history_cleanup_plan(session: Session):
    """每组报表保留最新批次和最近一次通过验证的批次，运行中批次永不清理。"""
    runs = session.query(RunModel).order_by(RunModel.created_at.desc()).all()
    groups: Dict[tuple, List[RunModel]] = {}
    for run in runs:
        key = tuple(sorted(item["table_code"] for item in _run_tables(session, run))) or (run.spec_id,)
        groups.setdefault(key, []).append(run)
    keep = set()
    for group in groups.values():
        ordered = sorted(group, key=lambda item: item.created_at or datetime.min, reverse=True)
        keep.add(ordered[0].id)
        completed = next((item for item in ordered if item.status == "COMPLETED"), None)
        if completed:
            keep.add(completed.id)
        keep.update(item.id for item in ordered if item.status in ACTIVE_RUN_STATUSES)
    deletes = [run for run in runs if run.id not in keep]
    return runs, deletes


@app.get("/api/runs/history/cleanup-preview")
def history_cleanup_preview(session: Session = Depends(db_session)) -> Dict:
    runs, deletes = _history_cleanup_plan(session)
    return {
        "total": len(runs),
        "keep_count": len(runs) - len(deletes),
        "delete_count": len(deletes),
        "runs": [
            {
                "id": run.id,
                "status": run.status,
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "tables": _run_tables(session, run),
            }
            for run in deletes
        ],
    }


def _delete_run_rows(session: Session, run: RunModel):
    result = run.result_json or {}
    oracle_tables = set()
    for key in ("oracle_tables", "oracle_target_tables", "oracle_code_set_tables"):
        oracle_tables.update((result.get(key) or {}).values())
    blob_uris = {row.blob_uri for row in session.query(ArtifactModel).filter(ArtifactModel.run_id == run.id).all()}
    for model in (RuleModel, EventModel, ArtifactModel, GenerationPlanModel, GeneratorDefinitionModel, SchemaDefinitionModel, AuditCaseDefinitionModel):
        session.query(model).filter(model.run_id == run.id).delete(synchronize_session=False)
    session.delete(run)
    return blob_uris, oracle_tables


def _finish_run_deletion(session: Session, blob_uris: set, oracle_tables: set) -> List[str]:
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


@app.post("/api/runs/history/cleanup")
def history_cleanup(session: Session = Depends(db_session)) -> Dict:
    _, deletes = _history_cleanup_plan(session)
    deleted_ids = [run.id for run in deletes]
    blob_uris = set()
    oracle_tables = set()
    for run in deletes:
        run_blobs, run_tables = _delete_run_rows(session, run)
        blob_uris.update(run_blobs)
        oracle_tables.update(run_tables)
    warnings = _finish_run_deletion(session, blob_uris, oracle_tables)
    return {"deleted_count": len(deleted_ids), "deleted_ids": deleted_ids, "sandbox_cleanup": warnings}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, session: Session = Depends(db_session)) -> Dict:
    run = session.get(RunModel, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    events = session.query(EventModel).filter(EventModel.run_id == run.id).order_by(EventModel.seq).all()
    replayed = RunModel(id=run.id, spec_id=run.spec_id, status="NEW", current_node="created")
    fold_events(replayed, events)
    if (replayed.status, replayed.current_node, replayed.result_json) != (run.status, run.current_node, run.result_json):
        raise HTTPException(status_code=500, detail="Event projection does not match the stored run projection")
    return run_view(session, run)


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str, session: Session = Depends(db_session)) -> Dict:
    run = session.get(RunModel, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status in ACTIVE_RUN_STATUSES:
        raise HTTPException(status_code=409, detail="Running validation batches cannot be deleted")
    blob_uris, oracle_tables = _delete_run_rows(session, run)
    warnings = _finish_run_deletion(session, blob_uris, oracle_tables)
    return {"deleted_id": run_id, "sandbox_cleanup": warnings}


def _latest_event_tables(events: List[EventModel], kind: str) -> Dict:
    tables: Dict = {}
    for event in events:
        if event.kind == kind:
            tables = (event.payload_json or {}).get("tables") or {}
    return tables


@app.get("/api/runs/{run_id}/validation-detail")
def validation_detail(run_id: str, session: Session = Depends(db_session)) -> Dict:
    """把 AI 数据定义和 Oracle 审计报告整理成可浏览、可逐规则核对的结果。"""
    run = session.get(RunModel, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    result = run.result_json or {}
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
        item = {
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
        rule_map[(row.table_code, row.rule_key)] = item

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
        rules.append({
            **rule,
            "covered": covered,
            "has_target_case": bool(rule["expected_case_ids"]),
            "effective": covered and not failed,
            "result": "FAILED" if failed else "EFFECTIVE" if covered else "UNCOVERED",
        })
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
        test_data.append({
            "table_code": table_code,
            "table_name": table_names.get(table_code) or table_code,
            "generation_mode": "AI_FIELD_ASSIGNMENT",
            "generator_count": len(generator.get("generators") or []),
            "golden_record": generator.get("golden_record") or {},
            "golden_count": int(plan.get("golden_count") or 1),
            "audit_count": audit_count,
            "generation_plan": plan,
            "audit_preview_url": f"/api/runs/{run.id}/oracle-preview?table_code={table_code}&dataset=audit&limit=100" if (result.get("oracle_tables") or {}).get(table_code) else None,
            "golden_preview_url": f"/api/runs/{run.id}/oracle-preview?table_code={table_code}&dataset=golden&limit=100" if (result.get("oracle_target_tables") or {}).get(table_code) else None,
        })

    active_hashes = set(result.get("active_rule_hashes") or [])
    approved_hashes = {row.rule_hash for row in approved}
    summary = {key: value for key, value in report.items() if key != "cases"}
    if not summary:
        summary = result.get("audit_summary") or {}
    return {
        "run_id": run.id,
        "status": run.status,
        "ai_generated": bool(generators),
        "audit_stale": bool(active_hashes and active_hashes != approved_hashes),
        "summary": summary,
        "rules": rules,
        "cases": cases,
        "test_data": test_data,
        "coverage_statement": report.get("coverage_statement") or "结果仅对本次透明展示的 AI 审计参数负责，不代表监管语义完备。",
    }


@app.post("/api/runs/{run_id}/approvals")
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
            settings,
        )
    except WorkflowError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run_view(session, run)


@app.post("/api/runs/{run_id}/test-confirmation")
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


@app.post("/api/runs/{run_id}/retry")
def retry_run(run_id: str, session: Session = Depends(db_session)) -> Dict:
    run = session.get(RunModel, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "FAILED":
        raise HTTPException(status_code=409, detail="Only failed runs can be retried")
    try:
        run = retry_generic_run(session, run, settings)
    except WorkflowError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run_view(session, run)


@app.get("/api/runs/{run_id}/replay")
def replay_run(run_id: str, session: Session = Depends(db_session)) -> Dict:
    run = session.get(RunModel, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    events = session.query(EventModel).filter(EventModel.run_id == run.id).order_by(EventModel.seq).all()
    replayed = RunModel(id=run.id, spec_id=run.spec_id, status="NEW", current_node="created")
    fold_events(replayed, events)
    return {"status": replayed.status, "current_node": replayed.current_node, "result": replayed.result_json}


@app.get("/api/blobs/{digest}")
def get_blob(digest: str) -> Dict:
    try:
        content = LocalBlobStore(settings.blob_dir).get(f"blob://{digest}").decode("utf-8")
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="Blob not found") from exc
    try:
        return {"digest": digest, "content": content, "json": json.loads(content)}
    except json.JSONDecodeError:
        return {"digest": digest, "content": content, "json": None}


@app.get("/api/business/preview")
def business_preview(table: str = Query(min_length=1), limit: int = Query(default=20, ge=1, le=100)) -> List[Dict]:
    try:
        return ReadOnlyBusinessDatabase(settings).preview(table, limit)
    except BusinessDatabaseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Business database query failed: {type(exc).__name__}") from exc


@app.get("/api/runs/{run_id}/oracle-preview")
def oracle_preview(
    run_id: str,
    table_code: str = Query(min_length=1),
    limit: int = Query(default=20, ge=1, le=100),
    dataset: str = Query(default="audit", pattern="^(audit|golden)$"),
    session: Session = Depends(db_session),
) -> List[Dict]:
    run = session.get(RunModel, run_id)
    result_key = "oracle_target_tables" if dataset == "golden" else "oracle_tables"
    if not run or not run.result_json or not run.result_json.get(result_key):
        raise HTTPException(status_code=404, detail="Oracle run artifacts not found")
    physical_table = run.result_json[result_key].get(table_code)
    if not physical_table:
        raise HTTPException(status_code=404, detail="Table is outside this run")
    try:
        return OracleSandbox(settings.oracle_sandbox_url).preview(physical_table, limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Oracle preview failed: {type(exc).__name__}") from exc
