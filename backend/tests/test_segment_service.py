import uuid

from app.assets.metadata_mutations import merge_metadata_tables
from app.infra.db_tables import RawBlockModel, TableMetadataModel
from app.ingestion.structure_service import _split_segments, analyze_block_segments
from .test_metadata_service import make_metadata
from .helpers import make_session


class SegmentFixtureLlm:
    def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
        return {
            "segments": [
                {"segment_type": "TERMINOLOGY", "title": "术语", "start_marker": "术语", "end_marker": "<REC>", "requires_human": False, "warnings": []},
                {"segment_type": "XML_FORMAT", "title": "XML", "start_marker": "<REC>", "end_marker": "字段校验", "requires_human": False, "warnings": []},
                {"segment_type": "DATA_DICTIONARY", "title": "字典", "start_marker": "字段校验", "end_marker": "主键", "requires_human": False, "warnings": []},
                {"segment_type": "PRIMARY_KEY", "title": "主键", "start_marker": "主键", "end_marker": "", "requires_human": False, "warnings": []},
            ]
        }, {"model": "fixture", "arguments": {}}


def make_parent(session):
    document = __import__('app.infra.db_tables', fromlist=['SourceDocumentModel']).SourceDocumentModel(
        id=str(uuid.uuid4()), file_name="sample.pdf", file_type="pdf", sha256=uuid.uuid4().hex,
        blob_uri="blob://sample", spec_version="TEST", status="UPLOADED", block_count=1,
    )
    parent = RawBlockModel(
        id=str(uuid.uuid4()), document_id=document.id, ordinal=1, block_type="TABLE_METADATA",
        raw_content="术语\n<REC>\n字段校验\n主键", source_locator={"section": "6.4.2"},
        content_json={"column_mapping": {}}, raw_hash="parent",
    )
    session.add_all([document, parent])
    session.commit()
    return parent


def test_block_segments_are_hierarchical_and_keep_parent_evidence(monkeypatch):
    session = make_session()
    monkeypatch.setattr("app.ingestion.structure_service.SessionLocal", lambda: session)
    parent = make_parent(session)
    ids = analyze_block_segments(parent.id, __import__('app.infra.config', fromlist=['Settings']).Settings(), SegmentFixtureLlm())
    assert len(ids) == 4
    parent = session.get(RawBlockModel, parent.id)
    assert parent.content_json["superseded"] is True
    children = session.query(RawBlockModel).filter(RawBlockModel.document_id == parent.document_id, RawBlockModel.id != parent.id).all()
    assert [child.block_type for child in children] == ["TERMINOLOGY", "XML_FORMAT", "DATA_DICTIONARY", "PRIMARY_KEY"]
    assert children[0].content_json["parent_block_id"] == parent.id


def test_split_segments_rejects_missing_marker():
    try:
        _split_segments("A\nB", {"segments": [{"start_marker": "MISSING", "end_marker": ""}]})
    except ValueError as exc:
        assert "not found" in str(exc)
    else:
        raise AssertionError("Missing marker was accepted")


def test_merge_metadata_tables_prefers_data_dictionary():
    session = make_session()
    parent = make_parent(session)
    first, _ = make_metadata(session)
    second, _ = make_metadata(session)
    result = merge_metadata_tables(session, parent.id, [first.id, second.id])
    assert result
    statuses = {row.id: row.status for row in session.query(TableMetadataModel).all()}
    assert "MERGED" in statuses.values()


def test_split_segments_preserves_prefix_and_preserves_doc_block_count_and_ordinal_zero(monkeypatch):
    """回归：首段前有前置文本时不炸 KeyError('_start')；ordinal=0 父块细分不撞唯一键且不覆盖整份文档 block_count。"""
    from app.infra.config import Settings
    from app.infra.db_tables import SourceDocumentModel

    # 1) start_marker 不在第 0 字符时，块前说明正确保留不炸 KeyError
    segs = _split_segments(
        "章节总说明\n术语：金额定义\n字段校验：必填",
        {
            "segments": [
                {"segment_type": "TERMINOLOGY", "title": "术语", "start_marker": "术语："},
                {"segment_type": "DATA_DICTIONARY", "title": "字典", "start_marker": "字段校验："},
            ]
        },
    )
    assert [s["segment_type"] for s in segs] == ["NARRATIVE", "TERMINOLOGY", "DATA_DICTIONARY"]
    assert segs[0]["content"] == "章节总说明"

    # 2) ordinal=0 父块 + 存在其他兄弟块时：细分不撞 uq_raw_block_order 且 block_count 为全部活跃块数
    session = make_session()
    monkeypatch.setattr("app.ingestion.structure_service.SessionLocal", lambda: session)
    doc = SourceDocumentModel(
        id=str(uuid.uuid4()), file_name="ord0.pdf", file_type="pdf", sha256=uuid.uuid4().hex,
        blob_uri="blob://ord0", spec_version="TEST", status="UPLOADED", block_count=2,
    )
    parent = RawBlockModel(
        id=str(uuid.uuid4()), document_id=doc.id, ordinal=0, block_type="TABLE_METADATA",
        raw_content="前置导语\n术语\n<REC>\n字段校验\n主键", source_locator={"section": "6.1"},
        content_json={"column_mapping": {}}, raw_hash="p0",
    )
    sibling = RawBlockModel(
        id=str(uuid.uuid4()), document_id=doc.id, ordinal=1, block_type="TABLE_METADATA",
        raw_content="另一张独立表", source_locator={"section": "6.2"},
        content_json={}, raw_hash="s1",
    )
    session.add_all([doc, parent, sibling])
    session.commit()

    ids = analyze_block_segments(parent.id, Settings(), SegmentFixtureLlm())
    assert len(ids) == 5  # 前置导语 + 4 个语义段
    refreshed_doc = session.get(SourceDocumentModel, doc.id)
    # 5 个新子块 + 1 个未动兄弟块 = 6（不能被覆盖成 5）
    assert refreshed_doc.block_count == 6
