import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from .audit import run_sqlite_bundle_audit
from .blobs import LocalBlobStore
from .compiler import compile_mysql_ddl, compile_rule_query
from .config import Settings
from .domain import RuleDefinition, SpecDocument, canonical_json
from .events import append_event
from .llm import build_llm
from .models import RuleModel, RunModel, SpecificationModel
from .oracle_sandbox import OracleSandbox
from .rules import build_cross_table_rules, build_explicit_rules


class WorkflowError(RuntimeError):
    pass


@dataclass(frozen=True)
class Send:
    target: str
    value: SpecDocument


def _specs(session: Session, spec_id: str) -> List[SpecDocument]:
    row = session.get(SpecificationModel, spec_id)
    if not row:
        raise WorkflowError("Specification not found")
    if "tables" in row.content_json:
        return [SpecDocument.model_validate(item) for item in row.content_json["tables"]]
    return [SpecDocument.model_validate(row.content_json)]


def _upsert_rule(session: Session, run: RunModel, rule: RuleDefinition, status: str) -> None:
    existing = (
        session.query(RuleModel)
        .filter(RuleModel.run_id == run.id, RuleModel.rule_hash == rule.rule_hash)
        .one_or_none()
    )
    if existing:
        if existing.status == "REJECTED" and status == "PENDING":
            existing.status = "PENDING"
        return
    session.add(
        RuleModel(
            row_id=str(uuid.uuid4()),
            run_id=run.id,
            spec_id=run.spec_id,
            table_code=rule.table_code,
            rule_key=rule.rule_key,
            rule_hash=rule.rule_hash,
            source=rule.source,
            status=status,
            definition_json=rule.model_dump(),
        )
    )
    session.flush()


def _pending_rules(session: Session, run_id: str) -> List[RuleModel]:
    return session.query(RuleModel).filter(RuleModel.run_id == run_id, RuleModel.status == "PENDING").all()


def _table_binding(spec: SpecDocument, provider: str, settings: Settings, feedback: Optional[str]):
    explicit = build_explicit_rules(spec)
    candidates = build_llm(settings, provider).propose(spec, feedback)
    return spec.table_code, explicit, candidates


def _bind_rules_fanout(
    session: Session,
    run: RunModel,
    provider: str,
    settings: Settings,
    feedback: Optional[str],
) -> int:
    specs = _specs(session, run.spec_id)
    directives = [Send("bind_table_rules", spec) for spec in specs]
    append_event(
        session,
        run,
        "BSP_FANOUT",
        "parse_spec",
        {"target": "bind_table_rules", "table_codes": [item.value.table_code for item in directives]},
    )
    with ThreadPoolExecutor(max_workers=min(8, len(directives))) as executor:
        futures = [executor.submit(_table_binding, item.value, provider, settings, feedback) for item in directives]
        wave_results = [future.result() for future in futures]
    for table_code, explicit, candidates in wave_results:
        for rule in explicit:
            _upsert_rule(session, run, rule, "APPROVED")
        for candidate in candidates:
            _upsert_rule(session, run, candidate, "PENDING")
    cross_rules = build_cross_table_rules(specs) if len(specs) > 1 else []
    for rule in cross_rules:
        _upsert_rule(session, run, rule, "APPROVED")
    append_event(
        session,
        run,
        "BSP_BARRIER_JOIN",
        "cross_table_rules",
        {
            "table_results": [
                {
                    "table_code": table_code,
                    "explicit_count": len(explicit),
                    "candidate_count": len(candidates),
                }
                for table_code, explicit, candidates in wave_results
            ],
            "cross_rule_count": len(cross_rules),
        },
    )
    return len(_pending_rules(session, run.id))


def start_run(session: Session, spec_id: str, provider: str, settings: Settings) -> RunModel:
    _specs(session, spec_id)
    run = RunModel(id=str(uuid.uuid4()), spec_id=spec_id, status="NEW", current_node="created")
    session.add(run)
    session.flush()
    append_event(session, run, "RUN_CREATED", "parse_spec", {"spec_id": spec_id, "provider": provider})
    pending_count = _bind_rules_fanout(session, run, provider, settings, None)
    bound_rules = session.query(RuleModel).filter(RuleModel.run_id == run.id).all()
    append_event(
        session,
        run,
        "RULES_BOUND",
        "bind_rules",
        {
            "pending_count": pending_count,
            "binding_policy": "AI candidates are always AI_EXPANDED",
            "rule_set": [
                {
                    "rule_hash": rule.rule_hash,
                    "rule_key": rule.rule_key,
                    "table_code": rule.table_code,
                    "source": rule.source,
                    "status": rule.status,
                }
                for rule in sorted(bound_rules, key=lambda item: (item.table_code, item.rule_key))
            ],
        },
    )
    if pending_count:
        append_event(
            session,
            run,
            "APPROVAL_REQUIRED",
            "human_gate",
            {"pending_rule_hashes": [rule.rule_hash for rule in _pending_rules(session, run.id)]},
        )
        session.commit()
        return run
    session.commit()
    return execute_audit(session, run, settings, provider)


def record_decisions(
    session: Session,
    run: RunModel,
    approver: str,
    decisions: Dict[str, str],
    settings: Settings,
    provider: str,
) -> RunModel:
    if run.status != "WAITING_APPROVAL":
        raise WorkflowError("Run is not waiting for approval")
    pending = {rule.rule_hash: rule for rule in _pending_rules(session, run.id)}
    if not decisions or any(rule_hash not in pending for rule_hash in decisions):
        raise WorkflowError("Decisions must target currently pending rules")
    normalized = []
    for rule_hash, decision in decisions.items():
        if decision not in {"APPROVED", "REJECTED"}:
            raise WorkflowError("Decision must be APPROVED or REJECTED")
        pending[rule_hash].status = decision
        normalized.append({"rule_hash": rule_hash, "decision": decision})
    session.flush()
    all_decided = not _pending_rules(session, run.id)
    append_event(
        session,
        run,
        "HUMAN_DECISIONS_RECORDED",
        "human_gate",
        {"approver": approver, "decisions": normalized, "all_decided": all_decided},
    )
    session.commit()
    if all_decided:
        return execute_audit(session, run, settings, provider)
    return run


def execute_audit(session: Session, run: RunModel, settings: Settings, provider: str) -> RunModel:
    specs = _specs(session, run.spec_id)
    rows = session.query(RuleModel).filter(RuleModel.run_id == run.id, RuleModel.status == "APPROVED").all()
    rules = [RuleDefinition.model_validate(row.definition_json) for row in rows]
    attempt = run.attempt + 1
    append_event(session, run, "AUDIT_STARTED", "build_audit", {"attempt": attempt})
    if settings.oracle_sandbox_url:
        report = OracleSandbox(settings.oracle_sandbox_url).run_audit(run.id, attempt, specs, rules)
        dialect = "oracle"
    else:
        report = run_sqlite_bundle_audit(specs, rules)
        report["backend"] = "SQLITE"
        dialect = "sqlite"
    blobs = LocalBlobStore(settings.blob_dir)
    ddl_uri = blobs.put("\n\n".join(compile_mysql_ddl(spec) for spec in specs).encode("utf-8"))
    physical_tables = report.get("physical_tables")
    compiled = []
    for rule in rules:
        sql, params = compile_rule_query(rule, dialect, physical_tables)
        compiled.append({"table_code": rule.table_code, "rule_key": rule.rule_key, "sql": sql, "params": params})
    sql_uri = blobs.put(canonical_json({"dialect": dialect, "queries": compiled}).encode("utf-8"))
    report_uri = blobs.put(canonical_json(report).encode("utf-8"))
    result = {
        "ddl_blob": ddl_uri,
        "validation_sql_blob": sql_uri,
        "audit_report_blob": report_uri,
        "active_rule_hashes": sorted(rule.rule_hash for rule in rules),
        "oracle_tables": physical_tables,
        "audit_summary": {
            key: value for key, value in report.items() if key not in {"cases", "physical_tables"}
        },
    }
    if report["all_passed"]:
        append_event(session, run, "AUDIT_COMPLETED", "sandbox_audit", {"result": result})
        session.commit()
        return run

    explicit_failure = any(
        case["missing_rules"] or case["unexpected_rules"]
        for case in report["cases"]
        if any(not rule_key.endswith("_AI") for rule_key in case["missing_rules"] + case["unexpected_rules"])
    )
    if explicit_failure or attempt >= 2 or provider == "scripted":
        append_event(session, run, "AUDIT_REJECTED", "sandbox_audit", {"result": result, "node": "sandbox_audit"})
        session.commit()
        return run

    append_event(
        session,
        run,
        "GO_REDIRECT",
        "sandbox_audit",
        {"target": "bind_rules", "reason": "AI-expanded rules failed independent audit"},
    )
    for row in rows:
        if row.source == "AI_EXPANDED":
            row.status = "SUPERSEDED"
    pending_count = _bind_rules_fanout(session, run, provider, settings, canonical_json(report["cases"]))
    if not pending_count:
        append_event(
            session,
            run,
            "RUN_FAILED",
            "bind_rules",
            {"result": result, "node": "bind_rules", "reason": "Model produced no revised candidates"},
        )
    else:
        append_event(session, run, "APPROVAL_REQUIRED", "human_gate", {"pending_count": pending_count})
    session.commit()
    return run
