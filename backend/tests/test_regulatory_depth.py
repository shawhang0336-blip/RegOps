"""监管业务深度能力回归测试：
1. 三大复杂金融监管算子全栈闭环（ARITHMETIC_BALANCE / COND_VALUE / UNIQUE_KEY）：
   - 领域模型与 Bus 字段白名单门禁拦截（terms[].field / target_field / key_fields / if_field）
   - 确定性参数校验（rules.py）与 Oracle/MySQL 双原生 SQL 编译（compiler.py）
   - Python 黄金样本自洽门禁与确定性反例变异构造（binding.py / mutations.py，0 Token）
   - SQLite 沙箱端到端执行（黄金零误报 + 变异 100% 精准命中，含 UNIQUE_KEY 隔离防污染）
2. 监管制度版本差异与影响面分析（version_diff.py + run_diff.py）：
   - 跨文档表/字段 Diff、影响等级分级（BREAKING / RULE_IMPACT / METADATA_ONLY / NONE）、受影响表清单；
   - 跨验证批次规则集合与编译后 Oracle SQL 差异对比。
"""

import uuid

import pytest

from app.catalog.version_diff import diff_documents
from app.generation.core.binding import (
    GeneratorBinding,
    SchemaBinding,
    bind_audit_cases,
    bind_rules,
    find_golden_rule_violations,
    violates_arithmetic_balance,
    violates_cond_value,
)
from app.delivery.run_diff import diff_runs
from app.delivery.testing.mutations import build_deterministic_mutations, build_golden_audit_case
from app.delivery.testing.sandbox_audit import GenericOracleExecutor
from app.delivery.testing.stage_data import _coupled_field_names, _partition_coupled_chunks
from app.generation.sql.compiler import compile_rule_query, compile_where
from app.generation.core.rules import RuleValidationError, validate_rule
from app.infra.db_tables import FieldMetadataModel, RawBlockModel, SourceDocumentModel, TableMetadataModel
from app.infra.contracts import FieldSpec, RuleDefinition, SpecDocument
from app.infra.db_tables import RuleModel, RunModel
from app.kernel.run import create_run
from app.policy.regops_bus import default_bus

from .helpers import make_session


def _account_balance_spec() -> SpecDocument:
    """贴近外管局 6.4.2 账户收支余信息 + 6.2.1 涉外收入的真实监管报表规格。"""
    return SpecDocument(
        version="SAFE_1.3",
        table_code="SAFE_BALANCE_DEMO",
        table_name="账户收支余与主体信息表",
        source_name="safe_demo.docx",
        source_sha256="sha_balance_demo",
        section="6.4.2",
        terms_text="期末余额 = 期初余额 + 收入金额 - 支出金额；对公客户证件号必空；申报号表内唯一。",
        dictionary_text="OPEN_BAL 期初余额; CREDIT_AMT 收入; DEBIT_AMT 支出; CLOSE_BAL 期末余额",
        fields=[
            FieldSpec(name="RPTNO", content="申报号码", data_type="VARCHAR", length=22, check_text="表内唯一，22位编号", evidence_page="1"),
            FieldSpec(name="CUSTYPE", content="主体类型", data_type="VARCHAR", length=1, check_text="C-对公，D-对私", evidence_page="1"),
            FieldSpec(name="IDCODE", content="个人身份证号", data_type="VARCHAR", length=32, check_text="对公时必须为空", evidence_page="1"),
            FieldSpec(name="TXCCY", content="币种", data_type="VARCHAR", length=3, check_text="境内业务必须为 CNY", evidence_page="1"),
            FieldSpec(name="IS_DOMESTIC", content="是否境内", data_type="VARCHAR", length=1, check_text="Y/N", evidence_page="1"),
            FieldSpec(name="OPEN_BAL", content="期初余额", data_type="NUMBER", length=22, decimal_places=2, check_text="数值", evidence_page="1"),
            FieldSpec(name="CREDIT_AMT", content="收入金额", data_type="NUMBER", length=22, decimal_places=2, check_text="数值", evidence_page="1"),
            FieldSpec(name="DEBIT_AMT", content="支出金额", data_type="NUMBER", length=22, decimal_places=2, check_text="数值", evidence_page="1"),
            FieldSpec(name="CLOSE_BAL", content="期末余额", data_type="NUMBER", length=22, decimal_places=2, check_text="等于期初+收入-支出", evidence_page="1"),
        ],
    )


def test_bus_adjudicator_blocks_hallucinated_fields_in_new_complex_operators():
    bus = default_bus()
    payload = {"field_names": ["RPTNO", "OPEN_BAL", "CREDIT_AMT", "DEBIT_AMT", "CLOSE_BAL"]}

    # ARITHMETIC_BALANCE 幻觉 term 字段拦截
    err1 = bus.adjudicate(
        "assign_rules",
        {
            "rules": [
                {
                    "field": "CLOSE_BAL",
                    "operator": "ARITHMETIC_BALANCE",
                    "params": {"terms": [{"field": "GHOST_AMT", "coef": 1}], "target_field": "CLOSE_BAL"},
                }
            ]
        },
        payload,
    )
    assert err1 and "GHOST_AMT" in err1

    # ARITHMETIC_BALANCE 幻觉 target_field 拦截
    err2 = bus.adjudicate(
        "assign_rules",
        {
            "rules": [
                {
                    "field": "OPEN_BAL",
                    "operator": "ARITHMETIC_BALANCE",
                    "params": {"terms": [{"field": "OPEN_BAL", "coef": 1}], "target_field": "FAKE_BAL"},
                }
            ]
        },
        payload,
    )
    assert err2 and "FAKE_BAL" in err2

    # UNIQUE_KEY 幻觉 key_fields 拦截
    err3 = bus.adjudicate(
        "assign_rules",
        {
            "rules": [
                {
                    "field": "RPTNO",
                    "operator": "UNIQUE_KEY",
                    "params": {"key_fields": ["RPTNO", "NO_SUCH_COL"]},
                }
            ]
        },
        payload,
    )
    assert err3 and "NO_SUCH_COL" in err3


def test_complex_operators_validation_sql_compilation_and_golden_guard():
    spec = _account_balance_spec()
    rules = bind_rules(
        spec,
        [
            {
                "rule_key": "R_BAL_EQ",
                "field": "CLOSE_BAL",
                "operator": "ARITHMETIC_BALANCE",
                "params": {
                    "terms": [
                        {"field": "OPEN_BAL", "coef": 1},
                        {"field": "CREDIT_AMT", "coef": 1},
                        {"field": "DEBIT_AMT", "coef": -1},
                    ],
                    "operator": "EQ",
                    "target_field": "CLOSE_BAL",
                    "tolerance": 0.01,
                },
                "evidence_kind": "DICTIONARY",
                "rationale": "期末余额等于期初加收入减支出",
            },
            {
                "rule_key": "R_CORP_NO_ID",
                "field": "IDCODE",
                "operator": "COND_VALUE",
                "params": {
                    "if_field": "CUSTYPE",
                    "if_operator": "EQ",
                    "if_values": ["C"],
                    "then_operator": "MUST_BE_NULL",
                },
                "evidence_kind": "DICTIONARY",
                "rationale": "对公客户个人身份证件号必须为空",
            },
            {
                "rule_key": "R_DOMESTIC_CNY",
                "field": "TXCCY",
                "operator": "COND_VALUE",
                "params": {
                    "if_field": "IS_DOMESTIC",
                    "if_operator": "EQ",
                    "if_values": ["Y"],
                    "then_operator": "ENUM",
                    "then_values": ["CNY"],
                },
                "evidence_kind": "DICTIONARY",
                "rationale": "境内业务币种必须为 CNY",
            },
            {
                "rule_key": "R_RPTNO_UNIQ",
                "field": "RPTNO",
                "operator": "UNIQUE_KEY",
                "params": {"key_fields": ["RPTNO"]},
                "evidence_kind": "DICTIONARY",
                "rationale": "申报号码表内唯一",
            },
        ],
    )
    assert len(rules) == 4

    # 校验 Oracle & MySQL 编译均生成合法带参数 SQL
    for rule in rules:
        o_sql, o_params = compile_rule_query(rule, "oracle", {spec.table_code: "T_ORACLE"})
        m_sql, m_params = compile_rule_query(rule, "mysql", {spec.table_code: "t_mysql"})
        assert "SELECT :compiled_rule_key AS rule_key" in o_sql
        assert "SELECT :compiled_rule_key AS rule_key" in m_sql
        assert o_params["compiled_rule_key"] == rule.rule_key
        assert m_params["compiled_rule_key"] == rule.rule_key

    # 非数值列禁止进入 ARITHMETIC_BALANCE
    with pytest.raises(RuleValidationError, match="NUMBER"):
        bind_rules(
            spec,
            [
                {
                    "rule_key": "R_BAD_BAL",
                    "field": "TXCCY",
                    "operator": "ARITHMETIC_BALANCE",
                    "params": {"terms": [{"field": "TXCCY", "coef": 1}], "target_value": 0},
                    "evidence_kind": "TERM_INFERENCE",
                    "rationale": "bad",
                }
            ],
        )

    # Python 黄金自洽门禁：不平衡公式 / 条件互斥冲突 / 多条黄金重复主键均被拦截
    bad_golden = {
        "RPTNO": "20260101000001",
        "CUSTYPE": "C",
        "IDCODE": "110101199001011234",  # 违反对公必空
        "TXCCY": "USD",  # 违反境内必须 CNY
        "IS_DOMESTIC": "Y",
        "OPEN_BAL": 1000,
        "CREDIT_AMT": 500,
        "DEBIT_AMT": 200,
        "CLOSE_BAL": 9999,  # 违反 1000+500-200 = 1300
    }
    violations = find_golden_rule_violations(spec, rules, bad_golden)
    violated_keys = {v["rule_key"] for v in violations}
    assert {"R_BAL_EQ", "R_CORP_NO_ID", "R_DOMESTIC_CNY"}.issubset(violated_keys)

    # 黄金样本多行时，FIXED 主键触发 UNIQUE_KEY 门禁拦截
    dup_gens = [
        GeneratorBinding(field="RPTNO", generator="FIXED", params={"value": "20260101000001"}),
        GeneratorBinding(field="CUSTYPE", generator="FIXED", params={"value": "C"}),
        GeneratorBinding(field="IDCODE", generator="FIXED", params={"value": ""}),
        GeneratorBinding(field="TXCCY", generator="FIXED", params={"value": "CNY"}),
        GeneratorBinding(field="IS_DOMESTIC", generator="FIXED", params={"value": "Y"}),
        GeneratorBinding(field="OPEN_BAL", generator="FIXED", params={"value": 1000}),
        GeneratorBinding(field="CREDIT_AMT", generator="FIXED", params={"value": 500}),
        GeneratorBinding(field="DEBIT_AMT", generator="FIXED", params={"value": 200}),
        GeneratorBinding(field="CLOSE_BAL", generator="FIXED", params={"value": 1300}),
    ]
    dup_violations = find_golden_rule_violations(spec, rules, {}, golden_generators=dup_gens, golden_count=2)
    assert any(v["rule_key"] == "R_RPTNO_UNIQ" for v in dup_violations)

    # 耦合连通分量测试：ARITHMETIC_BALANCE / COND_VALUE 关联字段必须打包在同一 chunk 内；
    # 单列无耦合 UNIQUE_KEY(RPTNO) 已下沉由程序确定性 SEQUENCE 求解，不再占用 AI 造数预算
    coupled = _coupled_field_names(spec, rules)
    assert {"OPEN_BAL", "CREDIT_AMT", "DEBIT_AMT", "CLOSE_BAL", "CUSTYPE", "IDCODE"}.issubset(coupled)
    assert "RPTNO" not in coupled
    chunks = _partition_coupled_chunks(spec.fields, rules, batch_size=4)
    bal_chunk = next(ch for ch in chunks if any(f.name == "CLOSE_BAL" for f in ch))
    assert {"OPEN_BAL", "CREDIT_AMT", "DEBIT_AMT", "CLOSE_BAL"}.issubset({f.name for f in bal_chunk})


def test_complex_operators_deterministic_mutations_and_sandbox_execution():
    """验证三大新算子 0 Token 确定性变异 + 真实 SQL 沙箱执行：黄金零误报、变异 100% 命中、覆盖率 100%。"""
    spec = _account_balance_spec()
    rules = bind_rules(
        spec,
        [
            {
                "rule_key": "R_BAL_EQ",
                "field": "CLOSE_BAL",
                "operator": "ARITHMETIC_BALANCE",
                "params": {
                    "terms": [
                        {"field": "OPEN_BAL", "coef": 1},
                        {"field": "CREDIT_AMT", "coef": 1},
                        {"field": "DEBIT_AMT", "coef": -1},
                    ],
                    "operator": "EQ",
                    "target_field": "CLOSE_BAL",
                    "tolerance": 0.01,
                },
                "evidence_kind": "DICTIONARY",
                "rationale": "收支余平衡",
            },
            {
                "rule_key": "R_CORP_NO_ID",
                "field": "IDCODE",
                "operator": "COND_VALUE",
                "params": {"if_field": "CUSTYPE", "if_operator": "EQ", "if_values": ["C"], "then_operator": "MUST_BE_NULL"},
                "evidence_kind": "DICTIONARY",
                "rationale": "对公证件号必空",
            },
            {
                "rule_key": "R_DOMESTIC_CNY",
                "field": "TXCCY",
                "operator": "COND_VALUE",
                "params": {"if_field": "IS_DOMESTIC", "if_operator": "EQ", "if_values": ["Y"], "then_operator": "ENUM", "then_values": ["CNY"]},
                "evidence_kind": "DICTIONARY",
                "rationale": "境内必用人民币",
            },
            {
                "rule_key": "R_RPTNO_UNIQ",
                "field": "RPTNO",
                "operator": "UNIQUE_KEY",
                "params": {"key_fields": ["RPTNO"]},
                "evidence_kind": "DICTIONARY",
                "rationale": "申报号唯一",
            },
        ],
    )
    golden = {
        "RPTNO": "20260101000001",
        "CUSTYPE": "C",
        "IDCODE": None,
        "TXCCY": "CNY",
        "IS_DOMESTIC": "Y",
        "OPEN_BAL": 1000.0,
        "CREDIT_AMT": 500.0,
        "DEBIT_AMT": 200.0,
        "CLOSE_BAL": 1300.0,
    }
    assert find_golden_rule_violations(spec, rules, golden) == []

    # 确定性构造所有变异用例（无需任何 LLM）
    active_keys = {r.rule_key for r in rules}
    rule_index = {r.rule_key: (r.field, r.operator, r.params) for r in rules}
    cases = [build_golden_audit_case()]
    for r in rules:
        muts = build_deterministic_mutations(spec, r, golden)
        assert muts, f"算子 {r.operator} 应产出确定性变异"
        cases.extend(bind_audit_cases(spec, [m.model_dump() for m in muts], active_keys, rule_index, golden_record=golden))

    # UNIQUE_KEY 应生成一对孤立重复键变异行
    dup_cases = [c for c in cases if "R_RPTNO_UNIQ" in c.expected_rule_keys]
    assert len(dup_cases) == 2
    assert dup_cases[0].overrides["RPTNO"] == dup_cases[1].overrides["RPTNO"]
    assert dup_cases[0].overrides["RPTNO"] != golden["RPTNO"]

    # 直接执行标准 ANSI SQL（在 SQLite 内存库中验证 `GenericOracleExecutor` 隔离与命中逻辑）
    schemas = {
        spec.table_code: [
            SchemaBinding(
                field=f.name,
                storage_type="NUMBER" if f.data_type == "NUMBER" else "TEXT",
                length=f.length,
                decimal_places=f.decimal_places or 0,
            )
            for f in spec.fields
        ]
    }
    # SQLite 不认识 VARCHAR2(4000 CHAR)，直接跑 compile_where 的标准 SQL 验证执行引擎
    from sqlalchemy import create_engine, text

    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        cols = ["record_id TEXT PRIMARY KEY"] + [
            f'"{f.name}" {"REAL" if f.data_type == "NUMBER" else "TEXT"}' for f in spec.fields
        ]
        conn.execute(text(f'CREATE TABLE "T_SANDBOX" ({", ".join(cols)})'))
        from app.delivery.testing.mutations import make_distinct_key_value

        for idx, case in enumerate(cases):
            rec = dict(golden)
            if "GOLDEN" not in case.case_id.upper() and "RPTNO" not in case.overrides:
                rec["RPTNO"] = make_distinct_key_value(rec["RPTNO"], spec.fields[0], idx + 1)
            rec.update(case.overrides)
            rec["record_id"] = case.case_id
            names = ["record_id"] + [f.name for f in spec.fields]
            conn.execute(
                text(f'INSERT INTO "T_SANDBOX" ({", ".join(chr(34)+n+chr(34) for n in names)}) VALUES ({", ".join(":"+n for n in names)})'),
                rec,
            )

        hits = {c.case_id: set() for c in cases}
        for rule in rules:
            sql, params = compile_rule_query(rule, "mysql", {spec.table_code: "T_SANDBOX"})
            sql = sql.replace("`", '"').replace("CHAR(9)", "char(9)").replace("CHAR(10)", "char(10)").replace("CHAR(13)", "char(13)")
            for row in conn.execute(text(sql), params).fetchall():
                hits[row[1]].add(row[0])

    # 黄金基线 0 误报，每条变异用例 100% 精准命中目标规则！
    assert hits["GOLDEN_BASELINE"] == set(), f"黄金基线误报：{hits['GOLDEN_BASELINE']}"
    for case in cases:
        if "GOLDEN" not in case.case_id.upper():
            for exp in case.expected_rule_keys:
                assert exp in hits[case.case_id], f"用例 {case.case_id} 未命中 {exp}，实际命中={hits[case.case_id]}"


def test_version_diff_documents_and_run_rules_diff():
    """验证跨版本监管文档 Diff（影响分级 + 增量受影响表清单）与跨批次规则/SQL Diff。"""
    session = make_session()

    def _seed_doc(version: str, tables_spec: list) -> SourceDocumentModel:
        doc = SourceDocumentModel(
            id=str(uuid.uuid4()),
            file_name=f"safe_{version}.docx",
            file_type="docx",
            sha256=uuid.uuid4().hex,
            blob_uri=f"blob://{version}",
            spec_version=version,
            status="EXTRACTED",
            block_count=len(tables_spec),
        )
        session.add(doc)
        session.flush()
        for idx, tspec in enumerate(tables_spec):
            blk = RawBlockModel(
                id=str(uuid.uuid4()),
                document_id=doc.id,
                ordinal=idx,
                block_type="DOCX_TABLE",
                raw_content=tspec["code"],
                source_locator={"section": tspec["code"]},
                content_json={},
                raw_hash=uuid.uuid4().hex,
            )
            session.add(blk)
            session.flush()
            tbl = TableMetadataModel(
                id=str(uuid.uuid4()),
                document_id=doc.id,
                block_id=blk.id,
                table_code=tspec["code"],
                table_name=tspec["name"],
                table_definition="",
                status="PUBLISHED",
                current_version=1,
                published_version=1,
                source_ordinal=idx,
            )
            session.add(tbl)
            session.flush()
            for f_idx, f in enumerate(tspec["fields"]):
                session.add(
                    FieldMetadataModel(
                        id=str(uuid.uuid4()),
                        table_id=tbl.id,
                        field_cn=f.get("cn", f["en"]),
                        field_en=f["en"],
                        field_definition="",
                        check_text_raw=f.get("check", ""),
                        data_type=f.get("type", "VARCHAR"),
                        length=f.get("length", 32),
                        decimal_places=f.get("dec", 0),
                        required=f.get("required", False),
                        code_reference_text=f.get("code_ref", ""),
                        confidence=1.0,
                        evidence_json={},
                        sort_order=f_idx,
                        ai_value_json={},
                    )
                )
        session.commit()
        return doc

    doc_v1 = _seed_doc(
        "SAFE_1.3",
        [
            {
                "code": "T_UNCHANGED",
                "name": "未变表",
                "fields": [{"en": "ID", "type": "VARCHAR", "length": 16, "required": True}],
            },
            {
                "code": "T_MODIFIED",
                "name": "变更表",
                "fields": [
                    {"en": "RPTNO", "type": "VARCHAR", "length": 22, "required": True, "check": "必填"},
                    {"en": "AMT", "type": "NUMBER", "length": 22, "dec": 2, "required": False, "check": "大于0"},
                    {"en": "LEGACY_COL", "type": "VARCHAR", "length": 10},
                ],
            },
            {
                "code": "T_REMOVED",
                "name": "下线表",
                "fields": [{"en": "OLD_ID", "type": "VARCHAR", "length": 8}],
            },
        ],
    )
    doc_v2 = _seed_doc(
        "SAFE_1.4",
        [
            {
                "code": "T_UNCHANGED",
                "name": "未变表",
                "fields": [{"en": "ID", "type": "VARCHAR", "length": 16, "required": True}],
            },
            {
                "code": "T_MODIFIED",
                "name": "变更表",
                "fields": [
                    {"en": "RPTNO", "type": "VARCHAR", "length": 22, "required": True, "check": "必填且唯一"},
                    {"en": "AMT", "type": "NUMBER", "length": 18, "dec": 2, "required": True, "check": "必须大于0"},
                    {"en": "NEW_FLAG", "type": "VARCHAR", "length": 1, "required": True},
                ],
            },
            {
                "code": "T_ADDED",
                "name": "新增表",
                "fields": [{"en": "ACCT_NO", "type": "VARCHAR", "length": 32, "required": True}],
            },
        ],
    )

    diff = diff_documents(session, doc_v1.id, doc_v2.id)
    assert diff["summary"]["added_tables"] == 1
    assert diff["summary"]["removed_tables"] == 1
    assert diff["summary"]["modified_tables"] == 1
    assert diff["summary"]["unchanged_tables"] == 1
    assert diff["summary"]["affected_table_count"] == 2  # T_MODIFIED + T_ADDED

    mod_entry = next(t for t in diff["tables"] if t["table_code"] == "T_MODIFIED")
    assert mod_entry["impact"] == "BREAKING"
    assert [f["field_en"] for f in mod_entry["added_fields"]] == ["NEW_FLAG"]
    assert [f["field_en"] for f in mod_entry["removed_fields"]] == ["LEGACY_COL"]
    amt_mod = next(f for f in mod_entry["modified_fields"] if f["field_en"] == "AMT")
    assert amt_mod["impact"] == "BREAKING"
    assert {c["attr"] for c in amt_mod["changes"]} >= {"length", "required", "check_text_raw"}

    # 跨批次规则与 Oracle SQL Diff
    r1 = create_run(session, str(uuid.uuid4()))
    r2 = create_run(session, str(uuid.uuid4()))
    ev = {"section": "6.4.2", "page": "1", "field": "AMT", "quote": "q"}
    session.add_all(
        [
            RuleModel(
                row_id=str(uuid.uuid4()),
                run_id=r1.id,
                spec_id=r1.spec_id,
                table_code="T_MODIFIED",
                rule_key="R_AMT_RANGE",
                rule_hash="h1",
                source="DOC_EXPLICIT",
                status="APPROVED",
                definition_json=RuleDefinition(
                    rule_key="R_AMT_RANGE", table_code="T_MODIFIED", field="AMT",
                    operator="NUM_RANGE", params={"min": 0}, source="DOC_EXPLICIT", evidence=ev, rationale="old"
                ).model_dump(),
            ),
            RuleModel(
                row_id=str(uuid.uuid4()),
                run_id=r1.id,
                spec_id=r1.spec_id,
                table_code="T_MODIFIED",
                rule_key="R_LEGACY_REQ",
                rule_hash="h2",
                source="DOC_EXPLICIT",
                status="APPROVED",
                definition_json=RuleDefinition(
                    rule_key="R_LEGACY_REQ", table_code="T_MODIFIED", field="LEGACY_COL",
                    operator="REQUIRED", params={}, source="DOC_EXPLICIT", evidence={**ev, "field": "LEGACY_COL"}, rationale="removed"
                ).model_dump(),
            ),
            RuleModel(
                row_id=str(uuid.uuid4()),
                run_id=r2.id,
                spec_id=r2.spec_id,
                table_code="T_MODIFIED",
                rule_key="R_AMT_RANGE",
                rule_hash="h3",
                source="DOC_EXPLICIT",
                status="APPROVED",
                definition_json=RuleDefinition(
                    rule_key="R_AMT_RANGE", table_code="T_MODIFIED", field="AMT",
                    operator="NUM_RANGE", params={"min": 0, "min_inclusive": False}, source="DOC_EXPLICIT", evidence=ev, rationale="strict > 0"
                ).model_dump(),
            ),
            RuleModel(
                row_id=str(uuid.uuid4()),
                run_id=r2.id,
                spec_id=r2.spec_id,
                table_code="T_MODIFIED",
                rule_key="R_RPTNO_UNIQ",
                rule_hash="h4",
                source="DOC_EXPLICIT",
                status="APPROVED",
                definition_json=RuleDefinition(
                    rule_key="R_RPTNO_UNIQ", table_code="T_MODIFIED", field="RPTNO",
                    operator="UNIQUE_KEY", params={"key_fields": ["RPTNO"]}, source="DOC_EXPLICIT", evidence={**ev, "field": "RPTNO"}, rationale="unique"
                ).model_dump(),
            ),
        ]
    )
    session.commit()

    rdiff = diff_runs(session, r1.id, r2.id)
    assert rdiff["summary"] == {"added": 1, "removed": 1, "modified": 1, "unchanged": 0}
    assert rdiff["added"][0]["rule_key"] == "R_RPTNO_UNIQ"
    assert "COUNT(*)" in (rdiff["added"][0]["sql"] or "")
    assert rdiff["removed"][0]["rule_key"] == "R_LEGACY_REQ"
    assert rdiff["modified"][0]["rule_key"] == "R_AMT_RANGE"
    assert any(c["attr"] == "sql" for c in rdiff["modified"][0]["changes"])


def test_policy_rules_compiler_deep_audit_fixes():
    """回归：Layer 2 policy + Layer 5 rules/compiler/binding + Layer 6 production 深审修复。"""
    import sqlite3
    from app.delivery.production import compile_production_rule_query, sql_literal
    from app.generation.core.binding import violates_arithmetic_balance
    from app.generation.sql.compiler import compile_where
    from app.infra.contracts import Evidence
    from app.kernel.event_log import RunState
    from app.policy.regops_bus import (
        code_set_values_adjudicator,
        cross_table_adjudicator,
    )
    from app.policy.regops_plan import _apply

    spec = _account_balance_spec()
    ev = Evidence(section="6.1", page="1", field="RPTNO", quote="申报号码")

    # 1) UNIQUE_KEY 生产 SQL 绝不引用沙箱专属的 record_id 列，可在无 record_id 的真实生产表上直接执行
    uniq_rule = RuleDefinition(
        rule_key="R_PROD_UNIQ",
        table_code=spec.table_code,
        field="RPTNO",
        operator="UNIQUE_KEY",
        params={"key_fields": ["RPTNO"]},
        source="DOC_EXPLICIT",
        evidence=ev,
        rationale="主键唯一",
    )
    prod_sql = compile_production_rule_query(uniq_rule, "oracle")
    assert "record_id" not in prod_sql.lower()
    assert "COUNT(*)" in prod_sql
    # 在没有 record_id 列的真 SQLite 生产结构表上执行验证
    conn = sqlite3.connect(":memory:")
    conn.execute(f'CREATE TABLE "{spec.table_code}" ("RPTNO" TEXT)')
    conn.executemany(f'INSERT INTO "{spec.table_code}" ("RPTNO") VALUES (?)', [("OK1",), ("DUP",), ("DUP",)])
    rows = conn.execute(prod_sql).fetchall()
    assert len(rows) == 2 and all(r[1] == "DUP" for r in rows)

    # 2) ARITHMETIC_BALANCE GT / LT 容差方向：tol=0.01 放宽浮点误差而非收紧导致误报
    gt_params = {"terms": [{"field": "OPEN_BAL", "coef": 1}], "operator": "GT", "target_field": "CLOSE_BAL", "tolerance": 0.01}
    assert violates_arithmetic_balance(gt_params, {"OPEN_BAL": 100.005, "CLOSE_BAL": 100.0}) is False
    assert violates_arithmetic_balance(gt_params, {"OPEN_BAL": 99.98, "CLOSE_BAL": 100.0}) is True
    lt_params = {"terms": [{"field": "OPEN_BAL", "coef": 1}], "operator": "LT", "target_field": "CLOSE_BAL", "tolerance": 0.01}
    assert violates_arithmetic_balance(lt_params, {"OPEN_BAL": 99.995, "CLOSE_BAL": 100.0}) is False
    assert violates_arithmetic_balance(lt_params, {"OPEN_BAL": 100.02, "CLOSE_BAL": 100.0}) is True
    gt_where, _ = compile_where(
        RuleDefinition(
            rule_key="R_GT", table_code=spec.table_code, field="OPEN_BAL",
            operator="ARITHMETIC_BALANCE", params=gt_params, source="AI_EXPANDED",
            evidence=Evidence(section="6.1", page="1", field="OPEN_BAL", quote="期初余额"), rationale="gt",
        ),
        "oracle",
    )
    assert "<= ((t.\"CLOSE_BAL\") - :bal_tol)" in gt_where

    # 3) COND_VALUE(ENUM) 在 Oracle 下超过 1000 项自动切片避开 ORA-01795
    big_cond = RuleDefinition(
        rule_key="R_BIG_COND",
        table_code=spec.table_code,
        field="TXCCY",
        operator="COND_VALUE",
        params={
            "if_field": "IS_DOMESTIC",
            "if_operator": "EQ",
            "if_values": ["Y"],
            "then_operator": "ENUM",
            "then_values": [f"C{i:04d}" for i in range(1205)],
        },
        source="AI_EXPANDED",
        evidence=Evidence(section="6.1", page="1", field="TXCCY", quote="币种"),
        rationale="big enum",
    )
    big_where, big_params = compile_where(big_cond, "oracle")
    assert big_where.count("NOT IN (") == 2
    assert len(big_params) == 1206  # 1205 then_val + 1 if_value_0

    # 4) rules.py 严格校验：非数字 length、非有限 NaN/Inf、空区间 10 < x < 10、自引用 COND_REQUIRED、非数值列 NUM_RANGE
    for bad_op, bad_field, bad_params in [
        ("LENGTH_MAX", "TXCCY", {"length": "not_int"}),
        ("COND_REQUIRED", "TXCCY", {"if_field": "TXCCY", "if_operator": "EQ", "if_values": ["CNY"]}),
        ("NUM_RANGE", "TXCCY", {"min": 0, "max": 10}),
        ("NUM_RANGE", "OPEN_BAL", {"min": 10, "max": 10, "min_inclusive": False}),
        ("NUM_RANGE", "OPEN_BAL", {"min": float("nan")}),
    ]:
        with pytest.raises(RuleValidationError):
            validate_rule(
                RuleDefinition(
                    rule_key="R_BAD",
                    table_code=spec.table_code,
                    field=bad_field,
                    operator=bad_op,
                    params=bad_params,
                    source="AI_EXPANDED",
                    evidence=Evidence(section="6.1", page="1", field=bad_field, quote="q"),
                    rationale="bad",
                ),
                spec,
            )
    with pytest.raises(ValueError, match="Non-finite"):
        sql_literal(float("inf"))

    # 5) regops_bus.py：published_code_sets 映射覆盖 assign_rules（含 COND_VALUE.ENUM）+ golden_overrides 跨表字段拦截
    cs_reason = code_set_values_adjudicator(
        "assign_rules",
        {
            "rules": [
                {
                    "field": "TXCCY",
                    "operator": "COND_VALUE",
                    "params": {"if_field": "IS_DOMESTIC", "if_operator": "EQ", "if_values": ["Y"], "then_operator": "ENUM", "then_values": ["FAKE_CCY"]},
                }
            ]
        },
        {"published_code_sets": {"TXCCY": {"code": "CCY", "values": ["CNY", "USD"]}}},
    )
    assert cs_reason is not None and "FAKE_CCY" in cs_reason

    ct_reason = cross_table_adjudicator(
        "assign_rules",
        {"rules": [], "golden_overrides": {"T1": {"GHOST_COL": "1"}}},
        {"table_codes": ["T1"], "table_fields": {"T1": ["REAL_COL"]}},
    )
    assert ct_reason is not None and "GHOST_COL" in ct_reason

    # 6) regops_plan._apply：RUN_FAILED / RUN_CANCELLED 保留显式传入的 resume_from_node 与 resume_when
    st = _apply(
        RunState(status="RUNNING", current_node="rule_binder", resume_from_node="created", resume_when="always"),
        "RUN_FAILED",
        {"node": "rule_binder", "result": {"error": "boom"}, "resume_from_node": "schema_mapper", "resume_when": "always"},
    )
    assert st.status == "FAILED" and st.resume_from_node == "schema_mapper"


def test_oracle_module_deep_audit_fixes(monkeypatch):
    """回归：oracle.py 深审修复（128 字节表名截断防 ORA-00972、Oracle/MySQL 精度与宽表 Error 1118 保护、Decimal(38,s)/NUMBER(4,4) 与 AL32UTF8 4000 字节防 ORA-01438/ORA-12899、Oracle 幂等重试 + executemany 批量写入 + CLOB preview）。"""
    from decimal import Decimal
    from types import SimpleNamespace
    import app.delivery.testing.sandbox_audit as sandbox_audit_mod
    from app.delivery.testing.sandbox_audit import GenericOracleExecutor
    import app.generation.sql.oracle as oracle_mod
    from app.generation.sql.oracle import (
        OracleSandbox,
        _fit_number,
        compile_ai_schema_mysql_ddl,
        compile_ai_schema_oracle_ddl,
        fit_record_values,
        fit_sandbox_values,
        physical_table_name,
    )
    from app.infra.contracts import AuditCaseBinding, FieldSpec, SchemaBinding, SpecDocument

    # 1) 超长表名自动哈希压缩，拼接 _TARGET 后仍严格 <= 128 字节（防 ORA-00972）
    long_code = "SAFE_" + "A" * 130
    phys = physical_table_name("12345678-1234-1234-1234-1234567890ab", 1, long_code)
    assert len(phys) <= 120
    assert len(f"{phys}_TARGET") <= 128
    assert phys != physical_table_name("12345678-1234-1234-1234-1234567890ab", 1, long_code[:-1] + "B")

    # 2) Oracle NUMBER(p,s) / VARCHAR2(>4000 -> CLOB) 与 MySQL DECIMAL(65,30) / VARCHAR(>16383 -> TEXT) + 宽表防 Error 1118
    schemas = [
        SchemaBinding(field="HUGE_NUM", storage_type="NUMBER", length=50, decimal_places=40),
        SchemaBinding(field="HUGE_TXT", storage_type="TEXT", length=5000, decimal_places=0),
    ]
    o_ddl = compile_ai_schema_oracle_ddl("T_O", schemas)
    assert "NUMBER(38,38)" in o_ddl
    assert '"HUGE_TXT" CLOB NULL' in o_ddl
    m_ddl = compile_ai_schema_mysql_ddl("T_M", [
        SchemaBinding(field="HUGE_NUM", storage_type="NUMBER", length=80, decimal_places=40),
        SchemaBinding(field="HUGE_TXT", storage_type="TEXT", length=20000, decimal_places=0),
    ])
    assert "DECIMAL(65,30)" in m_ddl
    assert "`HUGE_TXT` TEXT NULL" in m_ddl
    # 40 列 VARCHAR(500) 在 utf8mb4 下 80000B > 65535B，自动把超预算宽列升格 TEXT 防 MySQL Error 1118
    wide_mysql_ddl = compile_ai_schema_mysql_ddl(
        "T_WIDE",
        [SchemaBinding(field=f"COL_{i}", storage_type="TEXT", length=500, decimal_places=0) for i in range(40)],
    )
    assert "TEXT NULL" in wide_mysql_ddl

    # 3) _fit_number：千分位逗号、NUMBER(4,4) 零整数位不超 0.9999、NUMBER(38,2) 超 28 位 Decimal 精度不炸 ORA-01438
    assert _fit_number("1,234.56", 12, 2) == Decimal("1234.56")
    assert _fit_number("5.1234", 4, 4) == Decimal("0.9999")
    huge_val = _fit_number("9" * 40 + ".99", 50, 2)
    assert huge_val == Decimal("9" * 36 + ".99")

    # 4) fit_record_values：spec.length 为 None 时按 schema_length 截断；_fit_text 与 fit_sandbox_values 均守 AL32UTF8 4000 字节上限
    spec = _account_balance_spec()
    no_len_spec = SpecDocument(
        version="TEST",
        table_code="T_NOLEN",
        table_name="无长度测试",
        source_name="s.pdf",
        source_sha256="0" * 64,
        section="1",
        fields=[
            FieldSpec(name="NOTE", content="备注", data_type="VARCHAR", length=0, evidence_page="1"),
            FieldSpec(name="CN_BIG", content="大中文", data_type="VARCHAR", length=2000, evidence_page="1"),
        ],
        terms_text="",
        dictionary_text="",
    )
    fitted_rec = fit_record_values(no_len_spec, {"NOTE": 10, "CN_BIG": 2000}, {"NOTE": "a" * 30, "CN_BIG": "中" * 1800})
    assert len(fitted_rec["NOTE"]) == 10
    assert len(fitted_rec["CN_BIG"].encode("utf-8")) <= 4000
    fitted_cn = fit_sandbox_values(spec, {"RPTNO": "中" * 2000})["RPTNO"]
    assert len(fitted_cn.encode("utf-8")) <= 4000

    # 5) 模拟真实 Oracle 方言：验证 prepare_assets / execute 幂等 DROP TABLE ... PURGE（忽略 ORA-00942）、
    #    DELETE FROM 清旧行（防 ORA-00001）以及码表/黄金/变异全部走 executemany 批量列表，加上 CLOB preview 自动 read()
    executed_log = []

    class _FakeResult:
        def __init__(self, rows=None):
            self._rows = rows or []

        def fetchall(self):
            return list(self._rows)

        def mappings(self):
            return SimpleNamespace(all=lambda: list(self._rows))

    class _FakeOracleConn:
        dialect = SimpleNamespace(name="oracle")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, stmt, params=None):
            sql = str(stmt)
            executed_log.append((sql, params))
            if sql.startswith("DROP TABLE") and len(executed_log) == 1:
                raise RuntimeError("ORA-00942: table or view does not exist")
            if sql.startswith("SELECT * FROM"):
                fake_lob = SimpleNamespace(read=lambda: "clob-text")
                return _FakeResult([{"record_id": "R1", "HUGE_TXT": fake_lob}])
            return _FakeResult([])

    class _FakeOracleEngine:
        def begin(self):
            return _FakeOracleConn()

        def connect(self):
            return _FakeOracleConn()

        def dispose(self):
            pass

    monkeypatch.setattr(oracle_mod, "create_engine", lambda *a, **kw: _FakeOracleEngine())
    monkeypatch.setattr(sandbox_audit_mod, "create_engine", lambda *a, **kw: _FakeOracleEngine())
    executor = GenericOracleExecutor("oracle+oracledb://mock")
    table_schemas = {
        spec.table_code: [
            SchemaBinding(
                field=f.name,
                storage_type="NUMBER" if f.data_type == "NUMBER" else "TEXT",
                length=f.length or 64,
                decimal_places=f.decimal_places or 0,
            )
            for f in spec.fields
        ]
    }
    code_sets = [
        {
            "code": "CCY",
            "version": "v1",
            "values": [
                {"value": "CNY", "label_cn": "人民币"},
                {"value": "USD", "label_cn": "美元"},
                {"value": "EUR", "label_cn": "欧元"},
            ],
        }
    ]
    prepared = executor.prepare_assets("run-idem-001", 1, [spec], table_schemas, code_sets=code_sets, rules=[])
    drop_stmts = [sql for sql, _ in executed_log if sql.startswith("DROP TABLE") and sql.endswith("PURGE")]
    assert len(drop_stmts) == 3  # 码表 + TARGET + 沙箱表，且首次 ORA-00942 被安全忽略
    cs_inserts = [params for sql, params in executed_log if sql.startswith("INSERT INTO") and isinstance(params, list)]
    assert len(cs_inserts) == 1 and len(cs_inserts[0]) == 3  # 码表一次性 executemany 批量写入

    executed_log.clear()
    golden = {
        spec.table_code: {
            "RPTNO": "20260101000001",
            "CUSTYPE": "D",
            "IDCODE": "110101199001011234",
            "TXCCY": "USD",
            "IS_DOMESTIC": "N",
            "OPEN_BAL": 1000,
            "CREDIT_AMT": 500,
            "DEBIT_AMT": 200,
            "CLOSE_BAL": 1300,
        }
    }
    cases = {
        spec.table_code: [
            AuditCaseBinding(case_id="GOLDEN_BASELINE", overrides={}, expected_rule_keys=[]),
            AuditCaseBinding(case_id="MUT_1", overrides={"TXCCY": "CNY"}, expected_rule_keys=[]),
        ]
    }
    rep = executor.execute(
        "run-idem-001",
        1,
        [spec],
        table_schemas,
        golden,
        cases,
        [],
        golden_counts={spec.table_code: 3},
        prepared_assets=prepared,
    )
    assert rep["all_passed"] is True
    delete_stmts = [sql for sql, _ in executed_log if sql.startswith("DELETE FROM")]
    assert len(delete_stmts) == 2  # TARGET + 沙箱表均先清旧行，重复 execute 幂等防 ORA-00001
    batch_inserts = [params for sql, params in executed_log if sql.startswith("INSERT INTO") and isinstance(params, list)]
    assert [len(b) for b in batch_inserts] == [3, 2]  # 3 条黄金 + 2 条审计用例分别单次批量 executemany

    # CLOB preview 自动 read()
    preview_rows = OracleSandbox("oracle+oracledb://mock").preview("T_CLOB", limit=5)
    assert preview_rows == [{"record_id": "R1", "HUGE_TXT": "clob-text"}]


