import uuid

import pytest

from app.catalog import parse_document_catalog
from app.config import Settings, get_settings
from app.generic_domain import bind_generators, bind_rules, bind_schema
from app.generic_oracle import GenericOracleExecutor, compile_ai_schema_oracle_ddl, compile_sandbox_oracle_ddl
from app.generic_ai import LlmTimeoutError
from app.generic_workflow import _call_validated, _create_run, _dedupe_approved_rules, approve_generic_rules, confirm_generic_test_results, start_generic_run
from app.production_service import build_production_bundle
from app.models import EventModel, RuleModel, RunModel, SpecificationModel
from app.rules import RuleValidationError
from app.errors import WorkflowError
from .helpers import make_session


class FixtureDesignLlm:
    def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
        if tool_name == "assign_generation_plan":
            args = {"mandatory_rule_target": 1, "expanded_rule_target": 0, "golden_count": 2, "mutations_per_rule": 1, "rationale": "fixture plan"}
        elif tool_name == "assign_schema":
            fields = payload["fields"]
            args = {
                "fields": [
                    {
                        "field": field["name"],
                        "storage_type": "NUMBER" if field["data_type"] == "NUMBER" else "TEXT",
                        "length": field["length"],
                        "decimal_places": field["decimal_places"] or 0,
                    }
                    for field in fields
                ]
            }
        elif tool_name == "assign_rules" and "tables" in payload:
            args = {"rules": [], "golden_overrides": {}}
        elif tool_name == "assign_rules":
            field = payload["fields"][0]["name"]
            args = {
                "rules": [
                    {
                        "rule_key": "FIXTURE_REQUIRED",
                        "field": field,
                        "operator": "REQUIRED",
                        "params": {},
                        "evidence_kind": "TERM_INFERENCE",
                        "rationale": "fixture candidate",
                    }
                ]
            }
        elif tool_name == "assign_generators":
            args = {
                "generators": [
                    {"field": field["name"], "generator": "FIXED", "params": {"value": 1 if field["data_type"] == "NUMBER" else "V"}}
                    for field in payload["fields"]
                ]
            }
        elif tool_name == "assign_audit_cases":
            field = payload["table"]["fields"][0]["name"]
            rule_key = payload["active_rules"][0]["rule_key"]
            args = {
                "cases": [
                    {"case_id": "GOLDEN", "overrides": {}, "expected_rule_keys": []},
                    {"case_id": "BREAK_REQUIRED", "overrides": {field: ""}, "expected_rule_keys": [rule_key]},
                ]
            }
        else:
            raise AssertionError(tool_name)
        return args, {"provider": "FIXTURE", "model": "fixture", "tool_name": tool_name, "arguments": args}


def test_catalog_discovers_eligibility_without_preselected_tables():
    entries = parse_document_catalog(get_settings().safe_13_spec_path)
    eligible = [entry for entry in entries if entry["eligible"]]
    assert len(entries) > 80
    assert len(eligible) >= 20
    assert any(entry["table_code"] == "SAFE_6_1" for entry in eligible)
    assert all(entry["reason"] for entry in entries)


def test_mixed_case_xml_and_dictionary_fields_are_supported():
    entries = parse_document_catalog(get_settings().safe_13_spec_path)
    account_balance = next(entry for entry in entries if entry["table_code"] == "SAFE_6_4_2")
    assert account_balance["eligible"] is True
    assert [field["name"] for field in account_balance["spec"]["fields"]] == [
        "ACTIONTYPE", "ACTIONDESC", "BRANCH_CODE", "accountNO", "DEAL_DATE", "CURRENCY_code", "CREDIT", "DEBIT", "BALANCE", "REMARK"
    ]
    account_open = next(entry for entry in entries if entry["table_code"] == "SAFE_6_4_1")
    assert account_open["eligible"] is True
    assert len(account_open["spec"]["fields"]) == 17


def test_non_xml_long_field_names_use_known_english_names():
    entries = parse_document_catalog(get_settings().safe_13_spec_path)
    non_xml = next(entry for entry in entries if entry["table_code"] == "SAFE_6_8_1_1")
    field_names = [field["name"] for field in non_xml["spec"]["fields"]]
    assert "SETTLEBANKCODE" in field_names
    assert not any(name.startswith("FIELD_") for name in field_names)


def test_generic_binding_enforces_full_coverage_and_source(organization_spec):
    schema = bind_schema(
        organization_spec,
        [
            {"field": field.name, "storage_type": "NUMBER" if field.data_type == "NUMBER" else "TEXT", "length": field.length, "decimal_places": 0}
            for field in organization_spec.fields
        ],
    )
    assert len(schema) == 33
    rules = bind_rules(
        organization_spec,
        [{"rule_key": "AI_TEST", "field": "POSTCODE", "operator": "REGEXP", "params": {"pattern": "^[0-9]{6}$"}, "evidence_kind": "TERM_INFERENCE", "rationale": "test"}],
    )
    assert rules[0].source == "AI_EXPANDED"
    try:
        bind_generators(organization_spec, [])
    except RuleValidationError:
        pass
    else:
        raise AssertionError("Incomplete generator assignment was accepted")


def test_generic_run_records_ai_tool_calls_before_human_gate(organization_spec, monkeypatch, tmp_path):
    session = make_session()
    spec_id = str(uuid.uuid4())
    session.add(
        SpecificationModel(
            id=spec_id,
            version="SAFE_1.3",
            table_code="CONFIRMED_SNAPSHOT",
            source_name=organization_spec.source_name,
            source_sha256=organization_spec.source_sha256,
            content_json={"table_name": "confirmed", "selection_confirmed": True, "tables": [organization_spec.model_dump()]},
        )
    )
    session.commit()
    def prepare(self, run_id, attempt, specs, schemas, code_sets=None, rules=None):
        physical = {spec.table_code: f"STAGING_{attempt}" for spec in specs}
        return {"attempt": attempt, "physical_tables": physical, "target_tables": {code: f"TARGET_{attempt}" for code in physical}, "code_set_tables": {}, "code_set_versions": {}, "code_set_record_counts": {}, "empty_run": {"all_passed": True, "rule_count": len(rules or []), "statement_count": len(rules or [])}}
    monkeypatch.setattr(GenericOracleExecutor, "prepare_assets", prepare)
    run = start_generic_run(session, spec_id, Settings(oracle_sandbox_url=None, blob_dir=str(tmp_path)), FixtureDesignLlm())
    assert run.status == "WAITING_APPROVAL"
    events = session.query(EventModel).filter(EventModel.run_id == run.id).order_by(EventModel.seq).all()
    kinds = [event.kind for event in events]
    assert kinds.count("AI_TOOL_ASSIGNED") == 2
    assert "SCHEMA_ASSIGNED" in kinds
    assert "AUDIT_STARTED" not in kinds


def test_target_ddl_stays_narrow_while_staging_accepts_mutations(organization_spec):
    schema = bind_schema(
        organization_spec,
        [
            {"field": field.name, "storage_type": "NUMBER" if field.data_type == "NUMBER" else "TEXT", "length": field.length, "decimal_places": 0}
            for field in organization_spec.fields
        ],
    )
    target = compile_ai_schema_oracle_ddl("TARGET_TABLE", schema)
    staging = compile_sandbox_oracle_ddl("STAGING_TABLE", organization_spec)
    assert '"CUSTNAME" VARCHAR2(128 CHAR)' in target
    assert '"CUSTNAME" VARCHAR2(4000 CHAR)' in staging


def test_rule_barrier_supersedes_semantic_duplicates():
    session = make_session()
    run = RunModel(id=str(uuid.uuid4()), spec_id=str(uuid.uuid4()), status="RUNNING", current_node="rule_binder")
    session.add(run)
    session.flush()
    for index, key in enumerate(["REQUIRED_A", "REQUIRED_B"]):
        session.add(
            RuleModel(
                row_id=str(uuid.uuid4()),
                run_id=run.id,
                spec_id=run.spec_id,
                table_code="TEST",
                rule_key=key,
                rule_hash=f"{index + 1:064d}",
                source="DOC_EXPLICIT",
                status="APPROVED",
                definition_json={"field": "AMOUNT", "operator": "REQUIRED", "params": {}},
            )
        )
    session.flush()
    assert _dedupe_approved_rules(session, run) == 1
    assert session.query(RuleModel).filter_by(run_id=run.id, status="APPROVED").count() == 1
    assert session.query(RuleModel).filter_by(run_id=run.id, status="SUPERSEDED").count() == 1


def test_assets_precede_rule_approval_and_export_follows_test_confirmation(organization_spec, monkeypatch, tmp_path):
    session = make_session()
    spec_id = str(uuid.uuid4())
    session.add(
        SpecificationModel(
            id=spec_id,
            version="SAFE_1.3",
            table_code="CONFIRMED_SNAPSHOT",
            source_name=organization_spec.source_name,
            source_sha256=organization_spec.source_sha256,
            content_json={"table_name": "confirmed", "selection_confirmed": True, "tables": [organization_spec.model_dump()]},
        )
    )
    session.commit()
    calls = []

    def prepare(self, run_id, attempt, specs, schemas, code_sets=None, rules=None):
        calls.append("PREPARE")
        physical = {spec.table_code: f"STAGING_{attempt}" for spec in specs}
        return {
            "attempt": attempt,
            "physical_tables": physical,
            "target_tables": {code: f"TARGET_{attempt}" for code in physical},
            "code_set_tables": {},
            "code_set_versions": {},
            "code_set_record_counts": {},
            "empty_run": {"all_passed": True, "rule_count": len(rules or []), "statement_count": len(rules or [])},
        }

    def execute(self, run_id, attempt, specs, schemas, golden_records, audit_cases, rules, *args, **kwargs):
        calls.append("EXECUTE")
        assert kwargs["prepared_assets"]["attempt"] == attempt
        return {
            "all_passed": True,
            "backend": "ORACLE",
            "table_count": len(specs),
            "case_count": 2,
            "active_rule_count": len(rules),
            "false_positive_count": 0,
            "false_negative_count": 0,
            "record_counts": {},
            "physical_tables": kwargs["prepared_assets"]["physical_tables"],
            "target_tables": kwargs["prepared_assets"]["target_tables"],
            "code_set_tables": {},
            "code_set_versions": {},
            "code_set_record_counts": {},
            "cases": [],
        }

    monkeypatch.setattr(GenericOracleExecutor, "prepare_assets", prepare)
    monkeypatch.setattr(GenericOracleExecutor, "execute", execute)
    settings = Settings(blob_dir=str(tmp_path), oracle_sandbox_url="sqlite://")
    budget = {"max_rules_per_table": 10, "max_cases_per_table": 20, "max_retries": 3, "max_llm_concurrency": 4}

    run = start_generic_run(session, spec_id, settings, FixtureDesignLlm(), generation_budget=budget)

    assert run.status == "WAITING_APPROVAL"
    assert calls == ["PREPARE"]
    assert run.result_json["oracle_target_tables"][organization_spec.table_code] == "TARGET_1"
    pending = [rule for rule in session.query(RuleModel).filter(RuleModel.run_id == run.id).all() if rule.status == "PENDING"]
    run = approve_generic_rules(
        session, run, "规则审核员", {rule.rule_hash: "APPROVED" for rule in pending}, settings, FixtureDesignLlm()
    )
    assert calls == ["PREPARE", "EXECUTE"], run.result_json
    assert run.status == "WAITING_TEST_APPROVAL"
    with pytest.raises(Exception):
        build_production_bundle(session, run, str(tmp_path))

    run = confirm_generic_test_results(session, run, "测试审核员")

    assert run.status == "COMPLETED"
    assert build_production_bundle(session, run, str(tmp_path))[1]
    kinds = [event.kind for event in session.query(EventModel).filter(EventModel.run_id == run.id).order_by(EventModel.seq).all()]
    assert kinds.index("ORACLE_ASSETS_PREPARED") < kinds.index("HUMAN_DECISIONS_RECORDED") < kinds.index("AUDIT_STARTED") < kinds.index("TEST_CONFIRMATION_REQUIRED") < kinds.index("TEST_RESULTS_CONFIRMED")


def test_call_validated_retries_once_after_timeout():
    class FlakyLlm:
        def __init__(self):
            self.calls = 0

        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise LlmTimeoutError("ReadTimeout after 1s")
            return {"ok": True}, {"provider": "test"}

    llm = FlakyLlm()
    result, trace = _call_validated(llm, "assign_rules", "instr", {}, lambda args: args)

    assert llm.calls == 2
    assert result == {"ok": True}
    assert trace["retried_after_timeout"] is True
    assert "previous_rejection" not in trace


def test_call_validated_fails_after_two_timeouts():
    class AlwaysTimeout:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            raise LlmTimeoutError("ReadTimeout")

    with pytest.raises(WorkflowError, match="timed out after retry"):
        _call_validated(AlwaysTimeout(), "assign_rules", "instr", {}, lambda args: args)


def test_call_validated_timeout_error_carries_structured_context():
    class AlwaysTimeout:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            raise LlmTimeoutError("ReadTimeout")

    payload = {"table_code": "SAFE_6_1_ORGANIZATION", "fields": [{"name": "POSTCODE"}]}
    with pytest.raises(WorkflowError) as error:
        _call_validated(AlwaysTimeout(), "assign_rules", "instr", payload, lambda args: args)

    ctx = error.value.context
    assert ctx["error_kind"] == "timeout"
    assert ctx["tool_name"] == "assign_rules"
    assert ctx["stage"] == "rule_binder"
    assert ctx["table_code"] == "SAFE_6_1_ORGANIZATION"
    assert ctx["field"] == "POSTCODE"
    assert ctx["retried"] is True


def test_call_validated_validation_error_carries_structured_context():
    class BadLlm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            return {"rules": []}, {"provider": "test"}

    def reject(args):
        raise RuleValidationError("nope")

    payload = {"table_code": "SAFE_6_1_ORGANIZATION", "fields": [{"name": "POSTCODE"}]}
    with pytest.raises(WorkflowError) as error:
        _call_validated(BadLlm(), "assign_rules", "instr", payload, reject)

    ctx = error.value.context
    assert ctx["error_kind"] == "validation"
    assert ctx["stage"] == "rule_binder"
    assert ctx["field"] == "POSTCODE"


def test_create_run_marks_run_running(organization_spec):
    session = make_session()
    spec_id = str(uuid.uuid4())
    session.add(
        SpecificationModel(
            id=spec_id,
            version="SAFE_1.3",
            table_code="CONFIRMED_SNAPSHOT",
            source_name=organization_spec.source_name,
            source_sha256=organization_spec.source_sha256,
            content_json={"table_name": "confirmed", "selection_confirmed": True, "tables": [organization_spec.model_dump()]},
        )
    )
    session.commit()

    run = _create_run(session, spec_id, Settings())
    assert run.status == "RUNNING"
    assert run.current_node == "bind_rules"
