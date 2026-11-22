import uuid

from app.infra.models import (
    FieldMetadataModel,
    MetadataRevisionModel,
    RawBlockModel,
    SourceDocumentModel,
    TableMetadataModel,
)
from app.assets.metadata_service import apply_field_type_import, type_basis_for, update_field
from .helpers import make_session

DECLARED_CONTEXT = """字段 内容 类型（长度） 校验
REMARK 备注 字符型，256 非必填项。
"""

NO_TYPE_CONTEXT = """字段 内容 类型（长度） 校验
REMARK 备注 —— 非必填项。
"""


def make_table(session, raw_content=DECLARED_CONTEXT, basis="SOURCE", data_type="VARCHAR", length=256):
    document = SourceDocumentModel(
        id=str(uuid.uuid4()),
        file_name="sample.pdf",
        file_type="pdf",
        sha256=uuid.uuid4().hex,
        blob_uri="blob://test",
        spec_version="TEST",
        status="RAW_EXTRACTED",
        block_count=1,
    )
    block = RawBlockModel(
        id=str(uuid.uuid4()),
        document_id=document.id,
        ordinal=7,
        block_type="TABLE_METADATA",
        raw_content=raw_content,
        source_locator={"table_code": "T1"},
        content_json={},
        raw_hash="abc",
    )
    table = TableMetadataModel(
        id=str(uuid.uuid4()),
        document_id=document.id,
        block_id=block.id,
        table_code="T1",
        table_name="测试表",
        table_definition="测试",
        status="AI_SELF_VALIDATED",
        current_version=1,
        source_ordinal=7,
    )
    field = FieldMetadataModel(
        id=str(uuid.uuid4()),
        table_id=table.id,
        field_cn="备注",
        field_en="REMARK",
        field_definition="备注",
        check_text_raw="非必填项",
        data_type=data_type,
        length=length,
        decimal_places=0,
        required=False,
        code_reference_text="",
        confidence=0.9,
        evidence_json={},
        sort_order=0,
        ai_value_json={},
        type_basis=basis,
    )
    session.add_all([document, block, table, field])
    session.commit()
    return table, field


def test_type_basis_follows_source_declaration():
    assert type_basis_for(DECLARED_CONTEXT, "REMARK") == "SOURCE"
    assert type_basis_for(NO_TYPE_CONTEXT, "REMARK") == "INFERRED"


def test_editing_type_marks_manual_and_editing_definition_does_not():
    session = make_session()
    table, field = make_table(session)
    update_field(session, table, field, {"length": 512}, 1, "编辑员", "放宽长度")
    assert field.type_basis == "MANUAL"

    session2 = make_session()
    table2, field2 = make_table(session2)
    update_field(session2, table2, field2, {"field_definition": "备注说明"}, 1, "编辑员", "改定义")
    assert field2.type_basis == "SOURCE"


def test_batch_import_updates_types_and_marks_manual():
    session = make_session()
    table, field = make_table(session)
    rows = [
        ["报表代码", "字段英文名", "目标类型", "目标长度", "目标小数位"],
        ["T1", "REMARK", "字符型", "512", "0"],
        ["T1", "REMARK", "字符型", "512", "0"],
    ]
    result = apply_field_type_import(session, rows, editor="导入员")
    assert result["updated"] == 1
    assert result["errors"] == []
    assert field.length == 512
    assert field.type_basis == "MANUAL"
    assert session.query(MetadataRevisionModel).filter(MetadataRevisionModel.revision_kind == "HUMAN_EDIT").count() == 1


def test_batch_import_reports_bad_rows_without_aborting():
    session = make_session()
    table, field = make_table(session)
    rows = [
        ["报表代码", "字段英文名", "目标类型", "目标长度"],
        ["T1", "NOT_EXIST", "字符型", "10"],
        ["T1", "REMARK", "乱写", "10"],
        ["T1", "REMARK", "数值型", "12"],
    ]
    result = apply_field_type_import(session, rows, editor="导入员")
    assert result["updated"] == 1
    assert [item["line"] for item in result["errors"]] == [2, 3]
    assert field.data_type == "NUMBER"
    assert field.length == 12


def test_batch_import_dry_run_does_not_write():
    session = make_session()
    table, field = make_table(session)
    rows = [["字段英文名", "目标长度"], ["REMARK", "1024"]]
    result = apply_field_type_import(session, rows, editor="导入员", dry_run=True)
    assert result["updated"] == 1
    assert result["dry_run"] is True
    assert field.length == 256
    assert field.type_basis == "SOURCE"


def test_batch_import_rejects_file_without_required_columns():
    session = make_session()
    make_table(session)
    try:
        apply_field_type_import(session, [["随便一列"], ["x"]], editor="导入员")
    except ValueError as exc:
        assert "字段英文名" in str(exc)
    else:
        raise AssertionError("缺少必需列时应报错")
