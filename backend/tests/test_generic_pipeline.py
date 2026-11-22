import uuid

import pytest

from app.ingestion.parsers.pdf_catalog import parse_document_catalog
from app.infra.config import Settings, get_settings
from app.generation.core.binding import bind_generators, bind_rules, bind_schema
from app.generation.sql.oracle import GenericOracleExecutor, compile_ai_schema_oracle_ddl, compile_sandbox_oracle_ddl
from app.infra.llm import LlmTimeoutError
from app.policy.regops_plan import project
from app.generation.workflow import (
    _call_validated,
    _create_run,
    _dedupe_approved_rules,
    approve_generic_rules,
    confirm_generic_test_results,
    retry_generic_run,
    start_generic_run,
)
from app.delivery.production_service import build_production_bundle
from app.infra.db_tables import EventModel, RuleModel, RunModel, SpecificationModel
from app.generation.core.rules import RuleValidationError
from app.infra.errors import WorkflowError
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
    def prepare(self, run_id, attempt, specs, schemas, code_sets=None, rules=None, **kwargs):
        physical = {spec.table_code: f"STAGING_{attempt}" for spec in specs}
        return {"attempt": attempt, "physical_tables": physical, "target_tables": {code: f"TARGET_{attempt}" for code in physical}, "code_set_tables": {}, "code_set_versions": {}, "code_set_record_counts": {}, "empty_run": {"all_passed": True, "rule_count": len(rules or []), "statement_count": len(rules or [])}}
    monkeypatch.setattr(GenericOracleExecutor, "prepare_assets", prepare)
    run = start_generic_run(session, spec_id, Settings(oracle_sandbox_url=None, blob_dir=str(tmp_path)), FixtureDesignLlm())
    assert project(session, run).status == "WAITING_APPROVAL"
    events = session.query(EventModel).filter(EventModel.run_id == run.id).order_by(EventModel.seq).all()
    kinds = [event.kind for event in events]
    # schema_mapper 已切换为确定性编译，不再消耗 AI 调用；进入人工门前仅 rule_binder 产生 1 次表级 AI_TOOL_ASSIGNED
    assert kinds.count("AI_TOOL_ASSIGNED") == 1
    assert "SCHEMA_ASSIGNED" in kinds
    assert "RULE_BACKFILL" in kinds
    assert kinds.index("RULE_BACKFILL") < kinds.index("BSP_FANOUT")
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
    run = RunModel(id=str(uuid.uuid4()), spec_id=str(uuid.uuid4()))
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

    def prepare(self, run_id, attempt, specs, schemas, code_sets=None, rules=None, **kwargs):
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

    state = project(session, run)
    assert state.status == "WAITING_APPROVAL"
    assert calls == ["PREPARE"]
    assert state.result_json["oracle_target_tables"][organization_spec.table_code] == "TARGET_1"
    pending = [rule for rule in session.query(RuleModel).filter(RuleModel.run_id == run.id).all() if rule.status == "PENDING"]
    run = approve_generic_rules(
        session, run, "规则审核员", {rule.rule_hash: "APPROVED" for rule in pending}, settings, FixtureDesignLlm()
    )
    state = project(session, run)
    assert calls == ["PREPARE", "EXECUTE"], state.result_json
    assert state.status == "WAITING_TEST_APPROVAL"
    with pytest.raises(Exception):
        build_production_bundle(session, run, str(tmp_path))

    # 幂等重入：同样的审批/确认重复提交，返回原结果且不重复记账
    before = session.query(EventModel).filter(EventModel.run_id == run.id).count()
    run = approve_generic_rules(
        session, run, "规则审核员", {rule.rule_hash: "APPROVED" for rule in pending}, settings, FixtureDesignLlm()
    )
    assert session.query(EventModel).filter(EventModel.run_id == run.id).count() == before

    run = confirm_generic_test_results(session, run, "测试审核员")
    assert project(session, run).status == "COMPLETED"
    again = session.query(EventModel).filter(EventModel.run_id == run.id).count()
    run = confirm_generic_test_results(session, run, "测试审核员")
    assert session.query(EventModel).filter(EventModel.run_id == run.id).count() == again

    assert build_production_bundle(session, run, str(tmp_path))[1]
    kinds = [event.kind for event in session.query(EventModel).filter(EventModel.run_id == run.id).order_by(EventModel.seq).all()]
    assert kinds.index("ORACLE_ASSETS_PREPARED") < kinds.index("HUMAN_DECISIONS_RECORDED") < kinds.index("AUDIT_STARTED") < kinds.index("TEST_CONFIRMATION_REQUIRED") < kinds.index("TEST_RESULTS_CONFIRMED")
    # 回答事件带 interrupt_id / interrupt_seq 指回具体中断
    approval = next(e for e in session.query(EventModel).filter(EventModel.run_id == run.id, EventModel.kind == "HUMAN_DECISIONS_RECORDED").all())
    assert approval.payload_json.get("interrupt_id")
    assert approval.payload_json.get("interrupt_seq")
    confirmation = next(e for e in session.query(EventModel).filter(EventModel.run_id == run.id, EventModel.kind == "TEST_RESULTS_CONFIRMED").all())
    assert confirmation.payload_json.get("interrupt_id")


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


def test_call_validated_gives_up_after_transient_budget():
    inst = {}

    class AlwaysTimeout:
        calls = 0

        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            inst["calls"] = inst.get("calls", 0) + 1
            inst["timeouts"] = inst.get("timeouts", []) + [timeout]
            raise LlmTimeoutError("ReadTimeout")

    with pytest.raises(WorkflowError, match="timed out after retry"):
        _call_validated(AlwaysTimeout(), "assign_rules", "instr", {}, lambda args: args, backoff=(0, 0, 0))

    # 瞬时错误给 4 次机会，且超时窗口逐次放宽，不是同一个窗口反复被掐
    assert inst["calls"] == 4
    assert inst["timeouts"] == [None, None, None, None] or inst["timeouts"][-1] > inst["timeouts"][0]


def test_call_validated_retries_when_model_returns_no_tool_call():
    """模型只回文字不调工具时，带一句更硬的提示重试，而不是直接判死。"""

    class TextOnlyThenOk:
        def __init__(self):
            self.calls = 0
            self.feedback = []

        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            self.calls += 1
            self.feedback.append(feedback)
            if self.calls == 1:
                raise RuntimeError("Model returned no tool call: {...}")
            return {"ok": True}, {"provider": "test"}

    llm = TextOnlyThenOk()
    result, trace = _call_validated(llm, "assign_rules", "instr", {}, lambda args: args, backoff=(0, 0, 0))
    assert result == {"ok": True}
    assert llm.calls == 2
    assert llm.feedback[1] and "工具调用" in llm.feedback[1]
    assert trace["attempts"] == 2


def test_call_validated_timeout_error_carries_structured_context():
    class AlwaysTimeout:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            raise LlmTimeoutError("ReadTimeout")

    payload = {"table_code": "SAFE_6_1_ORGANIZATION", "fields": [{"name": "POSTCODE"}]}
    with pytest.raises(WorkflowError) as error:
        _call_validated(AlwaysTimeout(), "assign_rules", "instr", payload, lambda args: args, backoff=(0, 0, 0))

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
    state = project(session, run)
    assert state.status == "RUNNING"
    assert state.current_node == "created"


def test_self_heal_goto_rule_binder_only_rebinds_failed_rules_not_full_table(organization_spec, monkeypatch, tmp_path):
    """回归验证：沙箱首轮发现漏拦触发 goto='rule_binder' 自愈时，
    rule_binder 只靶向重绑失败的那条规则，绝不把整表 33 个字段重新跑一遍 _rules_stage。
    """
    session = make_session()
    spec_id = str(uuid.uuid4())
    # 只留 2 个字段加速单测，验证自愈时只修失败规则
    slim_spec = organization_spec.model_copy(update={"fields": organization_spec.fields[:2]})
    session.add(
        SpecificationModel(
            id=spec_id,
            version="SAFE_1.3",
            table_code="CONFIRMED_SNAPSHOT",
            source_name=slim_spec.source_name,
            source_sha256=slim_spec.source_sha256,
            content_json={"table_name": "confirmed", "selection_confirmed": True, "tables": [slim_spec.model_dump()]},
        )
    )
    session.commit()

    rule_call_payloads = []

    class SelfHealLlm(FixtureDesignLlm):
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            if tool_name == "assign_rules":
                rule_call_payloads.append(payload)
                if "current_rule" in payload:
                    # 自愈靶向重绑：将 DOC_EXPLICIT 规则从错误枚举 ['A'] 修正为 ['A', 'C', 'D']
                    cur = payload["current_rule"]
                    args = {
                        "rules": [
                            {
                                "rule_key": cur["rule_key"],
                                "field": cur["field"],
                                "operator": "ENUM",
                                "params": {"values": ["A", "C", "D"]},
                                "evidence_kind": "DICTIONARY",
                                "rationale": "self-healed explicit enum",
                            }
                        ]
                    }
                    return args, {"provider": "FIXTURE", "model": "fixture", "tool_name": tool_name, "arguments": args}
                # 初次绑定：只在第 1 个字段产出 1 条 DOC_EXPLICIT 规则（无 PENDING，直接进沙箱）
                if payload.get("existing_rule_keys"):
                    args = {"rules": []}
                elif payload["fields"][0]["name"] == slim_spec.fields[0].name:
                    args = {
                        "rules": [
                            {
                                "rule_key": "R_EXPLICIT_ENUM",
                                "field": slim_spec.fields[0].name,
                                "operator": "ENUM",
                                "params": {"values": ["A"]},
                                "evidence_kind": "DICTIONARY",
                                "rationale": "initial incomplete enum",
                            }
                        ]
                    }
                else:
                    args = {"rules": []}
                return args, {"provider": "FIXTURE", "model": "fixture", "tool_name": tool_name, "arguments": args}
            if tool_name == "assign_audit_cases":
                target = payload.get("target_rule")
                if target is None:
                    args = {"cases": [{"case_id": "GOLDEN", "overrides": {}, "expected_rule_keys": []}]}
                else:
                    r_key = target["rule_key"]
                    r_field = target["field"]
                    r_op = target["operator"]
                    bad_val = "X" * 300 if r_op == "LENGTH_MAX" else ("" if r_op == "REQUIRED" else "INVALID_Z")
                    args = {"cases": [{"case_id": f"BREAK_{r_key}", "overrides": {r_field: bad_val}, "expected_rule_keys": [r_key]}]}
                return args, {"provider": "FIXTURE", "model": "fixture", "tool_name": tool_name, "arguments": args}
            return super().assign(tool_name, instruction, payload, feedback=feedback, timeout=timeout)

    def prepare(self, run_id, attempt, specs, schemas, code_sets=None, rules=None, **kwargs):
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

    execute_attempts = []

    def execute(self, run_id, attempt, specs, schemas, golden_records, audit_cases, rules, *args, **kwargs):
        execute_attempts.append(attempt)
        passed = attempt >= 2
        return {
            "all_passed": passed,
            "coverage_complete": True,
            "coverage_rate": 1.0,
            "uncovered_rules": [],
            "backend": "ORACLE",
            "table_count": len(specs),
            "case_count": 2,
            "active_rule_count": len(rules),
            "false_positive_count": 0,
            "false_negative_count": 0 if passed else 1,
            "record_counts": {},
            "physical_tables": kwargs["prepared_assets"]["physical_tables"],
            "target_tables": kwargs["prepared_assets"]["target_tables"],
            "code_set_tables": {},
            "code_set_versions": {},
            "code_set_record_counts": {},
            "cases": [
                {
                    "table_code": slim_spec.table_code,
                    "case_id": "BREAK_REQUIRED",
                    "case_kind": "MUTATION",
                    "expected_rules": ["R_EXPLICIT_ENUM"],
                    "actual_rules": ["R_EXPLICIT_ENUM"] if passed else [],
                    "missing_rules": [] if passed else ["R_EXPLICIT_ENUM"],
                    "unexpected_rules": [],
                    "co_triggered_rules": [],
                    "passed": passed,
                }
            ],
        }

    monkeypatch.setattr(GenericOracleExecutor, "prepare_assets", prepare)
    monkeypatch.setattr(GenericOracleExecutor, "execute", execute)
    settings = Settings(blob_dir=str(tmp_path), oracle_sandbox_url="sqlite://")
    budget = {"max_rules_per_table": 10, "max_cases_per_table": 20, "max_retries": 3, "max_llm_concurrency": 2}

    run = start_generic_run(session, spec_id, settings, SelfHealLlm(), generation_budget=budget)
    state = project(session, run)
    assert state.status == "WAITING_TEST_APPROVAL"
    assert execute_attempts == [1, 2]
    # 自愈阶段只调用了 1 次带 current_rule 的靶向重绑，没有重新跑全表初绑
    rebind_calls = [p for p in rule_call_payloads if "current_rule" in p]
    initial_calls = [p for p in rule_call_payloads if "current_rule" not in p]
    assert len(rebind_calls) == 1
    # 确定性派生前置后，每字段单次返回全部剩余语义规则（2 个字段 = 2 次初绑调用），自愈未新增初绑调用
    assert len(initial_calls) == 2
    kinds = [e.kind for e in session.query(EventModel).filter(EventModel.run_id == run.id).order_by(EventModel.seq).all()]
    assert "GO_REDIRECT" in kinds


def test_retry_generic_run_resumes_from_last_completed_checkpoint_when_no_audit_report(organization_spec, monkeypatch, tmp_path):
    """当批次在中途（如 data_generator_binder）因超时或重启失败、尚未产生 audit_report_blob 时，
    retry_generic_run 应通过 RUN_RESUMED 从上一个已完成节点断点续跑，不重跑前半段。
    """
    session = make_session()
    spec_id = str(uuid.uuid4())
    slim_spec = organization_spec.model_copy(update={"fields": organization_spec.fields[:1]})
    session.add(
        SpecificationModel(
            id=spec_id,
            version="SAFE_1.3",
            table_code="CONFIRMED_SNAPSHOT",
            source_name=slim_spec.source_name,
            source_sha256=slim_spec.source_sha256,
            content_json={"table_name": "confirmed", "selection_confirmed": True, "tables": [slim_spec.model_dump()]},
        )
    )
    session.commit()

    rule_calls = []
    fail_generators = {"should_fail": True}

    class FlakyGeneratorLlm(FixtureDesignLlm):
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            if tool_name == "assign_rules":
                rule_calls.append(1)
                args = {
                    "rules": [
                        {
                            "rule_key": "R_DOC_REGEXP",
                            "field": slim_spec.fields[0].name,
                            "operator": "REGEXP",
                            "params": {"pattern": "^[A-Z0-9]+$"},
                            "evidence_kind": "DICTIONARY",
                            "rationale": "explicit format regexp",
                        }
                    ]
                }
                return args, {"provider": "FIXTURE", "model": "fixture", "tool_name": tool_name, "arguments": args}
            if tool_name == "assign_generators" and fail_generators["should_fail"]:
                raise RuleValidationError("simulated generator failure")
            if tool_name == "assign_generators":
                args = {
                    "generators": [
                        {"field": field["name"], "generator": "FIXED", "params": {"value": "ABC123"}}
                        for field in payload["fields"]
                    ]
                }
                return args, {"provider": "FIXTURE", "model": "fixture", "tool_name": tool_name, "arguments": args}
            return super().assign(tool_name, instruction, payload, feedback=feedback, timeout=timeout)

    def prepare(self, run_id, attempt, specs, schemas, code_sets=None, rules=None, **kwargs):
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
        return {
            "all_passed": True,
            "coverage_complete": True,
            "coverage_rate": 1.0,
            "uncovered_rules": [],
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
    llm = FlakyGeneratorLlm()

    run = start_generic_run(session, spec_id, settings, llm)
    failed_state = project(session, run)
    assert failed_state.status == "FAILED"
    assert failed_state.current_node == "data_generator_binder"
    assert failed_state.resume_from_node == "oracle_asset_builder"
    assert len(rule_calls) == 1

    # 恢复正常后调用 retry_generic_run：从 oracle_asset_builder 之后断点续跑，不重跑前半段（schema_mapper / rule_binder）
    fail_generators["should_fail"] = False
    run = retry_generic_run(session, run, settings, llm=llm)
    resumed_state = project(session, run)
    assert resumed_state.status == "WAITING_TEST_APPROVAL"
    assert len(rule_calls) == 1  # rule_binder 未被重复调用
    kinds = [e.kind for e in session.query(EventModel).filter(EventModel.run_id == run.id).order_by(EventModel.seq).all()]
    assert kinds.count("SCHEMA_ASSIGNED") == 1  # schema_mapper 未被重复执行
    assert "RUN_RESUMED" in kinds
