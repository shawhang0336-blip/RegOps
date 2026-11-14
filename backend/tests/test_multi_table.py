import uuid

from app.audit import run_sqlite_bundle_audit
from app.compiler import compile_rule_query
from app.config import Settings
from app.db import Base
from app.events import fold_events
from app.llm import ScriptedLlm
from app.models import EventModel, RunModel, SpecificationModel
from app.rules import build_cross_table_rules, build_explicit_rules
from app.workflow import record_decisions, start_run
from .test_workflow import make_session


def all_rules(specs):
    rules = []
    for spec in specs:
        rules.extend(build_explicit_rules(spec))
        rules.extend(ScriptedLlm().propose(spec))
    rules.extend(build_cross_table_rules(specs))
    return rules


def test_three_table_fanout_catalog_and_independent_audit(chapter6_specs):
    assert [len(spec.fields) for spec in chapter6_specs] == [33, 23, 17]
    rules = all_rules(chapter6_specs)
    assert len(rules) == 32
    report = run_sqlite_bundle_audit(chapter6_specs, rules)
    assert report["case_count"] == 33
    assert report["all_passed"] is True


def test_cross_table_rule_compiles_real_not_exists(chapter6_specs):
    rule = build_cross_table_rules(chapter6_specs)[0]
    sql, _ = compile_rule_query(rule, "oracle")
    assert "NOT EXISTS" in sql
    assert '"SAFE_6_2_1_INCOME_BASE"' in sql


def test_workflow_emits_fanout_barrier_and_replays(chapter6_specs, tmp_path):
    session = make_session()
    spec_id = str(uuid.uuid4())
    session.add(
        SpecificationModel(
            id=spec_id,
            version="SAFE_1.3",
            table_code="SAFE_CH6_MULTI_TABLE",
            source_name=chapter6_specs[0].source_name,
            source_sha256=chapter6_specs[0].source_sha256,
            content_json={
                "version": "SAFE_1.3",
                "table_name": "第六章多表规则装配切片",
                "tables": [spec.model_dump() for spec in chapter6_specs],
            },
        )
    )
    session.commit()
    settings = Settings(blob_dir=str(tmp_path / "blobs"), oracle_sandbox_url=None)
    run = start_run(session, spec_id, "scripted", settings)
    assert run.status == "WAITING_APPROVAL"
    assert len(run_view_rules(session, run.id)) == 32
    pending = [rule for rule in run_view_rules(session, run.id) if rule.status == "PENDING"]
    run = record_decisions(session, run, "多表测试员", {pending[0].rule_hash: "APPROVED"}, settings, "scripted")
    assert run.status == "COMPLETED"
    assert run.result_json["audit_summary"]["case_count"] == 33
    assert run.result_json["audit_summary"]["table_count"] == 3
    events = session.query(EventModel).filter(EventModel.run_id == run.id).order_by(EventModel.seq).all()
    kinds = [event.kind for event in events]
    assert kinds.index("BSP_FANOUT") < kinds.index("BSP_BARRIER_JOIN") < kinds.index("RULES_BOUND")
    fanout = next(event for event in events if event.kind == "BSP_FANOUT")
    assert len(fanout.payload_json["table_codes"]) == 3
    replayed = RunModel(id=run.id, spec_id=spec_id, status="NEW", current_node="created")
    fold_events(replayed, events)
    assert replayed.result_json == run.result_json


def run_view_rules(session, run_id):
    from app.models import RuleModel

    return session.query(RuleModel).filter(RuleModel.run_id == run_id).all()
