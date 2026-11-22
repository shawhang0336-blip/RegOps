import uuid

from app.infra.models import RawBlockModel, TableMetadataModel
from app.ingestion.segment_service import _split_segments, analyze_block_segments, merge_metadata_tables
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
    document = __import__('app.infra.models', fromlist=['SourceDocumentModel']).SourceDocumentModel(
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
    monkeypatch.setattr("app.ingestion.segment_service.SessionLocal", lambda: session)
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
