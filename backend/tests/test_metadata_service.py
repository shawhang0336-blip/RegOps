import uuid

import pytest

from app.asset_models import FieldMetadataModel, MetadataValidationModel, RawBlockModel, SourceDocumentModel, TableMetadataModel
from app.config import Settings
from app.metadata_domain import MetadataFieldDraft, MetadataTableDraft, validate_metadata_draft
from app.metadata_service import _deterministic_field_draft, _persist_draft, extract_metadata_for_block, metadata_to_spec, publish_table, update_field
from .helpers import make_session


def make_metadata(session):
    document = SourceDocumentModel(
        id=str(uuid.uuid4()),
        file_name="sample.docx",
        file_type="docx",
        sha256=uuid.uuid4().hex,
        blob_uri="blob://test",
        spec_version="TEST",
        status="RAW_EXTRACTED",
        block_count=1,
    )
    block = RawBlockModel(
        id=str(uuid.uuid4()),
        document_id=document.id,
        ordinal=0,
        block_type="DOCX_TABLE",
        raw_content="机构名称 | CUSTNAME | 必填",
        source_locator={"table_index": 0},
        content_json={"rows": [["机构名称", "CUSTNAME", "必填"]]},
        raw_hash="abc",
    )
    table = TableMetadataModel(
        id=str(uuid.uuid4()),
        document_id=document.id,
        block_id=block.id,
        table_code="TEST_TABLE",
        table_name="测试表",
        table_definition="测试",
        status="AI_SELF_VALIDATED",
        current_version=1,
    )
    field = FieldMetadataModel(
        id=str(uuid.uuid4()),
        table_id=table.id,
        field_cn="机构名称",
        field_en="CUSTNAME",
        field_definition="机构名称",
        check_text_raw="必填",
        data_type="VARCHAR",
        length=128,
        decimal_places=0,
        required=True,
        code_reference_text="",
        confidence=0.95,
        evidence_json={"table_index": 0},
        sort_order=0,
        ai_value_json={},
    )
    session.add_all([document, block, table, field])
    session.commit()
    return table, field


def test_publish_edit_version_and_spec():
    session = make_session()
    table, field = make_metadata(session)
    publish_table(session, table, "测试发布员", 1)
    session.commit()
    assert table.status == "PUBLISHED"
    spec = metadata_to_spec(session, table)
    assert spec.fields[0].name == "CUSTNAME"
    with pytest.raises(ValueError, match="PUBLISHED_TABLE_IS_IMMUTABLE"):
        update_field(session, table, field, {"field_cn": "修改"}, 1, "编辑员", "test")


def test_optimistic_lock_rejects_stale_edit():
    session = make_session()
    table, field = make_metadata(session)
    with pytest.raises(ValueError, match="VERSION_CONFLICT"):
        update_field(session, table, field, {"field_cn": "修改"}, 2, "编辑员", "test")


def test_duplicate_field_is_validation_error():
    draft = MetadataTableDraft(
        table_code="TEST",
        table_name="测试",
        table_definition="",
        fields=[
            {"field_cn": "甲", "field_en": "A", "length": 10, "evidence_json": {"cell": "A2"}},
            {"field_cn": "乙", "field_en": "A", "length": 10, "evidence_json": {"cell": "A3"}},
        ],
    )
    issues = validate_metadata_draft(draft)
    assert any(issue["code"] == "DUPLICATE_FIELD" for issue in issues)


def test_identical_metadata_reextract_does_not_create_version():
    session = make_session()
    table, field = make_metadata(session)
    block = session.get(RawBlockModel, table.block_id)
    draft = MetadataTableDraft(
        table_code=table.table_code,
        table_name=table.table_name,
        table_definition=table.table_definition,
        fields=[
            {
                "field_cn": field.field_cn,
                "field_en": field.field_en,
                "field_definition": field.field_definition,
                "check_text_raw": field.check_text_raw,
                "data_type": field.data_type,
                "length": field.length,
                "decimal_places": field.decimal_places,
                "required": field.required,
                "code_reference_text": "",
                "confidence": field.confidence,
                "evidence_json": field.evidence_json,
            }
        ],
    )
    result = _persist_draft(session, block, draft, {})
    assert result.id == table.id
    assert result.current_version == 1
    assert session.query(FieldMetadataModel).count() == 1


def test_reextract_preserves_human_edited_version():
    session = make_session()
    table, field = make_metadata(session)
    update_field(session, table, field, {"field_cn": "人工确认名称"}, 1, "编辑员", "确认")
    block = session.get(RawBlockModel, table.block_id)
    draft = MetadataTableDraft(
        table_code=table.table_code,
        table_name=table.table_name,
        table_definition=table.table_definition,
        fields=[{"field_cn": "AI 名称", "field_en": "CUSTNAME", "length": 128}],
    )
    result = _persist_draft(session, block, draft, {})
    assert result.id != table.id
    assert result.current_version == table.current_version + 1
    assert session.get(FieldMetadataModel, field.id).field_cn == "人工确认名称"


def test_tabular_fallback_reads_numeric_type_and_precision():
    field = _deterministic_field_draft(
        {"row": ["交易金额", "AMOUNT", "必填项，数值型，22.2"], "headers": ["字段中文名", "字段名", "字段校验"]},
        "",
        {"sheet": "字段字典"},
        fallback_reason="ReadTimeout",
    )
    assert field.data_type == "NUMBER"
    assert field.length == 22
    assert field.decimal_places == 2
    assert field.confidence == 0.5


def test_metadata_requires_only_four_core_field_values():
    number = MetadataFieldDraft(field_cn="金额", field_en="AMT", data_type="NUMBER", check_text_raw="必填")
    text = MetadataFieldDraft(field_cn="名称", field_en="NAME", data_type="VARCHAR", check_text_raw="")

    assert number.length == 22
    assert number.field_definition == ""
    assert number.code_reference_text == ""
    assert text.length == 128


def test_failed_assignment_skips_second_llm_review(monkeypatch):
    session = make_session()
    table, _ = make_metadata(session)
    block = session.get(RawBlockModel, table.block_id)
    block.content_json = {"headers": ["字段中文名", "字段名", "字段校验"], "rows": [["字段中文名", "字段名", "字段校验"], ["机构名称", "CUSTNAME", "必填"]]}
    session.commit()
    monkeypatch.setattr("app.metadata_service.SessionLocal", lambda: session)
    calls = []

    class UnavailableLlm:
        def assign(self, tool_name, *args, **kwargs):
            calls.append(tool_name)
            raise RuntimeError("offline unavailable")

    monkeypatch.setattr("app.metadata_service.RealDesignLlm", lambda settings: UnavailableLlm())
    extract_metadata_for_block(block.id, Settings(), auto_link=False)

    assert calls == ["assign_metadata"]
    validations = session.query(MetadataValidationModel).filter(MetadataValidationModel.table_id == table.id).all()
    program = next(item for item in validations if item.validator_kind == "PROGRAM")
    review = next(item for item in validations if item.validator_kind == "AI_REVIEW")
    assert len([issue for issue in program.issues_json if issue["code"] == "LOW_CONFIDENCE"]) == 1
    assert review.issues_json[0]["code"] == "AI_REVIEW_UNAVAILABLE"
    assert review.issues_json[0]["severity"] == "WARNING"


def test_enforce_issue_evidence_downgrades_type_claims_without_source():
    from app.metadata_service import enforce_issue_evidence

    # 来源没有类型声明：类型类 ERROR 属于推断，必须降级
    raw = "指对应月度末，境外被投资机构合并资产负债表中的“资产总额”。"
    issues = [
        {"severity": "ERROR", "code": "TYPE_MISMATCH", "field_en": "A0117", "message": "应为 NUMBER(22,2)"},
        {"severity": "ERROR", "code": "MISSING_FIELD", "field_en": "A0119", "message": "原文有该字段但未录入"},
    ]
    result = enforce_issue_evidence(issues, raw)
    assert result[0]["severity"] == "WARNING"
    assert result[0]["code"].startswith("UNVERIFIED_")
    assert "未声明数据类型" in result[0]["message"]
    # 非类型类问题不受影响
    assert result[1]["severity"] == "ERROR"


def test_enforce_issue_evidence_keeps_claim_when_source_declares_type():
    from app.metadata_service import enforce_issue_evidence

    raw = "A0117   实收资本   数值型，22.2   必填项。"
    issues = [{"severity": "ERROR", "code": "TYPE_MISMATCH", "field_en": "A0117", "message": "当前是 VARCHAR(128)"}]
    result = enforce_issue_evidence(issues, raw)
    assert result[0]["severity"] == "ERROR"


def test_enforce_issue_evidence_downgrades_unfindable_quote():
    from app.metadata_service import enforce_issue_evidence

    raw = "A0117   实收资本   数值型，22.2   必填项。"
    issues = [
        {
            "severity": "ERROR",
            "code": "TYPE_LENGTH_MISMATCH",
            "field_en": "A0117",
            "message": "长度不符",
            "evidence": "原文要求字段长度为 999 位",
        }
    ]
    result = enforce_issue_evidence(issues, raw)
    assert result[0]["severity"] == "WARNING"
    assert "未在来源中找到" in result[0]["message"]


def test_table_needs_review_only_for_errors():
    from app.metadata_service import table_needs_review

    assert table_needs_review([{"severity": "WARNING"}, {"severity": "INFO"}]) is False
    assert table_needs_review([{"severity": "WARNING"}, {"severity": "ERROR"}]) is True
