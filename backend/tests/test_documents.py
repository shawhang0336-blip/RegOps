import hashlib
import io
import uuid

import pytest
from fastapi import HTTPException
from openpyxl import Workbook

from app.api.v2 import code_sets, documents, metadata
from app.infra.asset_models import (
    CodeSetModel,
    CodeValueModel,
    DocumentArchitectureModel,
    RawBlockModel,
    SourceDocumentModel,
    TableMetadataModel,
)
from app.infra.config import Settings
from .helpers import make_session


def make_document(session, digest=None):
    document = SourceDocumentModel(
        id=str(uuid.uuid4()),
        file_name="sample.xlsx",
        file_type="xlsx",
        sha256=digest or uuid.uuid4().hex,
        blob_uri="blob://sample",
        spec_version="TEST",
        status="UPLOADED",
        block_count=1,
    )
    block = RawBlockModel(
        id=str(uuid.uuid4()),
        document_id=document.id,
        ordinal=0,
        block_type="XLSX_TABLE",
        raw_content="字段名 | 校验\nCUSTNAME | 必填",
        source_locator={"sheet": "字段字典"},
        content_json={"rows": [["字段名", "校验"], ["CUSTNAME", "必填"]]},
        raw_hash="test",
    )
    table = TableMetadataModel(
        id=str(uuid.uuid4()),
        document_id=document.id,
        block_id=block.id,
        table_code="TEST_TABLE",
        table_name="测试表",
        table_definition="测试资产",
        status="DRAFT",
        current_version=1,
    )
    session.add_all([document, block, table])
    session.commit()
    return document, block, table


def test_delete_hides_document_and_preserves_assets(monkeypatch):
    session = make_session()
    monkeypatch.setattr(documents, "SessionLocal", lambda: session)
    document, block, table = make_document(session)

    result = documents.delete_document(document.id)

    assert result == {"id": document.id, "status": "DELETED", "assets_preserved": True}
    assert documents.list_documents() == []
    assert session.get(RawBlockModel, block.id) is not None
    assert session.get(TableMetadataModel, table.id) is not None


def test_uploading_deleted_document_rebuilds_blocks(monkeypatch, tmp_path):
    session = make_session()
    content = xlsx_content()
    document, old_block, table = make_document(session, hashlib.sha256(content).hexdigest())
    document.status = "DELETED"
    session.commit()
    configure_upload(session, monkeypatch, tmp_path)

    class OverviewLlm:
        def assign(self, *args, **kwargs):
            return {
                "document_kind": "COLLECTION_SPEC",
                "summary": "重新检查完成",
                "chunking_strategy": "SHEET_PER_TABLE",
                "expected_contents": ["TABLE_METADATA"],
                "requires_human": False,
                "warnings": [],
            }, {"arguments": {}}

    monkeypatch.setattr("app.ingestion.structure_service.RealDesignLlm", lambda settings: OverviewLlm())
    monkeypatch.setattr(documents, "extract_document", lambda path, file_type: ("TEST", [extracted_block()]))
    monkeypatch.setattr("app.ingestion.structure_service.extract_document", lambda path, file_type: ("TEST", [extracted_block()]))

    reimported = documents.ingest_uploaded_file("sample.xlsx", content)

    blocks = session.query(RawBlockModel).filter(RawBlockModel.document_id == document.id).all()
    assert reimported.id == document.id
    assert reimported.status == "UPLOADED"
    assert len(blocks) == 2
    assert session.get(RawBlockModel, old_block.id).content_json["superseded"] is True
    assert sum(not block.content_json.get("superseded") for block in blocks) == 1
    assert session.get(TableMetadataModel, table.id) is not None
    assert [row["id"] for row in documents.list_documents()] == [document.id]


def xlsx_content():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "字段字典"
    sheet.append(["字段名", "字段校验"])
    sheet.append(["CUSTNAME", "必填"])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def extracted_block():
    return {
        "ordinal": 0,
        "block_type": "XLSX_SHEET",
        "raw_content": "字段名 | 字段校验\nCUSTNAME | 必填",
        "source_locator": {"sheet": "字段字典"},
        "content_json": {"rows": [["字段名", "字段校验"], ["CUSTNAME", "必填"]]},
        "raw_hash": "test",
    }


def configure_upload(session, monkeypatch, tmp_path):
    monkeypatch.setattr(documents, "SessionLocal", lambda: session)
    monkeypatch.setattr(documents, "get_settings", lambda: Settings(blob_dir=str(tmp_path)))
    monkeypatch.setattr("app.ingestion.structure_service.SessionLocal", lambda: session)


def test_document_overview_runs_before_initial_chunking(monkeypatch, tmp_path):
    session = make_session()
    configure_upload(session, monkeypatch, tmp_path)
    calls = []

    class OverviewLlm:
        def assign(self, tool_name, instruction, payload, timeout=None):
            calls.append(tool_name)
            assert payload["survey"]["available_chunk_strategy"] == "SHEET_PER_TABLE"
            return {
                "document_kind": "COLLECTION_SPEC",
                "summary": "一个字段字典工作表",
                "chunking_strategy": "SHEET_PER_TABLE",
                "expected_contents": ["TABLE_METADATA"],
                "requires_human": False,
                "warnings": [],
            }, {"arguments": {}}

    monkeypatch.setattr("app.ingestion.structure_service.RealDesignLlm", lambda settings: OverviewLlm())
    monkeypatch.setattr(documents, "extract_document", lambda path, file_type: (calls.append("INITIAL_CHUNK") or ("TEST", [extracted_block()])))

    document = documents.ingest_uploaded_file("sample.xlsx", xlsx_content())

    assert calls == ["inspect_document_structure", "INITIAL_CHUNK"]
    assert document.status == "UPLOADED"
    assert document.block_count == 1
    assert session.query(RawBlockModel).filter(RawBlockModel.document_id == document.id).count() == 1


def test_failed_overview_blocks_chunking_and_can_retry(monkeypatch, tmp_path):
    session = make_session()
    configure_upload(session, monkeypatch, tmp_path)
    should_fail = True

    class OverviewLlm:
        def assign(self, *args, **kwargs):
            if should_fail:
                raise RuntimeError("offline test failure")
            return {
                "document_kind": "COLLECTION_SPEC",
                "summary": "结构已确认",
                "chunking_strategy": "SHEET_PER_TABLE",
                "expected_contents": ["TABLE_METADATA"],
                "requires_human": False,
                "warnings": [],
            }, {"arguments": {}}

    def extract(path, file_type):
        assert not should_fail, "Initial chunking must not run after a failed overview"
        return "TEST", [extracted_block()]

    monkeypatch.setattr("app.ingestion.structure_service.RealDesignLlm", lambda settings: OverviewLlm())
    monkeypatch.setattr(documents, "extract_document", extract)

    document = documents.ingest_uploaded_file("sample.xlsx", xlsx_content())
    assert document.status == "OVERVIEW_FAILED"
    assert document.block_count == 0
    assert session.query(RawBlockModel).filter(RawBlockModel.document_id == document.id).count() == 0

    should_fail = False
    monkeypatch.setattr(documents.threading, "Thread", _InlineThread)
    result = documents.prepare_document(document.id)
    assert result["status"] == "PREPARING"
    # 接口立即返回，真实结果落在 document 上
    prepared = session.get(SourceDocumentModel, document.id)
    assert prepared.status == "UPLOADED"
    assert prepared.block_count == 1


def test_confirming_block_clears_pending_state(monkeypatch):
    session = make_session()
    monkeypatch.setattr(documents, "SessionLocal", lambda: session)
    document, block, _ = make_document(session)
    block.content_json = {**block.content_json, "human_required": True, "structure_warnings": ["请核对字段长度"]}
    session.commit()

    result = documents.adjust_structure(document.id, block.id, {"human_required": False, "editor": "测试审核员"})

    assert result["content_json"]["human_required"] is False
    architecture = session.query(DocumentArchitectureModel).filter(DocumentArchitectureModel.document_id == document.id).one()
    assert architecture.warnings_json == []
    assert architecture.trace_json["editor"] == "测试审核员"


def test_assets_can_be_scoped_to_selected_document(monkeypatch):
    session = make_session()
    monkeypatch.setattr(documents, "SessionLocal", lambda: session)
    monkeypatch.setattr(metadata, "SessionLocal", lambda: session)
    monkeypatch.setattr(code_sets, "SessionLocal", lambda: session)
    first, first_block, first_table = make_document(session)
    second, second_block, second_table = make_document(session)
    first_set = CodeSetModel(id=str(uuid.uuid4()), code="FIRST", name="第一份码表", definition="", source_type="DOCUMENT_TABLE", status="DRAFT", current_version=1)
    second_set = CodeSetModel(id=str(uuid.uuid4()), code="SECOND", name="第二份码表", definition="", source_type="DOCUMENT_TABLE", status="DRAFT", current_version=1)
    session.add_all([
        first_set,
        second_set,
        CodeValueModel(id=str(uuid.uuid4()), code_set_id=first_set.id, value="A", label_cn="甲", source_locator={"block_id": first_block.id}),
        CodeValueModel(id=str(uuid.uuid4()), code_set_id=second_set.id, value="B", label_cn="乙", source_locator={"block_id": second_block.id}),
    ])
    session.commit()

    assert [row["id"] for row in metadata.list_tables(document_id=first.id)] == [first_table.id]
    assert [row["id"] for row in metadata.list_tables(document_id=second.id)] == [second_table.id]
    assert [row["id"] for row in code_sets.list_code_sets(document_id=first.id)] == [first_set.id]
    assert [row["id"] for row in code_sets.list_code_sets(document_id=second.id)] == [second_set.id]


class _InlineThread:
    """同步执行 target 的假线程，让后台流程在测试里可断言。"""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}
        self.daemon = daemon

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)


def test_extraction_records_completion_state(monkeypatch):
    session = make_session()
    monkeypatch.setattr(documents, "SessionLocal", lambda: session)
    document, block, table = make_document(session)
    monkeypatch.setattr(documents, "analyze_document_structure", lambda *args: None)
    monkeypatch.setattr(
        documents,
        "extract_parallel_for_blocks",
        lambda *args, **kwargs: {"table_ids": [table.id], "code_set_ids": [], "errors": [], "resolved_reference_count": 0},
    )
    monkeypatch.setattr(documents.threading, "Thread", _InlineThread)

    result = documents.extract_document_parallel(document.id, documents.ParallelExtractRequest(block_ids=[block.id]))

    # 接口是 202：立刻返回，真正的完成状态落在 document 上
    assert result["status"] == "EXTRACTING"
    assert session.get(SourceDocumentModel, document.id).status == "EXTRACTED"
    architecture = session.query(DocumentArchitectureModel).filter(DocumentArchitectureModel.document_id == document.id).one()
    assert architecture.plan_json["stage"] == "EXTRACTION"


def test_extraction_exception_does_not_leave_running_state(monkeypatch):
    session = make_session()
    monkeypatch.setattr(documents, "SessionLocal", lambda: session)
    document, block, _ = make_document(session)
    monkeypatch.setattr(documents, "analyze_document_structure", lambda *args: None)
    def fail(*args, **kwargs):
        raise RuntimeError("offline extraction failure")
    monkeypatch.setattr(documents, "extract_parallel_for_blocks", fail)
    monkeypatch.setattr(documents.threading, "Thread", _InlineThread)

    # 异步执行：异常不会冒泡给调用方，但必须落到 document 状态上，不能停留在 EXTRACTING
    documents.extract_document_parallel(document.id, documents.ParallelExtractRequest(block_ids=[block.id]))
    assert session.get(SourceDocumentModel, document.id).status == "EXTRACTION_FAILED"

    assert session.get(SourceDocumentModel, document.id).status == "EXTRACTION_FAILED"
