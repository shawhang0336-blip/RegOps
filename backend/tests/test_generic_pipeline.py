import uuid

from app.catalog import parse_document_catalog
from app.config import Settings, get_settings
from app.generic_domain import bind_generators, bind_rules, bind_schema
from app.generic_oracle import compile_ai_schema_oracle_ddl, compile_sandbox_oracle_ddl
from app.generic_workflow import start_generic_run
from app.models import EventModel, SpecificationModel
from app.rules import RuleValidationError
from .test_workflow import make_session


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


def test_generic_run_records_ai_tool_calls_before_human_gate(organization_spec):
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
    run = start_generic_run(session, spec_id, Settings(oracle_sandbox_url=None), FixtureDesignLlm())
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
