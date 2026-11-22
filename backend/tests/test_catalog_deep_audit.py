"""Layer 4 `catalog/` 生产级深度审计回归测试（覆盖 MySQL 8 / Oracle 23 外键、唯一键、并发与逻辑边界）。"""

import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.catalog.code_sets.linking import (
    auto_link_all_references,
    generation_blockers,
    refresh_reference_status,
)
from app.catalog.code_sets.service import _upsert_code_set_draft, remove_code_set
from app.catalog.code_sets.parser import CodeSetDraft, CodeValueDraft
from app.catalog.tables.domain import MetadataTableDraft, validate_metadata_draft
from app.catalog.tables.mutations import (
    apply_field_type_import,
    merge_metadata_tables,
    update_field,
)
from app.catalog.tables.readiness import evaluate_table, latest_table_rows
from app.catalog.tables.service import _deterministic_field_draft, extract_metadata_for_block
from app.catalog.tables.views import type_basis_for
from app.catalog.version_diff import diff_documents
from app.infra.config import Settings
from app.infra.db import Base
from app.infra.db_tables import (
    CodeSetModel,
    CodeSetValidationModel,
    CodeValueModel,
    FieldCodeReferenceModel,
    FieldMetadataModel,
    MetadataRevisionModel,
    RawBlockModel,
    SourceDocumentModel,
    TableMetadataModel,
)
from app.infra.llm.guard import llm_execution_scope
from app.kernel.run import CancellationToken, RunCancelledError


def _make_fk_enforced_session():
    """开启 SQLite PRAGMA foreign_keys=ON，模拟 MySQL 8 (1451) 与 Oracle 23 (ORA-02292) 严格外键约束。"""
    engine = create_engine("sqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def _seed_doc_block_table(session, *, table_code="T1", block_ordinal=0, status="DRAFT", version=1):
    doc = SourceDocumentModel(
        id=str(uuid.uuid4()),
        file_name=f"{table_code}.pdf",
        file_type="pdf",
        sha256=uuid.uuid4().hex,
        blob_uri="blob://seed",
        spec_version="V1",
        status="UPLOADED",
        block_count=1,
    )
    blk = RawBlockModel(
        id=str(uuid.uuid4()),
        document_id=doc.id,
        ordinal=block_ordinal,
        block_type="DATA_DICTIONARY",
        raw_content="KHBH 客户编号 VARCHAR(60) 必填",
        source_locator={"section": table_code},
        content_json={},
        raw_hash=uuid.uuid4().hex,
    )
    tbl = TableMetadataModel(
        id=str(uuid.uuid4()),
        document_id=doc.id,
        block_id=blk.id,
        table_code=table_code,
        table_name=f"{table_code} 表",
        table_definition="测试表",
        status=status,
        current_version=version,
        source_ordinal=block_ordinal,
    )
    session.add(doc)
    session.flush()
    session.add(blk)
    session.flush()
    session.add(tbl)
    session.flush()
    return doc, blk, tbl


def test_remove_code_set_deletes_validations_under_strict_foreign_keys():
    """Bug 2: MySQL 8 (1451) / Oracle 23 (ORA-02292) 下删除带校验记录的码表不得触发外键冲突。"""
    session = _make_fk_enforced_session()
    cs = CodeSetModel(
        id=str(uuid.uuid4()),
        code="CS_STRICT_FK",
        name="外键删除测试码表",
        definition="test",
        source_type="DOCUMENT_TABLE",
        status="AI_SELF_VALIDATED",
        current_version=1,
    )
    session.add(cs)
    session.flush()
    session.add(
        CodeValueModel(
            id=str(uuid.uuid4()),
            code_set_id=cs.id,
            value="01",
            label_cn="正常",
            sort_order=0,
            enabled=True,
            source_locator={},
        )
    )
    session.add(
        CodeSetValidationModel(
            id=str(uuid.uuid4()),
            code_set_id=cs.id,
            validator_kind="PROGRAM",
            status="PASSED",
            issues_json=[],
            trace_json={},
        )
    )
    session.commit()

    res = remove_code_set(session, cs)
    session.commit()
    assert res["code"] == "CS_STRICT_FK"
    assert session.query(CodeSetModel).count() == 0
    assert session.query(CodeSetValidationModel).count() == 0


def test_auto_link_and_refresh_preserve_external_and_generation_blockers_ignore_fixed_rules():
    """Bug 1 & Bug 8: auto_link_all_references / refresh_reference_status 不得抹掉 EXTERNAL；generation_blockers 不得误拦固定值/规则引用。"""
    session = _make_fk_enforced_session()
    _, _, tbl = _seed_doc_block_table(session, table_code="T_EXT")
    f_ext = FieldMetadataModel(
        id=str(uuid.uuid4()),
        table_id=tbl.id,
        field_cn="外部代码",
        field_en="EXT_CODE",
        field_definition="外部",
        check_text_raw="见外汇局外部代码表",
        data_type="VARCHAR",
        length=32,
        decimal_places=0,
        required=True,
        code_reference_text="外汇局外部代码表",
        confidence=0.9,
        evidence_json={},
        sort_order=0,
        ai_value_json={},
        type_basis="SOURCE",
    )
    f_fixed = FieldMetadataModel(
        id=str(uuid.uuid4()),
        table_id=tbl.id,
        field_cn="国别固定值",
        field_en="FIXED_COUNTRY",
        field_definition="固定填中国",
        check_text_raw="本项固定填“中国”",
        data_type="VARCHAR",
        length=16,
        decimal_places=0,
        required=True,
        code_reference_text="本项固定填“中国”",
        confidence=0.9,
        evidence_json={},
        sort_order=1,
        ai_value_json={},
        type_basis="SOURCE",
    )
    f_rule = FieldMetadataModel(
        id=str(uuid.uuid4()),
        table_id=tbl.id,
        field_cn="外债编号",
        field_en="DEBT_NO",
        field_definition="编号",
        check_text_raw="按外债编号统一编号规则生成",
        data_type="VARCHAR",
        length=28,
        decimal_places=0,
        required=True,
        code_reference_text="外债编号统一编号规则",
        confidence=0.9,
        evidence_json={},
        sort_order=2,
        ai_value_json={},
        type_basis="SOURCE",
    )
    cs_ext = CodeSetModel(
        id=str(uuid.uuid4()),
        code="EXT_CODESET",
        name="外汇局外部代码表",
        definition="外部占位",
        source_type="DOCUMENT_REFERENCE",
        status="DRAFT",
        current_version=1,
    )
    session.add_all([f_ext, f_fixed, f_rule, cs_ext])
    session.flush()
    ref = FieldCodeReferenceModel(
        id=str(uuid.uuid4()),
        field_id=f_ext.id,
        code_set_id=cs_ext.id,
        reference_text="外汇局外部代码表",
        status="EXTERNAL",
        condition_json={},
        confidence=0.9,
    )
    session.add(ref)
    session.commit()

    # 反复运行 refresh 与全量 auto_link，EXTERNAL 必须保持不动
    refresh_reference_status(session, cs_ext.id)
    auto_link_all_references(session)
    session.commit()
    assert session.get(FieldCodeReferenceModel, ref.id).status == "EXTERNAL"

    # 固定值、格式规则、EXTERNAL 均不得被 generation_blockers 误拦截
    assert generation_blockers(session, tbl) == []


def test_merge_metadata_tables_strict_fk_needs_review_type_basis_and_version_diff():
    """Bug 3 & Bug 4: 严格外键下合并子表不崩、保留子表 NEEDS_REVIEW 与 type_basis、不撞 uq_table_block_version，且 readiness/version_diff 过滤 MERGED。"""
    session = _make_fk_enforced_session()
    doc, parent_blk, old_parent_tbl = _seed_doc_block_table(session, table_code="T_PARENT", block_ordinal=0, version=1)

    child_blk_a = RawBlockModel(
        id=str(uuid.uuid4()), document_id=doc.id, ordinal=1, block_type="TERMINOLOGY",
        raw_content="术语段", source_locator={}, content_json={}, raw_hash="h1",
    )
    child_blk_b = RawBlockModel(
        id=str(uuid.uuid4()), document_id=doc.id, ordinal=2, block_type="DATA_DICTIONARY",
        raw_content="字典段", source_locator={}, content_json={}, raw_hash="h2",
    )
    session.add_all([child_blk_a, child_blk_b])
    session.flush()

    t_a = TableMetadataModel(
        id=str(uuid.uuid4()), document_id=doc.id, block_id=child_blk_a.id,
        table_code="T_PARENT", table_name="子表A", table_definition="",
        status="NEEDS_REVIEW", current_version=1, source_ordinal=1,
    )
    t_b = TableMetadataModel(
        id=str(uuid.uuid4()), document_id=doc.id, block_id=child_blk_b.id,
        table_code="T_PARENT", table_name="子表B", table_definition="",
        status="AI_SELF_VALIDATED", current_version=1, source_ordinal=2,
    )
    session.add_all([t_a, t_b])
    session.flush()

    f_a = FieldMetadataModel(
        id=str(uuid.uuid4()), table_id=t_a.id, field_cn="客户号", field_en="KHBH",
        field_definition="d", check_text_raw="必填", data_type="VARCHAR", length=32, decimal_places=0,
        required=True, code_reference_text="", confidence=0.9, evidence_json={}, sort_order=0,
        ai_value_json={}, type_basis="SOURCE",
    )
    f_b = FieldMetadataModel(
        id=str(uuid.uuid4()), table_id=t_b.id, field_cn="币种", field_en="CCY",
        field_definition="d", check_text_raw="必填", data_type="VARCHAR", length=3, decimal_places=0,
        required=True, code_reference_text="币种表", confidence=0.9, evidence_json={}, sort_order=0,
        ai_value_json={}, type_basis="MANUAL",
    )
    cs = CodeSetModel(
        id=str(uuid.uuid4()), code="CCY_TEST", name="币种表", definition="",
        source_type="DOCUMENT_TABLE", status="PUBLISHED", current_version=1, published_version=1,
    )
    session.add_all([f_a, f_b, cs])
    session.flush()
    # 给 canonical(t_b) 的旧字段挂上外键引用与修订记录，测试严格 FK 下删除旧字段不崩
    session.add(FieldCodeReferenceModel(id=str(uuid.uuid4()), field_id=f_b.id, code_set_id=cs.id, reference_text="币种表", status="RESOLVED", condition_json={}))
    session.add(MetadataRevisionModel(id=str(uuid.uuid4()), table_id=t_b.id, field_id=f_b.id, revision_kind="HUMAN_EDIT", old_value_json={}, new_value_json={}, editor="u", reason="r"))
    session.commit()

    merged_id = merge_metadata_tables(session, parent_blk.id, [t_a.id, t_b.id])
    session.commit()
    merged = session.get(TableMetadataModel, merged_id)
    assert merged.block_id == parent_blk.id
    assert merged.current_version == 2  # 自动递增避开 old_parent_tbl (block_id=parent_blk.id, version=1)
    assert merged.status == "NEEDS_REVIEW"  # 保留了非 canonical 子表 t_a 的 NEEDS_REVIEW！
    merged_fields = {f.field_en: f for f in session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == merged.id).all()}
    assert merged_fields["KHBH"].type_basis == "SOURCE"
    assert merged_fields["CCY"].type_basis == "MANUAL"

    # latest_table_rows 与 diff_documents 均过滤 MERGED 且只取最新版本行（1 张有效表，不是 3 张！）
    live = latest_table_rows(session, document_id=doc.id)
    assert [t.id for t in live] == [merged.id]
    diff = diff_documents(session, doc.id, doc.id)
    assert diff["summary"]["total_base_tables"] == 1
    assert diff["summary"]["total_target_tables"] == 1
    assert diff["summary"]["unchanged_tables"] == 1


def test_apply_field_type_import_never_mutates_wrong_table_and_validates_bounds():
    """Bug 6: 批量导入写错报表代码时严禁静默回退去篡改其他表，且拦截非法长度/小数位；update_field 校验合法性。"""
    session = _make_fk_enforced_session()
    _, _, tbl = _seed_doc_block_table(session, table_code="REAL_TABLE")
    f = FieldMetadataModel(
        id=str(uuid.uuid4()), table_id=tbl.id, field_cn="金额", field_en="AMT",
        field_definition="金额", check_text_raw="必填", data_type="NUMBER", length=20, decimal_places=2,
        required=True, code_reference_text="", confidence=0.9, evidence_json={}, sort_order=0,
        ai_value_json={}, type_basis="SOURCE",
    )
    session.add(f)
    session.commit()

    res = apply_field_type_import(
        session,
        [
            ["报表代码", "字段英文名", "目标类型", "目标长度", "目标小数位"],
            ["TYPO_TABLE", "AMT", "NUMBER", "10", "2"],   # 写错表名 -> 必须报错，绝不能改 REAL_TABLE!
            ["REAL_TABLE", "AMT", "NUMBER", "0", "0"],    # 长度 <= 0 -> 报错
            ["REAL_TABLE", "AMT", "NUMBER", "10", "10"],  # 小数位 >= 长度 -> 报错
            ["REAL_TABLE", "AMT", "NUMBER", "18", "-1"],  # 负小数位 -> 报错
        ],
        editor="tester",
    )
    assert res["updated"] == 0
    assert len(res["errors"]) == 4
    assert "找不到报表 TYPO_TABLE" in res["errors"][0]["message"]
    assert f.length == 20 and f.decimal_places == 2

    with pytest.raises(ValueError, match="INVALID_DATA_TYPE"):
        update_field(session, tbl, f, {"data_type": "DATE"}, 1, "u", "r")
    with pytest.raises(ValueError, match="INVALID_LENGTH"):
        update_field(session, tbl, f, {"length": 0}, 1, "u", "r")


def test_case_insensitive_duplicate_guards_for_mysql8_and_oracle23():
    """Bug 7: MySQL 8 (utf8mb4_0900_ai_ci) 与 Oracle 23 下大小写重复字段与码值提前拦截/去重。"""
    draft = MetadataTableDraft(
        table_code="T_CI",
        table_name="大小写重名测试",
        fields=[
            {"field_cn": "甲", "field_en": "CustId", "length": 32, "evidence_json": {"p": 1}},
            {"field_cn": "乙", "field_en": "CUSTID", "length": 32, "evidence_json": {"p": 1}},
        ],
    )
    issues = validate_metadata_draft(draft)
    assert any(i["code"] == "DUPLICATE_FIELD" for i in issues)

    session = _make_fk_enforced_session()
    _, _, tbl = _seed_doc_block_table(session, table_code="T_CI")
    session.add_all([
        FieldMetadataModel(
            id=str(uuid.uuid4()), table_id=tbl.id, field_cn="甲", field_en="CustId",
            field_definition="", check_text_raw="", data_type="VARCHAR", length=32, decimal_places=0,
            required=False, code_reference_text="", confidence=0.9, evidence_json={}, sort_order=0,
            ai_value_json={}, type_basis="SOURCE",
        ),
        FieldMetadataModel(
            id=str(uuid.uuid4()), table_id=tbl.id, field_cn="乙", field_en="CUSTID",
            field_definition="", check_text_raw="", data_type="VARCHAR", length=32, decimal_places=0,
            required=False, code_reference_text="", confidence=0.9, evidence_json={}, sort_order=1,
            ai_value_json={}, type_basis="SOURCE",
        ),
    ])
    session.commit()
    readiness = evaluate_table(session, tbl)
    assert any(b["code"] == "DUPLICATE_FIELD" for b in readiness["ddl"]["blockers"])

    # 码值大小写冲突去重，防止撞 MySQL 8 uq_code_value
    cs_draft = CodeSetDraft(
        code="CI_CODE_TEST",
        name="大小写码值测试",
        values=[
            CodeValueDraft(value="a", label_cn="小写A"),
            CodeValueDraft(value="A", label_cn="大写A"),
        ],
    )
    row, _ = _upsert_code_set_draft(session, cs_draft, {})
    session.commit()
    vals = session.query(CodeValueModel).filter(CodeValueModel.code_set_id == row.id).all()
    assert len(vals) == 1


def test_tabular_header_mapping_sql_type_basis_and_cancel_propagation(monkeypatch):
    """Bug 5, Bug 9, Bug 10: Word/Excel 多列表头按别名映射不丢列；SQL/EAST 类型识别为 SOURCE；取消信号穿透元数据提取不被吞。"""
    # 1) 5 列 Word/Excel 表（带序号与独立数据类型/是否必填/校验要求列）精准提取
    draft = _deterministic_field_draft(
        {
            "headers": ["序号", "数据项名称", "字段代码", "数据类型", "是否必填", "校验要求"],
            "row": ["1", "贷款金额", "DKJE", "DECIMAL(20,2)", "是", "必须大于0"],
        },
        terms_text="",
        locator={"sheet": "贷款表"},
    )
    assert draft.field_cn == "贷款金额"
    assert draft.field_en == "DKJE"
    assert draft.data_type == "NUMBER"
    assert draft.length == 20
    assert draft.decimal_places == 2
    assert draft.required is True
    assert draft.check_text_raw == "必须大于0"

    # 2) EAST / SQL 原文声明应判为 SOURCE 而非 INFERRED
    east_raw = "1 银行机构代码 YXJGDM C..30 必填\n2 贷款金额 DKJE DECIMAL(20,2) 大于0\n存款账号 | CKZH | VARCHAR(40) | 主键"
    assert type_basis_for(east_raw, "YXJGDM") == "SOURCE"
    assert type_basis_for(east_raw, "DKJE") == "SOURCE"
    assert type_basis_for(east_raw, "CKZH") == "SOURCE"

    # 3) 元数据提取中收到 CancellationToken 取消信号时立即向上抛 RunCancelledError（不被吞为兜底）
    session = _make_fk_enforced_session()
    doc, blk, _ = _seed_doc_block_table(session, table_code="T_CANCEL")
    blk.content_json = {"rows": [["字段中文名", "字段名", "字段校验"], ["待推断字段", "", "无类型说明"]], "headers": ["字段中文名", "字段名", "字段校验"]}
    session.commit()
    monkeypatch.setattr("app.catalog.tables.service.SessionLocal", lambda: session)

    class DummyLlm:
        def assign(self, *args, **kwargs):
            raise AssertionError("Should be cancelled before calling LLM")

    monkeypatch.setattr("app.catalog.tables.service.RealDesignLlm", lambda s: DummyLlm())
    tok = CancellationToken()
    tok.cancel("operator aborted")
    with pytest.raises(RunCancelledError):
        with llm_execution_scope(cancellation=tok):
            extract_metadata_for_block(blk.id, Settings())


def test_unstructured_raw_text_extracts_multiple_fields_and_code_set_review_guard(monkeypatch):
    """槽点 A & B 回归：
    1. 非结构化纯文本块（raw_fields=[{"raw_text": ...}]）允许 AI 自由提取出多个字段，不再被 expected 1 卡死或 zip 丢字段；
    2. 码表 AI 复核护栏把虚构码值 ERROR 与超 6000 字截断后的遗漏 ERROR 降级为 WARNING。
    """
    from app.catalog.code_sets.service import enforce_code_set_review_evidence

    session = _make_fk_enforced_session()
    doc, blk, _ = _seed_doc_block_table(session, table_code="UNSTRUCTURED_T")
    blk.raw_content = "本表包含三个核心字段：客户号(CUST_NO，32位字符，必填)、交易金额(TX_AMT，数值型18位2位小数)、交易币种(TX_CCY，3位字符)。"
    blk.content_json = {"table_code": "UNSTRUCTURED_T", "table_name": "非结构化说明表"}
    session.commit()
    monkeypatch.setattr("app.catalog.tables.service.SessionLocal", lambda: session)

    class MultiFieldLlm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            if tool_name == "assign_metadata":
                return {
                    "table_definition": "非结构化文本提取的三字段表",
                    "fields": [
                        {"field_cn": "客户号", "field_en": "CUST_NO", "data_type": "VARCHAR", "length": 32, "required": True, "check_text_raw": "32位字符，必填", "confidence": 0.9, "evidence_json": {"p": 1}},
                        {"field_cn": "交易金额", "field_en": "TX_AMT", "data_type": "NUMBER", "length": 18, "decimal_places": 2, "required": False, "check_text_raw": "数值型18位2位小数", "confidence": 0.9, "evidence_json": {"p": 1}},
                        {"field_cn": "交易币种", "field_en": "TX_CCY", "data_type": "VARCHAR", "length": 3, "required": False, "check_text_raw": "3位字符", "confidence": 0.9, "evidence_json": {"p": 1}},
                    ],
                }, {"provider": "mock", "tool_name": tool_name}
            return {"issues": []}, {"provider": "mock", "tool_name": tool_name}

    monkeypatch.setattr("app.catalog.tables.service.RealDesignLlm", lambda s: MultiFieldLlm())
    tbl_id = extract_metadata_for_block(blk.id, Settings(), auto_link=False)
    extracted_fields = (
        session.query(FieldMetadataModel)
        .filter(FieldMetadataModel.table_id == tbl_id)
        .order_by(FieldMetadataModel.sort_order)
        .all()
    )
    assert [f.field_en for f in extracted_fields] == ["CUST_NO", "TX_AMT", "TX_CCY"]
    assert extracted_fields[1].data_type == "NUMBER" and extracted_fields[1].decimal_places == 2

    # 2) 码表 AI 复核防幻觉护栏
    cs_draft = CodeSetDraft(
        code="CCY_GUARD",
        name="币种表",
        values=[CodeValueDraft(value="CNY", label_cn="人民币"), CodeValueDraft(value="USD", label_cn="美元")],
    )
    checked = enforce_code_set_review_evidence(
        [
            {"severity": "ERROR", "code": "LABEL_MISMATCH", "value": "ZZZ_FAKE", "message": "虚构码值错配"},
            {"severity": "ERROR", "code": "MISSING_VALUES", "value": "", "message": "疑似漏提尾部码值"},
            {"severity": "ERROR", "code": "LABEL_MISMATCH", "value": "CNY", "message": "CNY 含义有误"},
        ],
        cs_draft,
        raw_content="CNY 人民币\nUSD 美元\n" + ("x" * 6500),
    )
    assert checked[0]["severity"] == "WARNING" and checked[0]["code"].startswith("UNVERIFIED_")
    assert checked[1]["severity"] == "WARNING" and checked[1]["code"].startswith("UNVERIFIED_")
    assert checked[2]["severity"] == "ERROR"  # 真实存在于码表的 ERROR 严格保留！

