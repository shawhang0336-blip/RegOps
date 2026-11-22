"""Layer 5 generation/ 深度审计与程序-AI 边界改造回归测试。

覆盖全部 3 项程序-AI 边界改造 + 10 项生产级 Bug 修复（MySQL 8 / Oracle 23 / 并发取消 / 自愈 / 空跑 / 差异对比）。
"""

import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text

from app.delivery.production import compile_production_rule_query
from app.generation.core.binding import (
    GeneratorBinding,
    SchemaBinding,
    find_golden_rule_violations,
    materialize_golden_record,
    violates_single_field_rule,
)
from app.generation.core.mutations import build_deterministic_mutations
from app.generation.core.planning import derive_generation_plan, needs_semantic_rule_binding
from app.generation.core.rule_backfill import (
    _backfill_fixed_value_rules,
    _backfill_length_rules,
    _backfill_required_rules,
)
from app.generation.core.rules import posix_classes_to_python
from app.generation.run_diff import diff_runs
from app.generation.sql.code_set_sql import (
    code_set_value_rows,
    compile_code_set_mysql_ddl,
    compile_code_set_mysql_upsert,
    compile_code_set_oracle_insert,
    stable_code_set_table,
)
from app.generation.sql.compiler import compile_where
from app.generation.sql.oracle import GenericOracleExecutor, OracleSandbox, physical_table_name
from app.generation.workflow.entrypoints import retry_generic_run
from app.generation.workflow.runtime import _emit, _upsert_rule
from app.generation.workflow.stage_audit import bind_table_audit_worker
from app.generation.workflow.stage_data import bind_table_data_worker
from app.generation.workflow.stage_plan_schema import _plan_stage
from app.generation.workflow.stage_rules import (
    _ensure_oracle_assets,
    _rebind_failed_rules,
    bind_table_rules_worker,
    join_table_rules_barrier,
    prepare_deterministic_rule_index,
)
from app.infra.blob_store import LocalBlobStore
from app.infra.config import Settings
from app.infra.contracts import AuditCaseBinding, Evidence, FieldSpec, RuleDefinition, SpecDocument
from app.infra.db_tables import (
    ArtifactModel,
    CodeSetModel,
    CodeValueModel,
    EventModel,
    FieldCodeReferenceModel,
    FieldMetadataModel,
    GenerationPlanModel,
    RuleModel,
    RunModel,
    SourceDocumentModel,
    SpecificationModel,
    TableMetadataModel,
)
from app.infra.errors import WorkflowError
from app.infra.llm.guard import current_cancellation, llm_execution_scope
from app.infra.llm.schemas import tool_prompt_preamble
from app.kernel.event_log import append_event
from app.kernel.run import CancellationToken, RunCancelledError
from .helpers import make_session


def _make_spec(table_code="T_AUDIT", fields=None, terms_text="") -> SpecDocument:
    return SpecDocument(
        version="V1",
        table_code=table_code,
        table_name="审计测试表",
        source_name="spec.pdf",
        source_sha256="a" * 64,
        section="6.1",
        fields=fields
        or [
            FieldSpec(name="PLAIN_REQ", content="普通必填", data_type="VARCHAR", length=32, check_text="必填项。", evidence_page="1"),
            FieldSpec(name="REMARK", content="备注", data_type="VARCHAR", length=128, check_text="非必填项。", evidence_page="1"),
            FieldSpec(name="TXCCY", content="币种", data_type="VARCHAR", length=3, check_text="必填项，见币种代码表。", evidence_page="1"),
            FieldSpec(name="COND_COL", content="英文名", data_type="VARCHAR", length=64, check_text="当常驻国为非中国时必填。", evidence_page="1"),
            FieldSpec(name="TXAMT", content="交易金额", data_type="NUMBER", length=22, decimal_places=2, check_text="必填项，大于0。", evidence_page="1"),
            FieldSpec(name="BIZ_DATE", content="业务日期", data_type="VARCHAR", length=8, check_text="非必填项。", evidence_page="1"),
        ],
        terms_text=terms_text,
        dictionary_text="",
    )


def _rule(table_code: str, key: str, field: str, op: str, params: dict, source: str = "DOC_EXPLICIT", rationale: str = "test") -> RuleDefinition:
    return RuleDefinition(
        rule_key=key,
        table_code=table_code,
        field=field,
        operator=op,
        params=params,
        source=source,
        evidence=Evidence(section="6.1.3", page="1", field=field, quote="test"),
        rationale=rationale,
        rule_hash=uuid.uuid4().hex + uuid.uuid4().hex,
    )


# ── 1. 程序 vs AI 边界改造 ────────────────────────────────────────────────────


def test_deterministic_generation_plan_zero_llm_calls_and_unique_hint():
    session = make_session()
    run = RunModel(id=str(uuid.uuid4()), spec_id=str(uuid.uuid4()))
    session.add(run)
    session.flush()

    spec_with_unique = _make_spec(
        fields=[
            FieldSpec(name="RPTNO", content="申报号", data_type="VARCHAR", length=22, check_text="必填项，全表唯一不可重复。", evidence_page="1"),
            FieldSpec(name="REMARK", content="备注", data_type="VARCHAR", length=64, check_text="非必填项。", evidence_page="1"),
        ]
    )

    class FailIfCalledLlm:
        def assign(self, *args, **kwargs):
            raise AssertionError("确定性规划绝不应调用 LLM")

    budget = {"max_rules_per_table": 20, "max_cases_per_table": 50}
    plans = _plan_stage(session, run, [spec_with_unique], FailIfCalledLlm(), budget)
    assert plans["T_AUDIT"]["golden_count"] == 2
    assert plans["T_AUDIT"]["mutations_per_rule"] >= 1
    assert session.query(GenerationPlanModel).filter_by(run_id=run.id).count() == 1
    assert derive_generation_plan(_make_spec(), {"max_rules_per_table": 2, "max_cases_per_table": 3})["golden_count"] == 1


def test_semantic_rule_gate_skips_plain_fields_and_keeps_semantic_fields():
    spec = _make_spec()
    det_rules = [
        _rule("T_AUDIT", "T_AUDIT_PLAIN_REQ_REQUIRED", "PLAIN_REQ", "REQUIRED", {}),
        _rule("T_AUDIT", "T_AUDIT_TXCCY_REQUIRED", "TXCCY", "REQUIRED", {}),
        _rule("T_AUDIT", "T_AUDIT_TXCCY_CODE_SET_ENUM", "TXCCY", "ENUM", {"values": ["CNY", "USD"]}),
    ]
    det_index = prepare_deterministic_rule_index(det_rules)

    called_fields = []

    class RecordingLlm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            called_fields.append(payload["target_field"])
            return {"rules": []}, {"provider": "test", "model": "test"}

    _, trace = bind_table_rules_worker(
        spec,
        llm=RecordingLlm(),
        plans=None,
        field_code_sets={},
        det_index=det_index,
        concurrency=1,
    )
    # PLAIN_REQ / REMARK / TXCCY（已被 REQUIRED + CODE_SET_ENUM 完整覆盖）直接跳过 AI
    assert set(called_fields) == {"COND_COL", "TXAMT", "BIZ_DATE"}
    assert trace["skipped_deterministic_count"] == 3
    assert trace["ai_field_count"] == 3

    # 如果 terms_text 提到了 REMARK，则不应跳过
    spec_with_term = _make_spec(terms_text="REMARK 在业务类型为其他时必须填写说明。")
    needs_ai, _ = needs_semantic_rule_binding(spec_with_term, spec_with_term.fields[1], [])
    assert needs_ai is True


def test_deterministic_data_generator_solves_uncoupled_num_range_and_unique_key():
    spec = _make_spec(
        fields=[
            FieldSpec(name="PK_ID", content="流水主键", data_type="VARCHAR", length=20, check_text="必填", evidence_page="1"),
            FieldSpec(name="RATE", content="利率", data_type="NUMBER", length=10, decimal_places=2, check_text="必填", evidence_page="1"),
            FieldSpec(name="NOTE", content="备注", data_type="VARCHAR", length=32, check_text="非必填", evidence_page="1"),
        ]
    )
    rules = [
        _rule("T_AUDIT", "R_PK_UQ", "PK_ID", "UNIQUE_KEY", {"key_fields": ["PK_ID"]}),
        _rule("T_AUDIT", "R_RATE_RANGE", "RATE", "NUM_RANGE", {"min": 0, "min_inclusive": False, "max": 100, "max_inclusive": False}),
        _rule("T_AUDIT", "R_RATE_NOT_IN", "RATE", "NOT_IN", {"values": ["50"]}),
    ]

    class FailIfCalledLlm:
        def assign(self, *args, **kwargs):
            raise AssertionError("独立 NUM_RANGE 与单列 UNIQUE_KEY 应 0 LLM 求解")

    generators, trace = bind_table_data_worker(
        spec,
        llm=FailIfCalledLlm(),
        active_rules=rules,
        golden_overrides={},
        field_code_sets={},
        plans={"T_AUDIT": {"golden_count": 3}},
    )
    assert trace["ai_field_count"] == 0
    assert trace["provider"] == "DETERMINISTIC"
    rec0 = materialize_golden_record(spec, generators, 0)
    rec1 = materialize_golden_record(spec, generators, 1)
    rec2 = materialize_golden_record(spec, generators, 2)
    assert len({rec0["PK_ID"], rec1["PK_ID"], rec2["PK_ID"]}) == 3
    assert 0 < float(rec0["RATE"]) < 100 and str(rec0["RATE"]) != "50"
    assert find_golden_rule_violations(spec, rules, rec0, generators, golden_count=3) == []


# ── 2. P0 Bug 修复回归 ────────────────────────────────────────────────────────


def test_inner_thread_pools_preserve_cancellation_contextvar():
    spec = _make_spec(
        fields=[
            FieldSpec(name=f"AMT_{i}", content=f"金额{i}", data_type="NUMBER", length=12, decimal_places=2, check_text="大于0", evidence_page="1")
            for i in range(4)
        ]
    )
    token = CancellationToken()
    seen_tokens = []

    class CancelAwareRuleLlm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            seen_tokens.append(current_cancellation())
            token.cancel("operator clicked stop")
            token.raise_if_cancelled()
            return {"rules": []}, {"provider": "test", "model": "test"}

    with llm_execution_scope(cancellation=token):
        with pytest.raises(RunCancelledError):
            bind_table_rules_worker(
                spec,
                llm=CancelAwareRuleLlm(),
                plans=None,
                field_code_sets={},
                det_index=prepare_deterministic_rule_index([]),
                concurrency=4,
                outer_workers=1,
            )
    assert seen_tokens and all(tok is token for tok in seen_tokens)

    # 同步验证 audit_table_worker 内层线程池也继承 cancellation contextvar
    token2 = CancellationToken()
    regex_rules = [
        _rule("T_AUDIT", f"R_REG_{i}", f"AMT_{i}", "REGEXP", {"pattern": "^[0-9]+$"})
        for i in range(4)
    ]
    seen_audit_tokens = []

    class CancelAwareAuditLlm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            seen_audit_tokens.append(current_cancellation())
            token2.cancel("stop audit")
            token2.raise_if_cancelled()
            return {"cases": []}, {"provider": "test", "model": "test"}

    with llm_execution_scope(cancellation=token2):
        with pytest.raises(RunCancelledError):
            bind_table_audit_worker(
                spec,
                llm=CancelAwareAuditLlm(),
                active_rules=regex_rules,
                golden_record={f"AMT_{i}": 10 for i in range(4)},
                field_code_sets={},
                concurrency=4,
                outer_workers=1,
            )
    assert seen_audit_tokens and all(tok is token2 for tok in seen_audit_tokens)


def test_cond_value_prompt_contract_matches_validator_and_compiler():
    prompt = tool_prompt_preamble("assign_rules")
    assert "COND_VALUE={if_field,if_operator:EQ|NE,if_values:[...],then_operator:MUST_BE_NULL|ENUM|NOT_IN|NUM_RANGE,then_values/min/max}" in prompt
    assert "if_operator:EQ|NE|IN|NOT_IN" not in prompt
    assert "then_operator:ENUM|NOT_IN|REGEXP|LENGTH_MAX" not in prompt


def test_rejected_mechanical_rules_are_never_resurrected_by_backfill():
    session = make_session()
    run = RunModel(id=str(uuid.uuid4()), spec_id=str(uuid.uuid4()))
    session.add(run)
    session.flush()

    spec = _make_spec(
        fields=[
            FieldSpec(name="F_REQ", content="必填字段", data_type="VARCHAR", length=16, check_text="必填项。", evidence_page="1"),
            FieldSpec(name="F_FIX", content="固定字段", data_type="VARCHAR", length=8, check_text="本项固定为CHN。", evidence_page="1"),
        ]
    )
    rejected = [
        _rule("T_AUDIT", "T_AUDIT_F_REQ_REQUIRED", "F_REQ", "REQUIRED", {}),
        _rule("T_AUDIT", "T_AUDIT_F_REQ_LENGTH_MAX", "F_REQ", "LENGTH_MAX", {"length": 16}),
        _rule("T_AUDIT", "T_AUDIT_F_FIX_FIXED_VALUE_ENUM", "F_FIX", "ENUM", {"values": ["CHN"]}),
    ]
    out = _backfill_required_rules(session, run, [spec], [], _upsert_rule, _emit, rejected_rules=rejected)
    out = _backfill_length_rules(session, run, [spec], out, _upsert_rule, _emit, rejected_rules=rejected)
    out = _backfill_fixed_value_rules(session, run, [spec], out, _upsert_rule, _emit, rejected_rules=rejected)
    # 仅未被拒的 F_FIX LENGTH_MAX 会被补齐，3 条被人工 REJECTED 的机械规则绝不复活
    assert [(r.field, r.operator) for r in out] == [("F_FIX", "LENGTH_MAX")]


def test_oracle_dry_run_attributes_failed_rule_and_cache_invalidates_idempotently(tmp_path):
    spec = _make_spec(
        fields=[
            FieldSpec(name="CODE", content="编码", data_type="VARCHAR", length=10, check_text="必填", evidence_page="1"),
        ]
    )
    schemas = {"T_AUDIT": [SchemaBinding(field="CODE", storage_type="TEXT", length=10, decimal_places=0)]}
    # 在 SQLite 沙箱里 REGEXP_LIKE 不存在，用于验证空跑出错时精准归因到 rule_key / field / sql
    bad_rule = _rule("T_AUDIT", "R_BAD_REGEX", "CODE", "REGEXP", {"pattern": "^[0-9]{4}$"})
    db_url = f"sqlite:///{tmp_path / 'dry_run.db'}"
    with pytest.raises(WorkflowError) as exc_info:
        GenericOracleExecutor(db_url).prepare_assets("run-dry-1", 1, [spec], schemas, rules=[bad_rule])
    ctx = exc_info.value.context
    assert ctx["error_kind"] == "oracle_precheck"
    assert ctx["rule_key"] == "R_BAD_REGEX"
    assert ctx["table_code"] == "T_AUDIT"
    assert ctx["field"] == "CODE"
    assert "REGEXP_LIKE" in ctx["sql"]

    # 验证 _ensure_oracle_assets 在规则集合变更时失效缓存，且 ArtifactModel 按 (run_id, artifact_type, version) 幂等更新
    session = make_session()
    run = RunModel(id=str(uuid.uuid4()), spec_id=str(uuid.uuid4()))
    session.add(run)
    session.flush()
    settings = Settings(blob_dir=str(tmp_path / "blobs"), oracle_sandbox_url=db_url)
    schema_payload = {"T_AUDIT": [item.model_dump() for item in schemas["T_AUDIT"]]}
    r1 = _rule("T_AUDIT", "R_CODE_LEN", "CODE", "LENGTH_MAX", {"length": 10})
    r2 = _rule("T_AUDIT", "R_CODE_ENUM", "CODE", "ENUM", {"values": ["A", "B"]})

    _ensure_oracle_assets(session, run, [spec], schema_payload, [r1], settings, attempt=1)
    first_sql_uri = (
        session.query(ArtifactModel).filter_by(run_id=run.id, artifact_type="VALIDATION_SQL", version=1).one().blob_uri
    )
    # 规则新增一条后同 attempt=1 再调：必须重跑并原地更新 ArtifactModel，绝不写 2 行重复记录
    _ensure_oracle_assets(session, run, [spec], schema_payload, [r1, r2], settings, attempt=1)
    all_sql_arts = session.query(ArtifactModel).filter_by(run_id=run.id, artifact_type="VALIDATION_SQL", version=1).all()
    assert len(all_sql_arts) == 1
    assert all_sql_arts[0].blob_uri != first_sql_uri
    compiled_queries = json.loads(LocalBlobStore(settings.blob_dir).get(all_sql_arts[0].blob_uri).decode("utf-8"))["queries"]
    assert {q["rule_key"] for q in compiled_queries} == {"R_CODE_LEN", "R_CODE_ENUM"}


def test_mysql8_and_oracle23_sql_compilation_precision_and_code_set_ddl():
    r_len = _rule("T_AUDIT", "R_LEN", "REMARK", "LENGTH_MAX", {"length": 10})
    mysql_len, _ = compile_where(r_len, "mysql")
    oracle_len, _ = compile_where(r_len, "oracle")
    assert "CHAR_LENGTH(" in mysql_len
    assert "LENGTH(" in oracle_len and "CHAR_LENGTH(" not in oracle_len

    r_re = _rule("T_AUDIT", "R_RE", "TXCCY", "REGEXP", {"pattern": "^[A-Z]{3}$"})
    mysql_re, _ = compile_where(r_re, "mysql")
    assert "NOT REGEXP_LIKE(`t`.`TXCCY`, :expected_pattern, 'c')" in mysql_re or "NOT REGEXP_LIKE(t.`TXCCY`, :expected_pattern, 'c')" in mysql_re

    # 18 位金融大额金额（NUMBER(22,2)）必须保留精确 Decimal，不可 float 舍入
    big_amt = "12345678901234567.89"
    r_amt = _rule("T_AUDIT", "R_BIG", "TXAMT", "NUM_RANGE", {"min": "0.01", "max": big_amt})
    _, params = compile_where(r_amt, "oracle")
    assert params["range_max"] == Decimal(big_amt)
    prod_sql = compile_production_rule_query(r_amt, "oracle")
    assert big_amt in prod_sql

    # 码表表名严守 MySQL 8 的 64 字符上限 + MySQL 8 DDL/UPSERT + Oracle 200 行分批 INSERT ALL + 空 label_cn 兜底
    long_code = "SAFE_VERY_LONG_ADMINISTRATIVE_DIVISION_STANDARD_CODE_SET_2026_EXTENDED_EDITION"
    tbl = stable_code_set_table(long_code)
    assert len(tbl) <= 64
    mysql_ddl = compile_code_set_mysql_ddl(tbl)
    assert "CREATE TABLE IF NOT EXISTS" in mysql_ddl and "utf8mb4_bin" in mysql_ddl

    large_cs = {
        "code": "BIG_CS",
        "values": [{"value": f"V{i:04d}", "label_cn": "" if i == 0 else f"名称{i}"} for i in range(450)],
    }
    rows = code_set_value_rows(large_cs)
    assert rows[0]["LABEL_CN"] == "V0000"  # 空 label_cn 回落为码值，防 Oracle ORA-01400
    insert_sql = compile_code_set_oracle_insert(large_cs, tbl)
    assert insert_sql.count("INSERT ALL") == 3  # 200 + 200 + 50
    upsert_sql = compile_code_set_mysql_upsert(large_cs, tbl)
    assert upsert_sql.count("ON DUPLICATE KEY UPDATE") == 3


# ── 3. P1 Bug 修复回归 ────────────────────────────────────────────────────────


def test_oracle_sandbox_utf8_bytes_multidialect_and_multi_golden_validation(tmp_path):
    # UTF-8 字节截断：中文表名 60 字 = 180 字节，必须收敛到 ≤120 字节
    cn_name = physical_table_name("run-utf8-12345", 1, "外汇业务基础信息采集明细表" * 6)
    assert len(cn_name.encode("utf-8")) <= 120

    # SQLite 沙箱验证 preview / drop_tables 跨方言不炸 + golden_count=2 时真跑 GOLDEN_BASELINE_2 查出重复 UNIQUE_KEY
    db_url = f"sqlite:///{tmp_path / 'sandbox.db'}"
    spec = _make_spec(
        fields=[
            FieldSpec(name="PK_ID", content="主键", data_type="VARCHAR", length=16, check_text="唯一", evidence_page="1"),
            FieldSpec(name="VAL", content="值", data_type="VARCHAR", length=16, check_text="必填", evidence_page="1"),
        ]
    )
    schemas = {
        "T_AUDIT": [
            SchemaBinding(field="PK_ID", storage_type="TEXT", length=16, decimal_places=0),
            SchemaBinding(field="VAL", storage_type="TEXT", length=16, decimal_places=0),
        ]
    }
    uq_rule = _rule("T_AUDIT", "R_PK_UQ", "PK_ID", "UNIQUE_KEY", {"key_fields": ["PK_ID"]})
    # 故意给 PK_ID 配 FIXED 生成器 + golden_count=2，验证沙箱真实检出 GOLDEN_BASELINE 与 GOLDEN_BASELINE_2 主键冲突
    bad_gens = {
        "T_AUDIT": [
            GeneratorBinding(field="PK_ID", generator="FIXED", params={"value": "SAME_PK"}),
            GeneratorBinding(field="VAL", generator="FIXED", params={"value": "OK"}),
        ]
    }
    cases = {"T_AUDIT": [AuditCaseBinding(case_id="GOLDEN_BASELINE", overrides={}, expected_rule_keys=[])]}
    executor = GenericOracleExecutor(db_url)
    report = executor.execute(
        "run-mg-1",
        1,
        [spec],
        schemas,
        {"T_AUDIT": {"PK_ID": "SAME_PK", "VAL": "OK"}},
        cases,
        [uq_rule],
        golden_counts={"T_AUDIT": 2},
        golden_generators=bad_gens,
    )
    assert report["all_passed"] is False
    golden_ids = [c["case_id"] for c in report["cases"] if c["case_kind"] == "GOLDEN"]
    assert golden_ids == ["GOLDEN_BASELINE", "GOLDEN_BASELINE_2"]
    assert all("R_PK_UQ" in c["unexpected_rules"] for c in report["cases"])

    sandbox = OracleSandbox(db_url)
    preview_rows = sandbox.preview(report["physical_tables"]["T_AUDIT"], limit=5)
    assert len(preview_rows) == 2
    assert sandbox.drop_tables([report["physical_tables"]["T_AUDIT"], report["target_tables"]["T_AUDIT"]]) == []


def _seed_published_code_set(session, spec: SpecDocument, field_name: str, code: str, values: list):
    doc = SourceDocumentModel(
        id=str(uuid.uuid4()),
        file_name=spec.source_name,
        file_type="PDF",
        sha256=spec.source_sha256,
        blob_uri=f"blob://{spec.source_sha256}",
        spec_version=spec.version,
        status="READY",
        block_count=1,
    )
    session.add(doc)
    session.flush()
    tbl = TableMetadataModel(
        id=str(uuid.uuid4()),
        document_id=doc.id,
        block_id=str(uuid.uuid4()),
        table_code=spec.table_code,
        table_name=spec.table_name,
        table_definition="",
        status="PUBLISHED",
        current_version=1,
        published_version=1,
    )
    session.add(tbl)
    session.flush()
    fm = FieldMetadataModel(
        id=str(uuid.uuid4()),
        table_id=tbl.id,
        field_cn=field_name,
        field_en=field_name,
        field_definition="",
        check_text_raw="",
        data_type="VARCHAR",
        length=16,
        decimal_places=0,
        required=True,
        code_reference_text="",
        confidence=1.0,
        evidence_json={},
        sort_order=0,
        ai_value_json={},
    )
    cs = CodeSetModel(
        id=str(uuid.uuid4()),
        code=code,
        name=code,
        definition="",
        source_type="DOCUMENT_TABLE",
        status="PUBLISHED",
        current_version=1,
        published_version=1,
    )
    session.add_all([fm, cs])
    session.flush()
    for idx, (val, label) in enumerate(values):
        session.add(
            CodeValueModel(
                id=str(uuid.uuid4()),
                code_set_id=cs.id,
                value=val,
                label_cn=label,
                label_en="",
                definition="",
                sort_order=idx,
                enabled=True,
                source_locator={},
            )
        )
    session.add(
        FieldCodeReferenceModel(
            id=str(uuid.uuid4()),
            field_id=fm.id,
            code_set_id=cs.id,
            reference_text=code,
            status="BOUND",
            condition_json={},
            confidence=1.0,
        )
    )
    session.flush()


def test_rebind_failed_rules_passes_code_set_handles_fk_and_missing_field():
    session = make_session()
    parent = _make_spec(
        "T_PARENT",
        fields=[FieldSpec(name="PID", content="主表ID", data_type="VARCHAR", length=16, check_text="必填", evidence_page="1")],
    )
    child = _make_spec(
        "T_CHILD",
        fields=[
            FieldSpec(name="REF_PID", content="关联主表ID", data_type="VARCHAR", length=16, check_text="关联T_PARENT.PID", evidence_page="1"),
            FieldSpec(name="CCY", content="币种", data_type="VARCHAR", length=3, check_text="见币种代码表", evidence_page="1"),
        ],
    )
    _seed_published_code_set(session, child, "CCY", "CS_CCY_REBIND", [("CNY", "人民币"), ("USD", "美元")])
    run = RunModel(id=str(uuid.uuid4()), spec_id=str(uuid.uuid4()))
    session.add(run)
    session.flush()

    r_fk = _rule("T_CHILD", "R_FK_BAD", "REF_PID", "FOREIGN_KEY", {"references_table": "T_PARENT", "references_field": "PID"})
    r_enum = _rule("T_CHILD", "R_CCY_ENUM", "CCY", "ENUM", {"values": ["CNY"]})
    r_ghost = _rule("T_CHILD", "R_GHOST", "NO_SUCH_FIELD", "ENUM", {"values": ["X"]})
    for r in (r_fk, r_enum, r_ghost):
        _upsert_rule(session, run, r, "APPROVED")

    seen_payloads = {}

    class RebindLlm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            cur = payload["current_rule"]
            seen_payloads[cur["rule_key"]] = payload
            if cur["rule_key"] == "R_CCY_ENUM":
                args = {
                    "rules": [
                        {
                            "rule_key": "R_CCY_ENUM",
                            "field": "CCY",
                            "operator": "ENUM",
                            "params": {"values": ["CNY", "USD"]},
                            "evidence_kind": "DICTIONARY",
                            "rationale": "updated from code set",
                        }
                    ]
                }
            else:
                args = {
                    "rules": [
                        {
                            "rule_key": "R_FK_BAD",
                            "field": "REF_PID",
                            "operator": "FOREIGN_KEY",
                            "params": {"references_table": "T_PARENT", "references_field": "PID"},
                            "evidence_kind": "TERM_INFERENCE",
                            "rationale": "rebound fk",
                        }
                    ]
                }
            return args, {"provider": "test", "model": "test"}

    report = {
        "cases": [
            {"table_code": "T_CHILD", "case_id": "M1", "missing_rules": ["R_CCY_ENUM", "R_FK_BAD", "R_GHOST"]},
        ]
    }
    events = []
    changed = _rebind_failed_rules(session, run, [parent, child], RebindLlm(), report, events=events)
    assert changed == 2
    assert "published_code_set" in seen_payloads["R_CCY_ENUM"]
    assert set(seen_payloads["R_CCY_ENUM"]["published_code_set"]["values"]) == {"CNY", "USD"}
    assert "T_PARENT" in seen_payloads["R_FK_BAD"]["table_codes"]
    assert any(kind == "RULE_REBIND_GAP" and payload["rule_key"] == "R_GHOST" for kind, _, payload in events)


def test_retry_generic_run_three_way_routing(tmp_path):
    session = make_session()
    spec = _make_spec()
    spec_id = str(uuid.uuid4())
    session.add(
        SpecificationModel(
            id=spec_id,
            version="V1",
            table_code="SNAP",
            source_name="spec.pdf",
            source_sha256="a" * 64,
            content_json={"selection_confirmed": True, "tables": [spec.model_dump()]},
        )
    )
    session.flush()
    settings = Settings(blob_dir=str(tmp_path / "blobs"))
    blobs = LocalBlobStore(settings.blob_dir)

    # 场景 A：黄金误报（missing_rules 为空）→ 路由到 data_generator_binder，不再报错卡死
    run_a = RunModel(id=str(uuid.uuid4()), spec_id=spec_id)
    session.add(run_a)
    session.flush()
    uri_a = blobs.put(json.dumps({"cases": [{"case_kind": "GOLDEN", "unexpected_rules": ["R1"], "missing_rules": []}]}).encode())
    append_event(session, run_a, "RUN_CREATED", "created", {})
    append_event(session, run_a, "AUDIT_REJECTED", "oracle_executor", {"result": {"audit_report_blob": uri_a}})
    session.commit()
    retry_generic_run(session, run_a, settings, defer_continuation=True)
    last_a = session.query(EventModel).filter_by(run_id=run_a.id, kind="GO_REDIRECT").order_by(EventModel.seq.desc()).first()
    assert last_a.payload_json["target"] == "data_generator_binder"

    # 场景 B：仅机械规则漏报 → 路由到 audit_case_binder，不削弱规则
    run_b = RunModel(id=str(uuid.uuid4()), spec_id=spec_id)
    session.add(run_b)
    session.flush()
    mech_rule = _rule("T_AUDIT", "T_AUDIT_PLAIN_REQ_REQUIRED", "PLAIN_REQ", "REQUIRED", {}, rationale="由元数据机械补齐，非 AI 生成")
    _upsert_rule(session, run_b, mech_rule, "APPROVED")
    uri_b = blobs.put(
        json.dumps({"cases": [{"case_kind": "MUTATION", "unexpected_rules": [], "missing_rules": ["T_AUDIT_PLAIN_REQ_REQUIRED"]}]}).encode()
    )
    append_event(session, run_b, "RUN_CREATED", "created", {})
    append_event(session, run_b, "AUDIT_REJECTED", "oracle_executor", {"result": {"audit_report_blob": uri_b}})
    session.commit()
    retry_generic_run(session, run_b, settings, defer_continuation=True)
    last_b = session.query(EventModel).filter_by(run_id=run_b.id, kind="GO_REDIRECT").order_by(EventModel.seq.desc()).first()
    assert last_b.payload_json["target"] == "audit_case_binder"


def test_core_decimal_sequence_posix_regexp_and_composite_unique_key_mutation():
    spec = _make_spec(
        fields=[
            FieldSpec(name="RATE", content="比率", data_type="NUMBER", length=10, decimal_places=2, check_text="必填", evidence_page="1"),
            FieldSpec(name="K1", content="键1", data_type="VARCHAR", length=16, check_text="必填", evidence_page="1"),
            FieldSpec(name="K2", content="键2", data_type="VARCHAR", length=16, check_text="选填", evidence_page="1"),
        ]
    )
    gens = [GeneratorBinding(field="RATE", generator="SEQUENCE", params={"start": 0.1, "step": 0.2})]
    assert materialize_golden_record(spec, gens, 1)["RATE"] == "0.3"

    # Oracle POSIX 字符类预判与 Oracle REGEXP_LIKE 对齐
    assert posix_classes_to_python("^[[:digit:]]{8}$") == "^[0-9]{8}$"
    assert violates_single_field_rule("REGEXP", {"pattern": "^[[:digit:]]{8}$"}, "20260101") is False
    assert violates_single_field_rule("REGEXP", {"pattern": "^[[:digit:]]{8}$"}, "ABCD1234") is True

    # 复合 UNIQUE_KEY(K1, K2) 在黄金 K2=None 时仍为两条 DUP 样本填上相同非空 K2
    uq_rule = _rule("T_AUDIT", "R_COMP_UQ", "K1", "UNIQUE_KEY", {"key_fields": ["K1", "K2"]})
    cases = build_deterministic_mutations(spec, uq_rule, {"K1": "A1", "K2": None})
    assert len(cases) == 2
    assert cases[0].overrides["K1"] == cases[1].overrides["K1"]
    assert cases[0].overrides["K2"] and cases[0].overrides["K2"] == cases[1].overrides["K2"]


def test_run_diff_uses_code_set_subquery_and_preserves_duplicate_rule_keys():
    session = make_session()
    spec = _make_spec(
        fields=[FieldSpec(name="CCY", content="币种", data_type="VARCHAR", length=3, check_text="见币种代码表", evidence_page="1")]
    )
    spec_id = str(uuid.uuid4())
    session.add(
        SpecificationModel(
            id=spec_id,
            version="V1",
            table_code="SNAP_DIFF",
            source_name="spec.pdf",
            source_sha256="a" * 64,
            content_json={"selection_confirmed": True, "tables": [spec.model_dump()]},
        )
    )
    _seed_published_code_set(session, spec, "CCY", "CS_DIFF_CCY", [("CNY", "人民币"), ("USD", "美元")])
    r1_run = RunModel(id=str(uuid.uuid4()), spec_id=spec_id)
    r2_run = RunModel(id=str(uuid.uuid4()), spec_id=spec_id)
    session.add_all([r1_run, r2_run])
    session.flush()

    enum_rule = _rule("T_AUDIT", "R_CCY_ENUM", "CCY", "ENUM", {"values": ["CNY", "USD"]})
    dup_a = _rule("T_AUDIT", "R_SAME_KEY", "CCY", "NOT_IN", {"values": ["XXX"]})
    dup_b = _rule("T_AUDIT", "R_SAME_KEY", "CCY", "NOT_IN", {"values": ["YYY"]})
    for r in (enum_rule, dup_a, dup_b):
        _upsert_rule(session, r1_run, r, "APPROVED")
        _upsert_rule(session, r2_run, r, "APPROVED")
    session.commit()

    diff = diff_runs(session, r1_run.id, r2_run.id)
    assert diff["summary"] == {"added": 0, "removed": 0, "modified": 0, "unchanged": 3}

    # 修改 r2 中 ENUM 规则状态后触发 modified，检查编译后的 SQL 是生产码表 NOT EXISTS 子查询
    row = session.query(RuleModel).filter_by(run_id=r2_run.id, rule_key="R_CCY_ENUM").one()
    row.status = "REJECTED"
    session.commit()
    diff2 = diff_runs(session, r1_run.id, r2_run.id)
    assert diff2["summary"]["modified"] == 1
    assert "NOT EXISTS" in diff2["modified"][0]["base"]["sql"]
    assert "REG_CS_" in diff2["modified"][0]["base"]["sql"]


def test_cross_validator_rejects_bad_golden_override_field_and_type():
    session = make_session()
    p = _make_spec(
        "T_P",
        fields=[FieldSpec(name="AMT", content="金额", data_type="NUMBER", length=12, decimal_places=2, check_text="必填", evidence_page="1")],
    )
    c = _make_spec(
        "T_C",
        fields=[FieldSpec(name="REF", content="关联", data_type="VARCHAR", length=12, check_text="必填", evidence_page="1")],
    )
    run = RunModel(id=str(uuid.uuid4()), spec_id=str(uuid.uuid4()))
    session.add(run)
    session.flush()

    attempts = {"n": 0}

    class BadOverrideLlm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return {"rules": [], "golden_overrides": {"T_P": {"AMT": "NOT_A_NUM"}}}, {"provider": "t", "model": "t"}
            return {"rules": [], "golden_overrides": {"T_P": {"AMT": "100.00"}}}, {"provider": "t", "model": "t"}

    _, overrides = join_table_rules_barrier(
        session,
        run,
        [p, c],
        BadOverrideLlm(),
        {"T_P": {"rules": [], "gaps": []}, "T_C": {"rules": [], "gaps": []}},
    )
    assert attempts["n"] == 2
    assert overrides == {"T_P": {"AMT": "100.00"}}
