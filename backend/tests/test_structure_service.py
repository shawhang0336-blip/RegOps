import uuid
from datetime import datetime, timedelta

import pytest

from app.config import Settings
from app.asset_models import DocumentArchitectureModel, RawBlockModel, SourceDocumentModel
from app.structure_service import _apply_plan, _validate_plan, latest_document_architecture
from .helpers import make_session


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
import app.structure_service as structure_service
from .helpers import make_session


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
    assert llm.calls == [structure_service.STRUCTURE_PLAN_TIMEOUT]
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


def test_latest_architecture_reads_only_latest_record():
    session = make_session()
    document, _, _ = make_document_with_blocks(session)
    now = datetime.utcnow()
    older = DocumentArchitectureModel(
        id=str(uuid.uuid4()), document_id=document.id, status="OVERVIEW_FAILED",
        plan_json={"stage": "DOCUMENT_OVERVIEW"}, warnings_json=[], trace_json={}, created_at=now - timedelta(minutes=1),
    )
    latest = DocumentArchitectureModel(
        id=str(uuid.uuid4()), document_id=document.id, status="OVERVIEW_REVIEWED",
        plan_json={"stage": "DOCUMENT_OVERVIEW"}, warnings_json=[], trace_json={}, created_at=now,
    )
    other = DocumentArchitectureModel(
        id=str(uuid.uuid4()), document_id=document.id, status="AI_STRUCTURE_REVIEWED",
        plan_json={}, warnings_json=[], trace_json={}, created_at=now + timedelta(minutes=1),
    )
    session.add_all([older, latest, other])
    session.commit()

    selected = latest_document_architecture(
        session, document.id, statuses={"OVERVIEW_FAILED", "OVERVIEW_REVIEWED"}
    )

    assert selected.id == latest.id


def _block_with_spec(session, document, ordinal, table_code, field_names):
    block = RawBlockModel(
        id=str(uuid.uuid4()), document_id=document.id, ordinal=ordinal, block_type="TABLE_METADATA",
        raw_content=f"{table_code} 表原文", source_locator={"section": table_code},
        content_json={
            "eligible": True,
            "spec": {
                "table_code": table_code,
                "table_name": f"{table_code} 表",
                "fields": [{"name": name, "data_type": "VARCHAR", "length": 32, "content": name} for name in field_names],
            },
        },
        raw_hash=f"spec-{ordinal}",
    )
    session.add(block)
    session.commit()
    return block


def test_merging_parsed_block_inherits_its_spec():
    session = make_session()
    document, first, second = make_document_with_blocks(session)
    first.content_json = {**first.content_json}
    session.commit()
    parsed = _block_with_spec(session, document, 2, "SAFE_6_9_4", ["A0117", "A0118"])
    args = {
        "blocks": [
            {
                "source_block_ids": [parsed.id, second.id],
                "role": "TABLE_METADATA",
                "title": "跨页合并表",
                "column_mapping": {},
                "human_required": False,
                "warnings": [],
                "split_markers": [],
            }
        ],
        "global_warnings": [],
    }
    _apply_plan(session, document, [parsed, second], args)
    session.commit()
    merged = (
        session.query(RawBlockModel)
        .filter(RawBlockModel.document_id == document.id, RawBlockModel.ordinal > 2)
        .order_by(RawBlockModel.ordinal)
        .first()
    )
    # 合并不能把已经解析好的字段结构丢掉
    assert merged.content_json["parent_spec"]["table_code"] == "SAFE_6_9_4"
    assert len(merged.content_json["parent_spec"]["fields"]) == 2


def test_structured_guard_rejects_merging_two_parsed_blocks():
    session = make_session()
    document, _, _ = make_document_with_blocks(session)
    left = _block_with_spec(session, document, 2, "T_A", ["F1"])
    right = _block_with_spec(session, document, 3, "T_B", ["F2"])
    args = {
        "blocks": [
            {
                "source_block_ids": [left.id, right.id],
                "role": "TABLE_METADATA",
                "title": "错误合并",
                "column_mapping": {},
                "human_required": False,
                "warnings": [],
                "split_markers": [],
            }
        ],
        "global_warnings": [],
    }
    with pytest.raises(ValueError, match="合并多个已解析结构"):
        structure_service._validate_structured_guard(args, [left, right])


class _CountingLlm:
    def __init__(self):
        self.calls = 0

    def assign(self, *args, **kwargs):
        self.calls += 1
        return {"blocks": [], "global_warnings": []}, {"arguments": {}}


def test_adaptive_scope_skips_ai_for_single_parsed_block(monkeypatch):
    session = make_session()
    monkeypatch.setattr("app.structure_service.SessionLocal", lambda: session)
    document = SourceDocumentModel(
        id=str(uuid.uuid4()), file_name="only.xlsx", file_type="xlsx", sha256=uuid.uuid4().hex,
        blob_uri="blob://only", spec_version="TEST", status="UPLOADED", block_count=1,
    )
    session.add(document)
    session.commit()
    _block_with_spec(session, document, 0, "SAFE_6_1", ["CUSTNAME"])

    llm = _CountingLlm()
    monkeypatch.setattr("app.structure_service.RealDesignLlm", lambda settings: llm)
    structure_service.analyze_document_structure(document.id, Settings())

    # 自适模式下，已完成结构化解析、又没有任何告警的单块章节不需要再问 AI
    assert llm.calls == 0


def test_all_scope_sends_every_block_to_ai(monkeypatch):
    session = make_session()
    monkeypatch.setattr("app.structure_service.SessionLocal", lambda: session)
    document = SourceDocumentModel(
        id=str(uuid.uuid4()), file_name="only.xlsx", file_type="xlsx", sha256=uuid.uuid4().hex,
        blob_uri="blob://only", spec_version="TEST", status="UPLOADED", block_count=1,
    )
    session.add(document)
    session.commit()
    block = _block_with_spec(session, document, 0, "SAFE_6_1", ["CUSTNAME"])

    calls = {}

    class PassingLlm:
        def assign(self, tool, prompt, payload, timeout=None):
            calls["payload"] = payload
            return {
                "blocks": [
                    {
                        "source_block_ids": [block.id],
                        "role": "TABLE_METADATA",
                        "title": "6.1 组织机构基本情况表",
                        "column_mapping": {},
                        "human_required": False,
                        "warnings": [],
                        "split_markers": [],
                    }
                ],
                "global_warnings": [],
            }, {"arguments": {}}

    monkeypatch.setattr("app.structure_service.RealDesignLlm", lambda settings: PassingLlm())
    structure_service.analyze_document_structure(document.id, Settings(structure_ai_scope="all"))

    candidates = calls["payload"]["candidate_blocks"]
    assert len(candidates) == 1
    # 完整 spec 不能原样塞进 prompt，只给摘要
    assert "parsed_spec" in candidates[0]["structure"]
    assert "fields" not in candidates[0]["structure"]["parsed_spec"]
