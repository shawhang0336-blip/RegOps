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

from .api_v2 import code_sets as code_sets_v2
from .api_v2 import documents as documents_v2
from .api_v2 import generation as generation_v2
from .api_v2 import metadata as metadata_v2
from .api_v2 import settings as settings_v2
from .blobs import LocalBlobStore
from .business import BusinessDatabaseError, ReadOnlyBusinessDatabase
from .catalog import parse_document_catalog
from .config import get_settings
from .db import SessionLocal, init_db
from .events import append_event, fold_events
from .generic_workflow import approve_generic_rules, confirm_generic_test_results, retry_generic_run, start_generic_run
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
    approver: str = Field(min_length=1, max_length=80)


class TestConfirmationRequest(BaseModel):
    approver: str = Field(min_length=1, max_length=80)


class Decision(BaseModel):
    rule_hash: str
    decision: str


class ApprovalRequest(BaseModel):
    approver: str = Field(min_length=1, max_length=80)
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


def run_view(session: Session, run: RunModel, include_events: bool = True) -> Dict:
    rules = session.query(RuleModel).filter(RuleModel.run_id == run.id).all()
    events = session.query(EventModel).filter(EventModel.run_id == run.id).order_by(EventModel.seq).all()
    result = {
        "id": run.id,
        "spec_id": run.spec_id,
        "status": run.status,
        "current_node": run.current_node,
        "attempt": run.attempt,
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
    session: Session = Depends(db_session),
) -> List[Dict]:
    run = session.get(RunModel, run_id)
    if not run or not run.result_json or not run.result_json.get("oracle_tables"):
        raise HTTPException(status_code=404, detail="Oracle run artifacts not found")
    physical_table = run.result_json["oracle_tables"].get(table_code)
    if not physical_table:
        raise HTTPException(status_code=404, detail="Table is outside this run")
    try:
        return OracleSandbox(settings.oracle_sandbox_url).preview(physical_table, limit)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Oracle preview failed: {type(exc).__name__}") from exc
