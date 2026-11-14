import uuid

import pytest

from app.asset_models import RawBlockModel, SourceDocumentModel
from app.structure_service import _apply_plan, _validate_plan
from .test_workflow import make_session


def make_document_with_blocks(session):
    document = SourceDocumentModel(
        id=str(uuid.uuid4()),
        file_name="sample.docx",
        file_type="docx",
        sha256=uuid.uuid4().hex,
        blob_uri="blob://sample",
        spec_version="TEST",
        status="UPLOADED",
        block_count=2,
    )
    first = RawBlockModel(
        id=str(uuid.uuid4()), document_id=document.id, ordinal=0, block_type="DOCX_TABLE",
        raw_content="字段名 | 校验\nCUSTNAME | 必填", source_locator={"table_index": 0},
        content_json={"headers": ["字段名", "校验"], "rows": []}, raw_hash="a",
    )
    second = RawBlockModel(
        id=str(uuid.uuid4()), document_id=document.id, ordinal=1, block_type="NARRATIVE",
        raw_content="附加说明", source_locator={"table_index": 1}, content_json={}, raw_hash="b",
    )
    session.add_all([document, first, second])
    session.commit()
    return document, first, second


def test_structure_plan_merges_without_losing_original_blocks():
    session = make_session()
    document, first, second = make_document_with_blocks(session)
    args = {
        "blocks": [
            {
                "source_block_ids": [first.id, second.id],
                "role": "TABLE_METADATA",
                "title": "合并字段表",
                "column_mapping": {"field_en": "字段名", "check_text_raw": "校验"},
                "human_required": True,
                "warnings": ["合并表头需要人工确认"],
                "split_markers": [],
            }
        ],
        "global_warnings": [],
    }
    _validate_plan(args, [first, second])
    _apply_plan(session, document, [first, second], args)
    session.commit()
    assert first.content_json["superseded"] is True
    assert second.content_json["superseded"] is True
    merged = session.query(RawBlockModel).filter(RawBlockModel.document_id == document.id, RawBlockModel.ordinal == 2).one()
    assert "CUSTNAME" in merged.raw_content
    assert merged.content_json["human_required"] is True
    assert document.block_count == 1


def test_structure_plan_rejects_uncovered_source_block():
    session = make_session()
    _, first, second = make_document_with_blocks(session)
    with pytest.raises(ValueError, match="does not cover"):
        _validate_plan({"blocks": [{"source_block_ids": [first.id], "role": "IGNORE"}]}, [first, second])
import uuid

from app.asset_models import RawBlockModel, SourceDocumentModel
from app.structure_service import analyze_document_structure
from .test_workflow import make_session


class SingleCallLlm:
    def __init__(self):
        self.calls = []

    def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
        self.calls.append(timeout)
        ids = [block["id"] for block in payload["candidate_blocks"]]
        return {
            "blocks": [
                {
                    "source_block_ids": ids,
                    "role": "TABLE_METADATA",
                    "title": "测试结构",
                    "column_mapping": {},
                    "human_required": False,
                    "warnings": [],
                    "split_markers": [],
                }
            ],
            "global_warnings": [],
        }, {"arguments": {"global_warnings": []}}


def make_document(session, eligible):
    document = SourceDocumentModel(
        id=str(uuid.uuid4()),
        file_name="sample.pdf",
        file_type="pdf",
        sha256=uuid.uuid4().hex,
        blob_uri="blob://sample",
        spec_version="TEST",
        status="UPLOADED",
        block_count=1,
    )
    block = RawBlockModel(
        id=str(uuid.uuid4()),
        document_id=document.id,
        ordinal=1,
        block_type="TABLE_CANDIDATE",
        raw_content="ACTIONTYPE 操作类型 必填",
        source_locator={"section": "6.4.2"},
        content_json={"eligible": eligible, "spec": {"table_code": "T"} if eligible else None},
        raw_hash="test",
    )
    session.add_all([document, block])
    session.commit()
    return document, block


def test_ambiguous_structure_uses_single_short_llm_call(monkeypatch):
    session = make_session()
    monkeypatch.setattr("app.structure_service.SessionLocal", lambda: session)
    document, _ = make_document(session, eligible=False)
    llm = SingleCallLlm()
    monkeypatch.setattr("app.structure_service.RealDesignLlm", lambda settings: llm)
    architecture_id = analyze_document_structure(document.id, __import__('app.config', fromlist=['Settings']).Settings())
    assert llm.calls == [25]
    assert architecture_id
    document = session.get(SourceDocumentModel, document.id)
    assert document.status == "STRUCTURE_REVIEWED"


def test_structured_block_skips_llm_architecture_call(monkeypatch):
    session = make_session()
    monkeypatch.setattr("app.structure_service.SessionLocal", lambda: session)
    document, _ = make_document(session, eligible=True)

    class NoCallLlm:
        def assign(self, *args, **kwargs):
            raise AssertionError("Structured block should not call LLM")

    monkeypatch.setattr("app.structure_service.RealDesignLlm", lambda settings: NoCallLlm())
    architecture_id = analyze_document_structure(document.id, __import__('app.config', fromlist=['Settings']).Settings())
    assert architecture_id
