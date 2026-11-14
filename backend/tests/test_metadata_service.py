import uuid

import pytest

from app.asset_models import FieldMetadataModel, RawBlockModel, SourceDocumentModel, TableMetadataModel
from app.metadata_domain import MetadataTableDraft, validate_metadata_draft
from app.metadata_service import metadata_to_spec, publish_table, update_field
from .test_workflow import make_session


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
