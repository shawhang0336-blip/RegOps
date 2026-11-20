"""三批次 Agent Flow 优化回归测试（覆盖全 3 步 + 编译器 + 门禁 + 确定性引擎 + 双路径自愈）。"""

import uuid

import pytest

from app.assets.code_sets.service import extract_code_sets_for_block
from app.engine.binding import (
    GeneratorBinding,
    bind_rules,
    compile_schema_bindings,
    find_golden_rule_violations,
    violates_field_compare,
    violates_single_field_rule,
)
from app.engine.compiler import compile_rule_query
from app.engine.mutations import (
    build_deterministic_mutations,
    build_golden_audit_case,
    build_regexp_fallback_mutation,
)
from app.engine.oracle import GenericOracleExecutor
from app.engine.rule_backfill import _code_set_enum_backfill_candidates
from app.engine.rules import RuleValidationError
from app.engine.workflow import start_generic_run
from app.engine.workflow.stages import _audit_stage, _data_stage
from app.infra.asset_models import CodeSetValidationModel, RawBlockModel, SourceDocumentModel
from app.infra.config import Settings
from app.infra.domain import Evidence, FieldSpec, RuleDefinition, SpecDocument
from app.infra.llm import _tool_prompt_preamble
from app.infra.models import EventModel, RuleModel, RunModel, SpecificationModel
from app.ingestion.structure_service import analyze_document_structure
from app.policy.llm_guard import call_llm_validated
from app.policy.regops_bus import code_set_values_adjudicator, cross_table_adjudicator
from app.policy.regops_plan import DEFAULT_PLAN, project, self_heal_cases_target, self_heal_data_target
from .helpers import make_session


def _sample_spec() -> SpecDocument:
    return SpecDocument(
        version="TEST",
        table_code="T_FLOW",
        table_name="测试流水表",
        source_name="spec.pdf",
        source_sha256="a" * 64,
        section="6.9.1",
        fields=[
            FieldSpec(name="ORG_ID", content="机构编号", data_type="VARCHAR", length=18, check_text="必填项。", evidence_page="1"),
            FieldSpec(name="TYPE_CODE", content="主体类型", data_type="VARCHAR", length=2, check_text="必填项。", evidence_page="1"),
            FieldSpec(name="EXTRA_NAME", content="扩展名称", data_type="VARCHAR", length=32, check_text="当主体类型为01或02时必填。", evidence_page="1"),
            FieldSpec(name="START_DATE", content="起息日", data_type="VARCHAR", length=8, check_text="YYYYMMDD格式。", evidence_page="1"),
            FieldSpec(name="END_DATE", content="到期日", data_type="VARCHAR", length=8, check_text="到期日>=起息日。", evidence_page="1"),
            FieldSpec(name="AMOUNT", content="金额", data_type="NUMBER", length=16, decimal_places=2, check_text="大于0且不超过1000。", evidence_page="1"),
            FieldSpec(name="AREA_CODE", content="地区代码", data_type="VARCHAR", length=6, check_text="不能为100000。", evidence_page="1"),
        ],
        terms_text="",
        dictionary_text="",
    )


def _make_rule(table_code: str, rule_key: str, field: str, operator: str, params: dict, rationale: str = "test") -> RuleDefinition:
    return RuleDefinition(
        rule_key=rule_key,
        table_code=table_code,
        field=field,
        operator=operator,
        params=params,
        source="DOC_EXPLICIT",
        evidence=Evidence(section="6.9.1.3", page="1", field=field, quote="test"),
        rationale=rationale,
        rule_hash=uuid.uuid4().hex + uuid.uuid4().hex,
    )


# ── Batch 1: 编译器修复 / Prompt 隔离 / 确定性 Schema / 码表前置派生 ──


def test_cond_required_multi_value_compiles_to_in_and_not_in():
    rule_eq = _make_rule(
        "T_FLOW",
        "R_COND_EQ",
        "EXTRA_NAME",
        "COND_REQUIRED",
        {"if_field": "TYPE_CODE", "if_operator": "EQ", "if_values": ["01", "02"]},
    )
    sql_eq, params_eq = compile_rule_query(rule_eq, "oracle")
    assert 't."TYPE_CODE" IN (:if_value_0, :if_value_1)' in sql_eq
    assert "= (" not in sql_eq
    assert params_eq["if_value_0"] == "01" and params_eq["if_value_1"] == "02"

    rule_ne = _make_rule(
        "T_FLOW",
        "R_COND_NE",
        "EXTRA_NAME",
        "COND_REQUIRED",
        {"if_field": "TYPE_CODE", "if_operator": "NE", "if_values": ["01", "02"]},
    )
    sql_ne, _ = compile_rule_query(rule_ne, "oracle")
    assert 't."TYPE_CODE" NOT IN (:if_value_0, :if_value_1)' in sql_ne
    assert "<> (" not in sql_ne

    # 单值仍然编译为合法标量比较（无括号）
    rule_single = _make_rule(
        "T_FLOW",
        "R_COND_ONE",
        "EXTRA_NAME",
        "COND_REQUIRED",
        {"if_field": "TYPE_CODE", "if_operator": "EQ", "if_values": ["01"]},
    )
    sql_single, _ = compile_rule_query(rule_single, "oracle")
    assert 't."TYPE_CODE" = :if_value_0' in sql_single


def test_prompt_preamble_isolates_rule_operators_from_step1_tools():
    rule_prompt = _tool_prompt_preamble("assign_rules")
    assert "COND_REQUIRED" in rule_prompt
    assert "LENGTH_MAX" in rule_prompt
    assert "FIELD_COMPARE" in rule_prompt

    for step1_tool in ("inspect_document_structure", "assign_document_structure", "assign_metadata", "review_metadata", "review_code_set"):
        prompt = _tool_prompt_preamble(step1_tool)
        assert "COND_REQUIRED" not in prompt
        assert "FIELD_COMPARE" not in prompt
        assert "LENGTH_MAX" not in prompt


def test_compile_schema_bindings_is_pure_deterministic():
    spec = _sample_spec()
    bindings = compile_schema_bindings(spec)
    assert len(bindings) == len(spec.fields)
    by_field = {b.field: b for b in bindings}
    assert by_field["ORG_ID"].storage_type == "TEXT"
    assert by_field["ORG_ID"].length == 18
    assert by_field["AMOUNT"].storage_type == "NUMBER"
    assert by_field["AMOUNT"].decimal_places == 2


def test_code_set_enum_backfill_candidates_detects_published_code_sets():
    spec = _sample_spec()
    field_code_sets = {
        ("T_FLOW", "TYPE_CODE"): {
            "code": "CS_TYPE",
            "name": "主体类型代码表",
            "version": 1,
            "values": [{"value": "01"}, {"value": "02"}],
        }
    }
    candidates = _code_set_enum_backfill_candidates([spec], [], field_code_sets)
    assert len(candidates) == 1
    assert candidates[0][1].name == "TYPE_CODE"
    assert candidates[0][3] == ["01", "02"]


# ── Batch 2: 确定性变异引擎 / 黄金自洽门禁 / 双路径自愈 ──


def test_deterministic_mutation_engine_covers_all_non_regexp_operators():
    spec = _sample_spec()
    golden = {
        "ORG_ID": "ORG000001",
        "TYPE_CODE": "01",
        "EXTRA_NAME": "测试公司",
        "START_DATE": "20260115",
        "END_DATE": "20260630",
        "AMOUNT": 500,
        "AREA_CODE": "110000",
    }
    assert build_golden_audit_case().overrides == {}

    cases_req = build_deterministic_mutations(spec, _make_rule("T_FLOW", "R_REQ", "ORG_ID", "REQUIRED", {}), golden, mutations_per_rule=2)
    assert len(cases_req) == 2
    assert cases_req[0].overrides == {"ORG_ID": None}
    assert violates_single_field_rule("REQUIRED", {}, cases_req[1].overrides["ORG_ID"]) is True

    cases_len = build_deterministic_mutations(spec, _make_rule("T_FLOW", "R_LEN", "ORG_ID", "LENGTH_MAX", {"length": 18}), golden)
    assert len(cases_len[0].overrides["ORG_ID"]) == 19

    cases_enum = build_deterministic_mutations(spec, _make_rule("T_FLOW", "R_ENUM", "TYPE_CODE", "ENUM", {"values": ["01", "02"]}), golden)
    assert violates_single_field_rule("ENUM", {"values": ["01", "02"]}, cases_enum[0].overrides["TYPE_CODE"]) is True

    cases_notin = build_deterministic_mutations(spec, _make_rule("T_FLOW", "R_NOTIN", "AREA_CODE", "NOT_IN", {"values": ["100000"]}), golden)
    assert cases_notin[0].overrides["AREA_CODE"] == "100000"

    cases_range = build_deterministic_mutations(
        spec,
        _make_rule("T_FLOW", "R_RANGE", "AMOUNT", "NUM_RANGE", {"min": 0, "max": 1000, "min_inclusive": False, "max_inclusive": True}),
        golden,
        mutations_per_rule=2,
    )
    assert len(cases_range) == 2
    assert cases_range[0].overrides["AMOUNT"] == 0
    assert cases_range[1].overrides["AMOUNT"] > 1000

    cases_cmp = build_deterministic_mutations(
        spec,
        _make_rule("T_FLOW", "R_CMP", "END_DATE", "FIELD_COMPARE", {"right_field": "START_DATE", "operator": "GE"}),
        golden,
    )
    assert violates_field_compare("GE", cases_cmp[0].overrides["END_DATE"], golden["START_DATE"]) is True

    cases_cond = build_deterministic_mutations(
        spec,
        _make_rule("T_FLOW", "R_COND", "EXTRA_NAME", "COND_REQUIRED", {"if_field": "TYPE_CODE", "if_operator": "EQ", "if_values": ["01", "02"]}),
        golden,
    )
    assert cases_cond[0].overrides == {"TYPE_CODE": "01", "EXTRA_NAME": None}

    # REGEXP 返回 None 交给 AI，并具备兜底探测
    regex_rule = _make_rule("T_FLOW", "R_REG", "START_DATE", "REGEXP", {"pattern": "^[0-9]{8}$"})
    assert build_deterministic_mutations(spec, regex_rule, golden) is None
    fb = build_regexp_fallback_mutation(spec, regex_rule)
    assert fb is not None
    assert violates_single_field_rule("REGEXP", {"pattern": "^[0-9]{8}$"}, fb.overrides["START_DATE"]) is True


def test_golden_self_consistency_gate_rejects_contradictory_ai_generators_and_retries():
    session = make_session()
    spec = _sample_spec()
    run = RunModel(id=str(uuid.uuid4()), spec_id=str(uuid.uuid4()))
    session.add(run)
    session.flush()

    rules = [
        _make_rule("T_FLOW", "R_CMP", "END_DATE", "FIELD_COMPARE", {"right_field": "START_DATE", "operator": "GE"}),
        _make_rule("T_FLOW", "R_DATE", "START_DATE", "REGEXP", {"pattern": "^[0-9]{8}$"}),
    ]

    class ContradictoryThenFixedLlm:
        def __init__(self):
            self.calls = 0
            self.feedbacks = []

        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            assert tool_name == "assign_generators"
            self.calls += 1
            self.feedbacks.append(feedback)
            requested = [f["name"] for f in payload["fields"]]
            # 只应向 AI 发送关联字段 START_DATE / END_DATE（其余 5 个确定性字段零 LLM 调用）
            assert set(requested) == {"START_DATE", "END_DATE"}
            if self.calls == 1:
                # 第 1 次：故意给出到期日 < 起息日，触发 Python 侧黄金自洽门禁拦截
                args = {
                    "generators": [
                        {"field": "START_DATE", "generator": "FIXED", "params": {"value": "20260601"}},
                        {"field": "END_DATE", "generator": "FIXED", "params": {"value": "20260101"}},
                    ]
                }
            else:
                args = {
                    "generators": [
                        {"field": "START_DATE", "generator": "FIXED", "params": {"value": "20260101"}},
                        {"field": "END_DATE", "generator": "FIXED", "params": {"value": "20260601"}},
                    ]
                }
            return args, {"provider": "test", "model": "test", "tool_name": tool_name}

    llm = ContradictoryThenFixedLlm()
    golden_records, _ = _data_stage(session, run, [spec], llm, rules, {})
    assert llm.calls == 2
    assert llm.feedbacks[1] and "黄金样本自洽门禁拦截" in llm.feedbacks[1]
    assert golden_records["T_FLOW"]["START_DATE"] == "20260101"
    assert golden_records["T_FLOW"]["END_DATE"] == "20260601"
    assert find_golden_rule_violations(spec, rules, golden_records["T_FLOW"]) == []


def test_audit_stage_calls_llm_only_for_regexp_rules():
    session = make_session()
    spec = _sample_spec()
    run = RunModel(id=str(uuid.uuid4()), spec_id=str(uuid.uuid4()))
    session.add(run)
    session.flush()
    golden = {
        "T_FLOW": {
            "ORG_ID": "ORG001",
            "TYPE_CODE": "01",
            "EXTRA_NAME": "测试",
            "START_DATE": "20260101",
            "END_DATE": "20260601",
            "AMOUNT": 100,
            "AREA_CODE": "110000",
        }
    }
    rules = [
        _make_rule("T_FLOW", "R_REQ", "ORG_ID", "REQUIRED", {}),
        _make_rule("T_FLOW", "R_LEN", "ORG_ID", "LENGTH_MAX", {"length": 18}),
        _make_rule("T_FLOW", "R_REG", "START_DATE", "REGEXP", {"pattern": "^[0-9]{8}$"}),
    ]

    class OnlyRegexpAuditLlm:
        def __init__(self):
            self.targets = []

        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            assert tool_name == "assign_audit_cases"
            self.targets.append(payload["target_rule"]["rule_key"])
            return {
                "cases": [{"case_id": "MUT_REGEX_BAD_DATE", "overrides": {"START_DATE": "2026-01-01"}, "expected_rule_keys": ["R_REG"]}]
            }, {"provider": "test", "model": "test"}

    llm = OnlyRegexpAuditLlm()
    cases_by_table = _audit_stage(session, run, [spec], llm, rules, golden)
    # 3 条规则 + 1 条黄金基线，只应为 REGEXP 调 1 次 AI（其余全部确定性构造）
    assert llm.targets == ["R_REG"]
    ids = [c.case_id for c in cases_by_table["T_FLOW"]]
    assert "GOLDEN_BASELINE" in ids
    assert "MUT_REGEX_BAD_DATE" in ids
    assert len(ids) == 4


def test_dual_path_self_heal_routes_golden_false_positive_to_data_generator_without_weakening_rules(monkeypatch, tmp_path):
    assert DEFAULT_PLAN.validate() == []
    assert self_heal_data_target() == "data_generator_binder"
    assert self_heal_cases_target() == "audit_case_binder"

    session = make_session()
    spec = _sample_spec().model_copy(update={"fields": [_sample_spec().fields[3]]})  # START_DATE
    spec_id = str(uuid.uuid4())
    session.add(
        SpecificationModel(
            id=spec_id,
            version="TEST",
            table_code="CONFIRMED_SNAPSHOT",
            source_name=spec.source_name,
            source_sha256=spec.source_sha256,
            content_json={"table_name": "confirmed", "selection_confirmed": True, "tables": [spec.model_dump()]},
        )
    )
    session.commit()

    calls = {"rules": 0, "generators": 0, "heal_feedback_seen": False}

    class GoldenHealLlm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            if tool_name == "assign_rules":
                calls["rules"] += 1
                args = {
                    "rules": [
                        {
                            "rule_key": "R_START_REGEXP",
                            "field": "START_DATE",
                            "operator": "REGEXP",
                            "params": {"pattern": "^[0-9]{8}$"},
                            "evidence_kind": "DICTIONARY",
                            "rationale": "8位日期",
                        }
                    ]
                }
                return args, {"provider": "test", "model": "test"}
            if tool_name == "assign_generators":
                calls["generators"] += 1
                if payload.get("oracle_false_positive_feedback"):
                    calls["heal_feedback_seen"] = True
                args = {"generators": [{"field": "START_DATE", "generator": "FIXED", "params": {"value": "20260115"}}]}
                return args, {"provider": "test", "model": "test"}
            if tool_name == "assign_audit_cases":
                args = {"cases": [{"case_id": "BAD_DATE", "overrides": {"START_DATE": "BAD"}, "expected_rule_keys": ["R_START_REGEXP"]}]}
                return args, {"provider": "test", "model": "test"}
            raise AssertionError(tool_name)

    def prepare(self, run_id, attempt, specs, schemas, code_sets=None, rules=None, **kwargs):
        physical = {s.table_code: f"STAGING_{attempt}" for s in specs}
        return {
            "attempt": attempt,
            "physical_tables": physical,
            "target_tables": {c: f"TARGET_{attempt}" for c in physical},
            "code_set_tables": {},
            "code_set_versions": {},
            "code_set_record_counts": {},
            "empty_run": {"all_passed": True, "rule_count": len(rules or []), "statement_count": len(rules or [])},
        }

    attempts = []

    def execute(self, run_id, attempt, specs, schemas, golden_records, audit_cases, rules, *args, **kwargs):
        attempts.append(attempt)
        passed = attempt >= 2
        return {
            "all_passed": passed,
            "coverage_complete": True,
            "coverage_rate": 1.0,
            "uncovered_rules": [],
            "backend": "ORACLE",
            "table_count": 1,
            "case_count": 2,
            "active_rule_count": len(rules),
            "false_positive_count": 0 if passed else 1,
            "false_negative_count": 0,
            "record_counts": {},
            "physical_tables": kwargs["prepared_assets"]["physical_tables"],
            "target_tables": kwargs["prepared_assets"]["target_tables"],
            "code_set_tables": {},
            "code_set_versions": {},
            "code_set_record_counts": {},
            "cases": [
                {
                    "table_code": spec.table_code,
                    "case_id": "GOLDEN_BASELINE",
                    "case_kind": "GOLDEN",
                    "expected_rules": [],
                    "actual_rules": [] if passed else ["R_START_REGEXP"],
                    "missing_rules": [],
                    "unexpected_rules": [] if passed else ["R_START_REGEXP"],
                    "co_triggered_rules": [],
                    "passed": passed,
                }
            ],
        }

    monkeypatch.setattr(GenericOracleExecutor, "prepare_assets", prepare)
    monkeypatch.setattr(GenericOracleExecutor, "execute", execute)
    settings = Settings(blob_dir=str(tmp_path), oracle_sandbox_url="sqlite://")

    run = start_generic_run(session, spec_id, settings, GoldenHealLlm())
    state = project(session, run)
    assert state.status == "WAITING_TEST_APPROVAL"
    assert attempts == [1, 2]
    assert calls["rules"] == 1  # 规则未被重绑或削弱
    assert calls["generators"] == 2  # 回到 data_generator_binder 重造测试数据
    assert calls["heal_feedback_seen"] is True
    redirects = [
        e.payload_json for e in session.query(EventModel).filter(EventModel.run_id == run.id, EventModel.kind == "GO_REDIRECT").all()
    ]
    assert len(redirects) == 1
    assert redirects[0]["target"] == "data_generator_binder"


# ── Batch 3: 统一守卫 / 第一步重试 / 自适应码表复核 / 异名外键 ──


def test_call_llm_validated_fails_fast_on_fatal_runtime_error():
    calls = []

    class AuthErrorLlm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            calls.append(1)
            raise RuntimeError("LLM HTTP 401 unauthorized")

    with pytest.raises(Exception, match="401"):
        call_llm_validated(AuthErrorLlm(), "assign_metadata", "instr", {}, backoff=(0,))
    assert len(calls) == 1


def test_structure_service_recovers_after_bus_block_id_rejection(monkeypatch):
    session = make_session()
    monkeypatch.setattr("app.ingestion.structure_service.SessionLocal", lambda: session)
    doc = SourceDocumentModel(
        id=str(uuid.uuid4()),
        file_name="sample.docx",
        file_type="docx",
        sha256=uuid.uuid4().hex,
        blob_uri="blob://sample",
        spec_version="TEST",
        status="UPLOADED",
        block_count=1,
    )
    block = RawBlockModel(
        id=str(uuid.uuid4()),
        document_id=doc.id,
        ordinal=0,
        block_type="RAW_TEXT",
        raw_content="某章节混合正文",
        source_locator={"section": "6.1"},
        content_json={},
        raw_hash="h1",
    )
    session.add_all([doc, block])
    session.commit()

    class BusRecoverLlm:
        def __init__(self):
            self.calls = 0
            self.feedbacks = []

        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            self.calls += 1
            self.feedbacks.append(feedback)
            if self.calls == 1:
                # 编造不存在的块 ID -> 应被 structure_block_adjudicator 拦截并回喂重试
                return {
                    "blocks": [{"source_block_ids": ["GHOST_ID"], "role": "NARRATIVE", "title": "错", "column_mapping": {}, "human_required": False, "warnings": [], "split_markers": []}],
                    "global_warnings": [],
                }, {"provider": "test"}
            return {
                "blocks": [{"source_block_ids": [block.id], "role": "NARRATIVE", "title": "已修正", "column_mapping": {}, "human_required": False, "warnings": [], "split_markers": []}],
                "global_warnings": [],
            }, {"provider": "test"}

    llm = BusRecoverLlm()
    monkeypatch.setattr("app.ingestion.structure_service.RealDesignLlm", lambda settings: llm)
    analyze_document_structure(doc.id, Settings())
    assert llm.calls == 2
    assert llm.feedbacks[1] and "块 ID 幻觉拦截" in llm.feedbacks[1]


def test_adaptive_code_set_review_skips_ai_for_clean_structured_tables(monkeypatch):
    session = make_session()
    monkeypatch.setattr("app.assets.code_sets.service.SessionLocal", lambda: session)
    calls = []

    class CountingLlm:
        def assign(self, tool_name, *args, **kwargs):
            calls.append(tool_name)
            return {"issues": []}, {"provider": "test"}

    monkeypatch.setattr("app.assets.code_sets.service.RealDesignLlm", lambda settings: CountingLlm())
    doc = SourceDocumentModel(
        id=str(uuid.uuid4()),
        file_name="codes.xlsx",
        file_type="xlsx",
        sha256=uuid.uuid4().hex,
        blob_uri="blob://codes",
        spec_version="TEST",
        status="STRUCTURE_REVIEWED",
        block_count=1,
    )
    block = RawBlockModel(
        id=str(uuid.uuid4()),
        document_id=doc.id,
        ordinal=0,
        block_type="CODE_SET",
        raw_content="USD 美元",
        source_locator={"sheet": "币种"},
        content_json={
            "candidate_role": "CODE_SET",
            "code_set_spec": {
                "code": "CCY_CLEAN",
                "name": "币种表",
                "values": [{"value": "USD", "label_cn": "美元"}],
            },
        },
        raw_hash="ccy",
    )
    session.add_all([doc, block])
    session.commit()

    extract_code_sets_for_block(block.id, Settings(), auto_link=False, review_with_ai="adaptive")
    assert calls == []
    review = session.query(CodeSetValidationModel).filter_by(validator_kind="AI_REVIEW").one()
    assert review.trace_json["strategy"] == "DETERMINISTIC_CROSS_CHECK"


def test_cross_table_foreign_key_allows_non_identical_field_names_and_blocks_hallucination():
    parent = SpecDocument(
        version="TEST",
        table_code="T_PARENT",
        table_name="主表",
        source_name="s.pdf",
        source_sha256="1" * 64,
        section="6.1",
        fields=[FieldSpec(name="ID", content="主键", data_type="VARCHAR", length=32, check_text="必填", evidence_page="1")],
        terms_text="",
        dictionary_text="",
    )
    child = SpecDocument(
        version="TEST",
        table_code="T_CHILD",
        table_name="子表",
        source_name="s.pdf",
        source_sha256="1" * 64,
        section="6.2",
        fields=[FieldSpec(name="CUST_ID", content="客户主键", data_type="VARCHAR", length=32, check_text="关联主表ID", evidence_page="2")],
        terms_text="",
        dictionary_text="",
    )
    specs_by_code = {"T_PARENT": parent, "T_CHILD": child}
    rules = bind_rules(
        child,
        [
            {
                "rule_key": "R_FK_CUST",
                "field": "CUST_ID",
                "operator": "FOREIGN_KEY",
                "params": {"references_table": "T_PARENT", "references_field": "ID"},
                "evidence_kind": "DICTIONARY",
                "rationale": "异名外键",
            }
        ],
        selected_table_codes={"T_PARENT", "T_CHILD"},
        specs_by_code=specs_by_code,
    )
    assert rules[0].params == {"references_table": "T_PARENT", "references_field": "ID"}

    with pytest.raises(RuleValidationError, match="outside the target table whitelist"):
        bind_rules(
            child,
            [
                {
                    "rule_key": "R_FK_BAD",
                    "field": "CUST_ID",
                    "operator": "FOREIGN_KEY",
                    "params": {"references_table": "T_PARENT", "references_field": "NO_SUCH_COL"},
                    "evidence_kind": "DICTIONARY",
                    "rationale": "幻觉列",
                }
            ],
            selected_table_codes={"T_PARENT", "T_CHILD"},
            specs_by_code=specs_by_code,
        )

    # Bus 跨表字段门禁 & 批量码表门禁同步校验
    rej = cross_table_adjudicator(
        "assign_rules",
        {"rules": [{"table_code": "T_CHILD", "field": "CUST_ID", "operator": "FOREIGN_KEY", "params": {"references_table": "T_PARENT", "references_field": "GHOST"}}]},
        {"table_codes": ["T_PARENT", "T_CHILD"], "table_fields": {"T_PARENT": ["ID"], "T_CHILD": ["CUST_ID"]}},
    )
    assert rej and "跨表字段幻觉拦截" in rej

    cs_rej = code_set_values_adjudicator(
        "assign_generators",
        {"generators": [{"field": "TYPE_CODE", "generator": "ENUM", "params": {"values": ["99"]}}]},
        {"published_code_sets": {"TYPE_CODE": {"code": "CS_TYPE", "values": ["01", "02"]}}},
    )
    assert cs_rej and "码值白名单拦截" in cs_rej


def test_metadata_extraction_with_zero_suspect_fields_succeeds_without_zero_worker_crash(monkeypatch):
    """回归：所有字段均为确定性可解析（suspect_items=[] / chunks=[]）时，不得因 max_workers=0 崩溃。"""
    from app.assets.metadata_service import extract_metadata_for_block
    from app.infra.asset_models import TableMetadataModel

    session = make_session()
    monkeypatch.setattr("app.assets.metadata_service.SessionLocal", lambda: session)
    doc = SourceDocumentModel(
        id=str(uuid.uuid4()),
        file_name="clean.xlsx",
        file_type="xlsx",
        sha256=uuid.uuid4().hex,
        blob_uri="blob://clean",
        spec_version="TEST",
        status="STRUCTURE_REVIEWED",
        block_count=1,
    )
    block = RawBlockModel(
        id=str(uuid.uuid4()),
        document_id=doc.id,
        ordinal=0,
        block_type="TABLE_METADATA",
        raw_content="字段中文名 | 字段英文名 | 字段校验与类型说明",
        source_locator={"sheet": "外汇申报明细表"},
        content_json={
            "headers": ["字段中文名", "字段英文名", "字段校验与类型说明"],
            "rows": [
                ["字段中文名", "字段英文名", "字段校验与类型说明"],
                ["机构代码", "ORG_CODE", "必填项，字符型，18。"],
                ["交易金额", "TX_AMT", "必填项，数值型，16.2。"],
            ],
        },
        raw_hash="clean_meta",
    )
    session.add_all([doc, block])
    session.commit()

    table_id = extract_metadata_for_block(block.id, Settings(llm_api_key=None), auto_link=False)
    table = session.get(TableMetadataModel, table_id)
    assert table is not None
    assert table.table_name == "外汇申报明细表"


def test_long_case_id_is_normalized_within_oracle_record_id_limit():
    from app.engine.binding import MAX_CASE_ID_LENGTH, bind_audit_cases, normalize_case_id

    long_id = "TABLE_2915CE84B85B_START_DATE_DATE_VALID_MUT_03_YEAR_PREFIX_OUT_OF_RANGE"
    assert len(long_id) > MAX_CASE_ID_LENGTH
    short = normalize_case_id(long_id)
    assert len(short) <= MAX_CASE_ID_LENGTH
    assert short == normalize_case_id(long_id)  # 确定性幂等

    spec = _sample_spec()
    bound = bind_audit_cases(
        spec,
        [{"case_id": long_id, "overrides": {"ORG_ID": None}, "expected_rule_keys": ["R_REQ"]}],
        {"R_REQ"},
        {"R_REQ": ("ORG_ID", "REQUIRED", {})},
    )
    assert len(bound[0].case_id) <= MAX_CASE_ID_LENGTH


def test_rule_binder_drops_cross_field_bleed_and_dedupes_pending_rules():
    from app.engine.workflow.stages import _dedupe_approved_rules, _rules_stage

    session = make_session()
    spec = _sample_spec().model_copy(update={"fields": _sample_spec().fields[:2]})  # ORG_ID, TYPE_CODE
    run = RunModel(id=str(uuid.uuid4()), spec_id=str(uuid.uuid4()))
    session.add(run)
    session.flush()

    class BleedyLlm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            # 故意在每次字段调用里都越界返回两个字段的规则
            args = {
                "rules": [
                    {
                        "rule_key": "R_ORG_REGEXP",
                        "field": "ORG_ID",
                        "operator": "REGEXP",
                        "params": {"pattern": "^[A-Z0-9]{18}$"},
                        "evidence_kind": "TERM_INFERENCE",
                        "rationale": "18位",
                    },
                    {
                        "rule_key": "R_TYPE_REGEXP",
                        "field": "TYPE_CODE",
                        "operator": "REGEXP",
                        "params": {"pattern": "^[0-9]{2}$"},
                        "evidence_kind": "TERM_INFERENCE",
                        "rationale": "2位",
                    },
                ]
            }
            return args, {"provider": "test", "model": "test"}

    pending_count, _ = _rules_stage(session, run, [spec], BleedyLlm())
    assert pending_count == 2  # 每个字段只保留自身那 1 条，绝不翻倍成 4 条
    pending_rows = session.query(RuleModel).filter_by(run_id=run.id, status="PENDING").all()
    assert {(r.definition_json["field"], r.definition_json["operator"]) for r in pending_rows} == {
        ("ORG_ID", "REGEXP"),
        ("TYPE_CODE", "REGEXP"),
    }

    # 再人为塞一条同语义 PENDING 副本，验证 _dedupe_approved_rules 也能去重 PENDING
    dup = RuleModel(
        row_id=str(uuid.uuid4()),
        run_id=run.id,
        spec_id=run.spec_id,
        table_code=spec.table_code,
        rule_key="R_ORG_REGEXP_DUP",
        rule_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        source="AI_EXPANDED",
        status="PENDING",
        definition_json=pending_rows[0].definition_json,
    )
    session.add(dup)
    session.flush()
    assert _dedupe_approved_rules(session, run) == 1


