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


def test_data_stage_filters_extra_ai_fields_and_aligns_cross_table_fk_golden_values():
    """回归：AI 超范围多返回确定性字段时不崩溃、不覆盖确定性值，且并发多表外键黄金值自动对齐（含异名外键）。"""
    session = make_session()
    parent = SpecDocument(
        version="TEST",
        table_code="T_ORG",
        table_name="机构主档",
        source_name="s.xlsx",
        source_sha256="1" * 64,
        section="1",
        fields=[
            FieldSpec(name="ORG_ID", content="机构主键", data_type="VARCHAR", length=18, check_text="必填", evidence_page="1"),
            FieldSpec(name="ORG_NAME", content="机构名称", data_type="VARCHAR", length=64, check_text="必填", evidence_page="1"),
        ],
        terms_text="",
        dictionary_text="",
    )
    child = SpecDocument(
        version="TEST",
        table_code="T_TXN",
        table_name="交易明细",
        source_name="s.xlsx",
        source_sha256="1" * 64,
        section="2",
        fields=[
            FieldSpec(name="TXN_NO", content="流水号", data_type="VARCHAR", length=32, check_text="必填", evidence_page="2"),
            FieldSpec(name="CUST_ORG_REF", content="关联机构", data_type="VARCHAR", length=18, check_text="关联主表ORG_ID", evidence_page="2"),
            FieldSpec(name="TXN_AMT", content="交易金额", data_type="NUMBER", length=16, decimal_places=2, check_text="大于0", evidence_page="2"),
        ],
        terms_text="",
        dictionary_text="",
    )
    run = RunModel(id=str(uuid.uuid4()), spec_id=str(uuid.uuid4()))
    session.add(run)
    session.flush()

    rules = [
        _make_rule("T_ORG", "R_ORG_REQ", "ORG_ID", "REQUIRED", {}),
        _make_rule("T_TXN", "R_TXN_REQ", "TXN_NO", "REQUIRED", {}),
        _make_rule("T_TXN", "R_TXN_FK", "CUST_ORG_REF", "FOREIGN_KEY", {"references_table": "T_ORG", "references_field": "ORG_ID"}),
        _make_rule("T_TXN", "R_TXN_AMT", "TXN_AMT", "NUM_RANGE", {"min": 1, "max": 9999}),
    ]

    class OverReturningGeneratorLlm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            assert tool_name == "assign_generators"
            assert payload["target_fields"] == ["CUST_ORG_REF", "TXN_AMT"]
            # 故意顺手把确定性字段 TXN_NO 也一起返回，且 CUST_ORG_REF 给一个与主表不同的值
            return {
                "generators": [
                    {"field": "TXN_NO", "generator": "FIXED", "params": {"value": "AI_SHOULD_NOT_OVERWRITE"}},
                    {"field": "CUST_ORG_REF", "generator": "FIXED", "params": {"value": "DIFF_ORG_999"}},
                    {"field": "TXN_AMT", "generator": "FIXED", "params": {"value": 88.5}},
                ]
            }, {"provider": "test", "model": "test"}

    golden_records, generator_payload = _data_stage(session, run, [parent, child], OverReturningGeneratorLlm(), rules, {})
    # 确定性字段 TXN_NO 未被 AI 覆盖
    assert golden_records["T_TXN"]["TXN_NO"] == "SAMPLE_OK"
    assert golden_records["T_TXN"]["TXN_AMT"] == 88.5
    # 跨表异名外键黄金值与生成器均已与主表 ORG_ID 自动对齐
    assert golden_records["T_TXN"]["CUST_ORG_REF"] == golden_records["T_ORG"]["ORG_ID"]
    child_gens = {g["field"]: g for g in generator_payload["T_TXN"]["generators"]}
    assert child_gens["CUST_ORG_REF"]["params"]["value"] == golden_records["T_ORG"]["ORG_ID"]

    # 码表门禁：存在 published_code_sets 时不得把单字段 published_code_set 错套到其他字段
    assert (
        code_set_values_adjudicator(
            "assign_generators",
            {
                "generators": [
                    {"field": "CURRENCY_code", "generator": "ENUM", "params": {"values": ["USD"]}},
                    {"field": "ACTIONTYPE", "generator": "ENUM", "params": {"values": ["A"]}},
                ]
            },
            {
                "published_code_set": {"code": "CS_CCY", "values": ["USD", "CNY"]},
                "published_code_sets": {"CURRENCY_code": {"code": "CS_CCY", "values": ["USD", "CNY"]}},
            },
        )
        is None
    )


def test_delete_run_rows_collects_oracle_tables_from_failed_and_multi_attempt_events():
    """回归：失败批次或经历多轮自愈/重审的批次，删除时必须收集所有历史 attempt 的 Oracle 沙箱表。"""
    from app.api.runs import _delete_run_rows
    from app.kernel.event_log import append_event

    session = make_session()
    run = RunModel(id=str(uuid.uuid4()), spec_id=str(uuid.uuid4()))
    session.add(run)
    session.flush()

    append_event(session, run, "RUN_CREATED", "created", {})
    append_event(
        session,
        run,
        "ORACLE_ASSETS_PREPARED",
        "oracle_asset_builder",
        {
            "attempt": 1,
            "assets": {
                "physical_tables": {"T1": "DQ_AAA_1_T1"},
                "target_tables": {"T1": "DQ_AAA_1_T1_TARGET"},
                "code_set_tables": {"CS1": "DQ_AAA_1_CS1"},
            },
            "result": {
                "oracle_tables": {"T1": "DQ_AAA_1_T1"},
                "oracle_target_tables": {"T1": "DQ_AAA_1_T1_TARGET"},
                "oracle_code_set_tables": {"CS1": "DQ_AAA_1_CS1"},
            },
        },
    )
    append_event(
        session,
        run,
        "GO_REDIRECT",
        "oracle_executor",
        {
            "target": "data_generator_binder",
            "failed_result": {
                "oracle_tables": {"T1": "DQ_AAA_2_T1"},
                "oracle_target_tables": {"T1": "DQ_AAA_2_T1_TARGET"},
                "oracle_code_set_tables": {"CS1": "DQ_AAA_2_CS1"},
            },
        },
    )
    # 模拟最终失败覆盖投影 result_json
    append_event(session, run, "RUN_FAILED", "data_generator_binder", {"error": "boom"})
    session.flush()

    _, oracle_tables = _delete_run_rows(session, run)
    assert oracle_tables == {
        "DQ_AAA_1_T1",
        "DQ_AAA_1_T1_TARGET",
        "DQ_AAA_1_CS1",
        "DQ_AAA_2_T1",
        "DQ_AAA_2_T1_TARGET",
        "DQ_AAA_2_CS1",
    }


def test_partition_coupled_chunks_keeps_distant_field_compare_cluster_together():
    """回归：相隔超过 12 个字段的 FIELD_COMPARE 关联字段（如 TXCCY 与 INCHARGECCY/OUTCHARGECCY）必须在同一批联合造数。"""
    from app.engine.workflow.stages import _partition_coupled_chunks

    fields = [
        FieldSpec(name=f"F_{i:02d}", content=f"字段{i}", data_type="VARCHAR", length=8, check_text="必填", evidence_page="1")
        for i in range(18)
    ]
    rules = [
        _make_rule("T", "R_EQ_1", "F_16", "FIELD_COMPARE", {"right_field": "F_01", "operator": "EQ"}),
        _make_rule("T", "R_EQ_2", "F_17", "FIELD_COMPARE", {"right_field": "F_01", "operator": "EQ"}),
    ]
    chunks = _partition_coupled_chunks(fields, rules, batch_size=12)
    chunk_sets = [{f.name for f in c} for c in chunks]
    # F_01, F_16, F_17 必须落在同一个 chunk，绝不被切开
    assert any({"F_01", "F_16", "F_17"}.issubset(s) for s in chunk_sets)
    assert sum(len(s) for s in chunk_sets) == 18


def test_flattened_terms_text_does_not_smear_neighbor_code_set_and_clearing_unbinds():
    """回归：非制表符展平术语文本不得把相邻字段的「见币种代码表」串味到前面字段；清空 code_reference_text 必须解绑旧引用。"""
    from app.assets.code_sets.linking import ensure_field_reference
    from app.assets.metadata_service import _deterministic_field_draft
    from app.infra.asset_models import CodeSetModel, FieldCodeReferenceModel, FieldMetadataModel, TableMetadataModel
    from app.ingestion.spec_parser import lookup_term_definition

    flattened = "金融机构标识码 国家外汇管理局签发的12位标识码。 发生日期 账户收支余发生变动日期。 币种 币种代码，见币种代码表。"
    assert lookup_term_definition(flattened, "发生日期") == "账户收支余发生变动日期。"
    assert "币种代码表" not in lookup_term_definition(flattened, "发生日期")

    draft = _deterministic_field_draft(
        {"name": "DEAL_DATE", "content": "发生日期", "data_type": "VARCHAR", "length": 8, "check_text": "必填项，格式YYYYMMDD"},
        flattened,
        {"page": "130"},
    )
    assert draft.code_reference_text == ""

    # 清空 code_reference_text 时 ensure_field_reference 必须删除残留关联
    session = make_session()
    doc = SourceDocumentModel(
        id=str(uuid.uuid4()), file_name="a.pdf", file_type="pdf", sha256=uuid.uuid4().hex, blob_uri="blob://a", spec_version="V1", status="DONE", block_count=1
    )
    block = RawBlockModel(
        id=str(uuid.uuid4()), document_id=doc.id, ordinal=0, block_type="TABLE_METADATA", raw_content="", source_locator={}, content_json={}, raw_hash="h"
    )
    tbl = TableMetadataModel(
        id=str(uuid.uuid4()), document_id=doc.id, block_id=block.id, table_code="T1", table_name="T1", table_definition="", status="DRAFT", current_version=1
    )
    fld = FieldMetadataModel(
        id=str(uuid.uuid4()), table_id=tbl.id, field_cn="发生日期", field_en="DEAL_DATE", field_definition="", check_text_raw="必填",
        data_type="VARCHAR", length=8, decimal_places=0, required=True, code_reference_text="币种代码表", confidence=0.9, evidence_json={}, ai_value_json={}, sort_order=0,
    )
    cs = CodeSetModel(id=str(uuid.uuid4()), code="CS_CCY", name="币种代码表", definition="", source_type="MANUAL", status="PUBLISHED", current_version=1, published_version=1)
    session.add_all([doc, block, tbl, fld, cs])
    session.flush()
    session.add(FieldCodeReferenceModel(id=str(uuid.uuid4()), field_id=fld.id, code_set_id=cs.id, reference_text="币种代码表", status="RESOLVED", condition_json={}, confidence=0.9))
    session.flush()
    assert session.query(FieldCodeReferenceModel).filter_by(field_id=fld.id).count() == 1

    fld.code_reference_text = ""
    ensure_field_reference(session, fld)
    assert session.query(FieldCodeReferenceModel).filter_by(field_id=fld.id).count() == 0


def test_fullwidth_unicode_digits_rejected_on_number_field_and_large_num_range_precision():
    """回归：拦截全角数字 '１００' 避免 Oracle ORA-01722；22 位金额大边界（±1e20）不因 float64 精度丢失导致漏变异。"""
    from app.engine.binding import bind_audit_cases

    spec = _sample_spec()
    with pytest.raises(RuleValidationError, match="非法数值"):
        bind_audit_cases(
            spec,
            [{"case_id": "MUT_FULLWIDTH", "overrides": {"AMOUNT": "１００"}, "expected_rule_keys": ["R_AMT"]}],
            {"R_AMT"},
            {"R_AMT": ("AMOUNT", "REGEXP", {"pattern": "^[0-9]+$"})},
        )

    huge_rule = _make_rule("T_FLOW", "R_AMT_HUGE", "AMOUNT", "NUM_RANGE", {"min": -1e20, "max": 1e20, "min_inclusive": True, "max_inclusive": True})
    cases = build_deterministic_mutations(spec, huge_rule, {"AMOUNT": 100}, mutations_per_rule=2)
    assert cases and len(cases) == 2
    for c in cases:
        assert violates_single_field_rule("NUM_RANGE", huge_rule.params, c.overrides["AMOUNT"]) is True


def test_call_llm_validated_retries_wrapped_httpx_connect_error():
    """回归：llm.py 将 httpx.ConnectError 包装成 RuntimeError 后，守卫层仍应判定为瞬时网络错误并退避重试。"""
    import httpx

    attempts = []

    class FlakyConnectLlm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            attempts.append(1)
            if len(attempts) == 1:
                try:
                    raise httpx.ConnectError("temporary dns blip")
                except httpx.HTTPError as exc:
                    raise RuntimeError("ConnectError after 0.19s; request_chars=8300") from exc
            return {"rules": []}, {"provider": "test"}

    res, _ = call_llm_validated(FlakyConnectLlm(), "assign_rules", "instr", {}, backoff=(0,))
    assert res == {"rules": []}
    assert len(attempts) == 2


def test_backfilled_length_max_rule_remains_editable_and_impossible_field_compare_is_rejected():
    """回归：确定性补齐的 LENGTH_MAX 规则在人工修改参数后仍能通过 validate_rule；单字符 vs 长编码的 FIELD_COMPARE(EQ) 被硬拦截。"""
    from app.engine.rule_backfill import _backfill_length_rules
    from app.engine.rules import validate_rule

    session = make_session()
    spec = _sample_spec().model_copy(
        update={
            "fields": [
                FieldSpec(name="CUSTYPE", content="收款人类型", data_type="VARCHAR", length=1, check_text="C-对公 D-对私，需与申报号码第19位一致", evidence_page="1"),
                FieldSpec(name="RPTNO", content="申报号码", data_type="VARCHAR", length=22, check_text="必填项，22位编号", evidence_page="1"),
                FieldSpec(name="AMOUNT", content="金额", data_type="NUMBER", length=16, decimal_places=2, check_text="必填项", evidence_page="1"),
            ]
        }
    )
    run = RunModel(id=str(uuid.uuid4()), spec_id=str(uuid.uuid4()))
    session.add(run)
    session.flush()

    added = _backfill_length_rules(session, run, [spec], [], lambda *a, **kw: None, lambda *a, **kw: None)
    rptno_len = next(r for r in added if r.field == "RPTNO" and r.operator == "LENGTH_MAX")
    # 模拟前端 PATCH /api/v2/artifacts/rules/{row_id} 修改 length 参数
    edited = rptno_len.model_copy(update={"params": {"length": 18}})
    validate_rule(edited, spec)  # 不得抛出 Explicit-rule evidence does not match

    # 1位枚举 vs 22位编号的整列 EQ 必须被拦截
    with pytest.raises(RuleValidationError, match="长度量级悬殊"):
        bind_rules(
            spec,
            [
                {
                    "rule_key": "R_BAD_EQ",
                    "field": "CUSTYPE",
                    "operator": "FIELD_COMPARE",
                    "params": {"right_field": "RPTNO", "operator": "EQ"},
                    "evidence_kind": "DICTIONARY",
                    "rationale": "错误建模",
                }
            ],
        )

    # 跨数据类型（VARCHAR vs NUMBER）的 FIELD_COMPARE 必须被拦截
    with pytest.raises(RuleValidationError, match="不能跨数据类型比较"):
        bind_rules(
            spec,
            [
                {
                    "rule_key": "R_BAD_TYPE_CMP",
                    "field": "RPTNO",
                    "operator": "FIELD_COMPARE",
                    "params": {"right_field": "AMOUNT", "operator": "GE"},
                    "evidence_kind": "DICTIONARY",
                    "rationale": "错类型比较",
                }
            ],
        )


def test_oracle_regexp_normalization_and_mysql8_large_int_event_hash_stability():
    """回归：Oracle POSIX 字符类 [...] 内规范化 \\- / \\d / \\. 防止 ORA-12728；超大整数事件在 MySQL 8 JSON 往返后哈希链仍有效。"""
    import re
    from app.engine.compiler import normalize_oracle_regexp
    from app.kernel.event_log import canonical_event_json

    assert normalize_oracle_regexp(r"^[0-9+\-() ]+$") == r"^[0-9+() -]+$"
    assert normalize_oracle_regexp(r"^[\d\-]+$") == r"^[0-9-]+$"
    assert normalize_oracle_regexp(r"^[a-zA-Z0-9_\-\.]+$") == r"^[a-zA-Z0-9_.-]+$"
    assert re.search(normalize_oracle_regexp(r"^[0-9+\-() ]+$"), "+86-010 (8888)") is not None

    spec = _sample_spec()
    # PCRE 负向前瞻在 Oracle REGEXP_LIKE 中不被支持，绑定阶段必须硬拦截
    with pytest.raises(RuleValidationError, match="前后瞻"):
        bind_rules(
            spec,
            [
                {
                    "rule_key": "R_LOOKAHEAD",
                    "field": "ORG_ID",
                    "operator": "REGEXP",
                    "params": {"pattern": r"^(?!000)[0-9]{6}$"},
                    "evidence_kind": "DICTIONARY",
                    "rationale": "前瞻",
                }
            ],
        )

    # MySQL 8 JSON 会把超过 2^64-1 的 int 存成 double，规范化序列化后写入与读出必须哈希一致
    assert canonical_event_json({"override": 101000000000000000000}) == canonical_event_json({"override": 1.01e+20})
    assert canonical_event_json({"normal_int": 42}) == '{"normal_int":42}'


def test_cond_required_blank_sentinel_compiles_for_oracle_without_null_trap():
    """回归：COND_REQUIRED 的 if_values 含空串/None（如「某字段非空时本字段必填」）不得编译出 Oracle 恒假谓词 <> '' / NOT IN (NULL, '')。"""
    from app.engine.binding import violates_cond_required

    spec = _sample_spec()
    rule_ne_blank = _make_rule(
        "T_FLOW", "R_COND_NONBLANK", "EXTRA_NAME", "COND_REQUIRED",
        {"if_field": "TYPE_CODE", "if_operator": "NE", "if_values": [None, ""]},
    )
    sql, params = compile_rule_query(rule_ne_blank, "oracle")
    assert "NOT (" in sql and "IS NULL" in sql
    assert "<>" not in sql and "NOT IN" not in sql
    assert "if_value_0" not in params
    assert violates_cond_required(rule_ne_blank.params, {"TYPE_CODE": "01", "EXTRA_NAME": None}, "EXTRA_NAME") is True
    assert violates_cond_required(rule_ne_blank.params, {"TYPE_CODE": None, "EXTRA_NAME": None}, "EXTRA_NAME") is False

    rule_ne_mixed = _make_rule(
        "T_FLOW", "R_COND_NONBLANK_NE0", "EXTRA_NAME", "COND_REQUIRED",
        {"if_field": "AMOUNT", "if_operator": "NE", "if_values": ["", "0"]},
    )
    sql_m, params_m = compile_rule_query(rule_ne_mixed, "oracle")
    assert "NOT (" in sql_m and "<> :if_value_0" in sql_m
    assert params_m["if_value_0"] == "0"

    rule_eq_blank = _make_rule(
        "T_FLOW", "R_COND_WHEN_BLANK", "EXTRA_NAME", "COND_REQUIRED",
        {"if_field": "TYPE_CODE", "if_operator": "EQ", "if_values": [""]},
    )
    sql_eq, params_eq = compile_rule_query(rule_eq_blank, "oracle")
    assert "IS NULL" in sql_eq and "= :if_value_0" not in sql_eq
    assert violates_cond_required(rule_eq_blank.params, {"TYPE_CODE": None, "EXTRA_NAME": None}, "EXTRA_NAME") is True
    muts = build_deterministic_mutations(spec, rule_eq_blank, {"TYPE_CODE": "01", "EXTRA_NAME": "OK"})
    assert muts and muts[0].overrides == {"TYPE_CODE": None, "EXTRA_NAME": None}


def test_audit_binding_rejects_empty_string_and_canonical_number_noops_for_oracle_regexp():
    """回归：Oracle 中 '' 等同于 NULL（不触发 REGEXP），且 NUMBER 列将 '100.00'/'00100' 规范化为 '100'，绑定阶段必须拦截此类空转反例。"""
    from app.engine.binding import bind_audit_cases

    spec = _sample_spec()
    # 空串 '' 期望命中 REGEXP 必须被拦下
    with pytest.raises(RuleValidationError, match="并不违规"):
        bind_audit_cases(
            spec,
            [{"case_id": "MUT_EMPTY_REGEX", "overrides": {"ORG_ID": ""}, "expected_rule_keys": ["R_ORG_REG"]}],
            {"R_ORG_REG"},
            {"R_ORG_REG": ("ORG_ID", "REGEXP", {"pattern": "^[A-Z0-9]{18}$"})},
        )
    # 数值列 "100.00" 存入 Oracle NUMBER 后变成 "100"，仍匹配 ^[1-9][0-9]*$，必须被拦下
    with pytest.raises(RuleValidationError, match="Oracle 实际评估值 '100'"):
        bind_audit_cases(
            spec,
            [{"case_id": "MUT_NUM_ZERO_DEC", "overrides": {"AMOUNT": "100.00"}, "expected_rule_keys": ["R_AMT_INT"]}],
            {"R_AMT_INT"},
            {"R_AMT_INT": ("AMOUNT", "REGEXP", {"pattern": "^[1-9][0-9]*$"})},
        )
    # 真正带非零小数的 "100.5" 存入 Oracle 后仍为 "100.5"，违反整数正则 -> 放行
    bound = bind_audit_cases(
        spec,
        [{"case_id": "MUT_NUM_REAL_DEC", "overrides": {"AMOUNT": "100.5"}, "expected_rule_keys": ["R_AMT_INT"]}],
        {"R_AMT_INT"},
        {"R_AMT_INT": ("AMOUNT", "REGEXP", {"pattern": "^[1-9][0-9]*$"})},
    )
    assert len(bound) == 1


def test_regops_centralized_run_cancel_resume_recover_and_active_guard(tmp_path):
    """回归：P0 收口后，RegOps 的取消、断点续跑、启动恢复、活跃删除守卫全部经由 kernel.run 统一驱动。"""
    from app.engine.workflow import (
        _create_run,
        cancel_generic_run,
        delete_generic_run,
        recover_interrupted_generic_runs,
        replay_generic_run,
        retry_generic_run,
    )
    from app.infra.errors import WorkflowError
    from app.kernel.run import run_registry

    session = make_session()
    spec = _sample_spec()
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

    settings = Settings(blob_dir=str(tmp_path), oracle_sandbox_url="")
    run = _create_run(session, spec_id, settings)
    session.commit()
    assert project(session, run).status == "RUNNING"

    # 运行中不允许删除
    with pytest.raises(WorkflowError, match="cannot be deleted"):
        delete_generic_run(session, run, settings)

    # 模拟进程内活跃线程：cancel_generic_run 应只投递取消令牌
    reg = run_registry()
    token = reg.register(run.id)
    try:
        sig = cancel_generic_run(session, run, reason="手工中止测试")
        assert sig["signalled"] is True
        assert token.is_cancelled is True
        assert token.reason == "手工中止测试"
    finally:
        reg.unregister(run.id, token)

    # 无活跃线程的挂起运行态：直接落账 RUN_CANCELLED -> status=FAILED(cancelled=True)，且支持断点续跑
    res = cancel_generic_run(session, run, reason="清理卡死任务")
    assert res["cancelled"] is True
    assert project(session, run).status == "FAILED"
    assert (project(session, run).result_json or {}).get("cancelled") is True
    assert replay_generic_run(session, run)["chain_valid"] is True

    # 取消后可通过 retry_generic_run 断点续跑回到 RUNNING
    retry_generic_run(session, run, settings, defer_continuation=True)
    assert project(session, run).status == "RUNNING"

    # 模拟进程重启恢复：将 RUNNING 统一标记 FAILED，之后允许干净删除
    recovered = recover_interrupted_generic_runs(session)
    assert run.id in recovered
    assert project(session, run).status == "FAILED"

    deleted = delete_generic_run(session, run, settings)
    assert deleted["deleted_id"] == run.id
    assert session.get(RunModel, run.id) is None


def test_regops_budget_guard_tracks_usage_and_trips_on_exceeded_limit():
    """回归：call_llm_validated 将 usage/elapsed_seconds 透传至 Bus，BudgetGuard 在超限时立即熔断。"""
    from app.kernel.budget import BudgetExceededError, BudgetGuard, BudgetLimits
    from app.policy.regops_bus import default_bus

    bus = default_bus()
    guard = BudgetGuard(BudgetLimits(max_llm_calls=2), run_id="budget-test").attach(bus)

    class CountingUsageLlm:
        def __init__(self):
            self.calls = 0

        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            self.calls += 1
            return {"rules": []}, {
                "provider": "test",
                "model": "test",
                "elapsed_seconds": 0.4,
                "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
            }

    llm = CountingUsageLlm()
    call_llm_validated(llm, "assign_rules", "instr", {}, bus=bus)
    call_llm_validated(llm, "assign_rules", "instr", {}, bus=bus)
    snap = guard.snapshot()
    assert snap["llm_calls"] == 2
    assert snap["total_tokens"] == 280
    assert snap["by_tool"]["assign_rules"] == 2

    # 第 3 次调用前应直接被 BudgetGuard 熔断，不再真正发出模型请求
    with pytest.raises(BudgetExceededError):
        call_llm_validated(llm, "assign_rules", "instr", {}, bus=bus)
    assert llm.calls == 2

    # 跨 Interrupt 断点恢复时通过 seed 无损续接累计消耗（不含人工等待时长）
    resumed_bus = default_bus()
    resumed_guard = BudgetGuard(BudgetLimits(max_llm_calls=5), run_id="budget-test", seed=snap).attach(resumed_bus)
    call_llm_validated(llm, "assign_generators", "instr", {}, bus=resumed_bus)
    resumed_snap = resumed_guard.snapshot()
    assert resumed_snap["llm_calls"] == 3
    assert resumed_snap["total_tokens"] == 420
    assert resumed_snap["by_tool"]["assign_rules"] == 2
    assert resumed_snap["by_tool"]["assign_generators"] == 1


def test_bsp_send_partial_wave_checkpoint_and_per_table_incremental_resume(monkeypatch, tmp_path):
    """P3 回归：多表真 BSP Send 波次中表 1 成功、表 2 失败时，表 1 检查点先落 EventLog；断点续跑时表 1 零重跑，仅补跑表 2。"""
    from app.engine.oracle import GenericOracleExecutor
    from app.engine.workflow import drive, retry_generic_run, start_generic_run
    from app.infra.models import EventModel, SpecificationModel
    from app.policy.regops_plan import project

    monkeypatch.setattr(
        GenericOracleExecutor,
        "prepare_assets",
        lambda self, run_id, attempt, specs, schemas, code_sets=None, rules=None, **kwargs: {
            "attempt": attempt,
            "physical_tables": {spec.table_code: f"STAGING_{spec.table_code}_{attempt}" for spec in specs},
            "target_tables": {spec.table_code: f"TARGET_{spec.table_code}_{attempt}" for spec in specs},
            "code_set_tables": {},
            "code_set_versions": {},
            "code_set_record_counts": {},
            "empty_run": {"all_passed": True, "rule_count": len(rules or []), "statement_count": len(rules or [])},
        },
    )

    session = make_session()
    spec_id = str(uuid.uuid4())
    spec_doc = SpecificationModel(
        id=spec_id,
        version="V1",
        table_code="DUAL_RESUME",
        source_name="dual-resume-test",
        source_sha256="dual_sha_" + uuid.uuid4().hex[:8],
        content_json={
            "selection_confirmed": True,
            "tables": [
                {
                    "version": "V1",
                    "table_code": "T1",
                    "table_name": "表一",
                    "source_name": "dual-resume-test",
                    "section": "1.1",
                    "source_sha256": "sha_t1",
                    "terms_text": "",
                    "dictionary_text": "CODE_A 必须为4位字母",
                    "fields": [{"name": "CODE_A", "content": "编码A", "data_type": "VARCHAR2", "length": 16, "check_text": "必须为4位字母", "evidence_page": "1"}],
                },
                {
                    "version": "V1",
                    "table_code": "T2",
                    "table_name": "表二",
                    "source_name": "dual-resume-test",
                    "section": "1.2",
                    "source_sha256": "sha_t2",
                    "terms_text": "",
                    "dictionary_text": "CODE_B 必须为4位数字",
                    "fields": [{"name": "CODE_B", "content": "编码B", "data_type": "VARCHAR2", "length": 16, "check_text": "必须为4位数字", "evidence_page": "2"}],
                },
            ],
        },
    )
    session.add(spec_doc)
    session.commit()

    fail_t2 = True
    rule_calls = {"T1": 0, "T2": 0}

    class PartialFailDualLlm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            trace = {"provider": "test", "model": "test", "elapsed_seconds": 0.1, "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70}}
            if tool_name == "assign_rules":
                if "tables" in payload:  # cross-table FK
                    return {"rules": [], "golden_overrides": {}}, trace
                tcode = payload.get("table_code")
                rule_calls[tcode] = rule_calls.get(tcode, 0) + 1
                if tcode == "T2" and fail_t2:
                    # 不可恢复错误直接穿透，触发同波次 T1 成功、T2 失败的部分波次中断
                    raise RuntimeError("LLM HTTP 401 simulated T2 failure")
                field_name = payload["target_field"]
                pattern = "^[A-Z]{4}$" if tcode == "T1" else "^[0-9]{4}$"
                return {
                    "rules": [
                        {
                            "rule_key": f"{tcode}_{field_name}_FMT",
                            "field": field_name,
                            "operator": "REGEXP",
                            "params": {"pattern": pattern},
                            "source": "AI_EXPANDED",
                            "rationale": "格式约束",
                        }
                    ]
                }, trace
            return {}, trace

    settings = Settings(blob_dir=str(tmp_path), oracle_sandbox_url="sqlite://")
    llm = PartialFailDualLlm()
    run = start_generic_run(session, spec_id, settings, llm=llm)
    assert project(session, run).status == "FAILED"
    assert rule_calls["T1"] == 1
    assert rule_calls["T2"] == 1

    # 关键断言 1：T1 已通过 partial_outcomes 在主线程持久化 TABLE_RULES_BOUND 检查点
    table_bound_events = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id, EventModel.kind == "TABLE_RULES_BOUND")
        .all()
    )
    assert [e.payload_json["table_code"] for e in table_bound_events] == ["T1"]

    # 恢复后重跑：T2 故障解除，断点续跑绝不重调 T1！
    fail_t2 = False
    retry_generic_run(session, run, settings, defer_continuation=True)
    drive(session, run, settings, llm=llm)

    assert project(session, run).status == "WAITING_APPROVAL"
    assert rule_calls["T1"] == 1, f"T1 应直接复用检查点，不应重复调用 LLM：{rule_calls}"
    assert rule_calls["T2"] == 2
    final_bound_tables = sorted(
        e.payload_json["table_code"]
        for e in session.query(EventModel)
        .filter(EventModel.run_id == run.id, EventModel.kind == "TABLE_RULES_BOUND")
        .all()
    )
    assert final_bound_tables == ["T1", "T2"]













