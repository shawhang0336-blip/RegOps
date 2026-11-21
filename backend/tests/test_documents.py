import hashlib
import io
import uuid

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
from app.infra.task_runner import TaskRunner
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


def test_delete_hides_document_and_preserves_assets():
    session = make_session()
    document, block, table = make_document(session)

    result = documents.delete_document(document.id, session=session)

    assert result == {"id": document.id, "status": "DELETED", "assets_preserved": True}
    assert documents.list_documents(session=session) == []
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
    assert [row["id"] for row in documents.list_documents(session=session)] == [document.id]


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
    inline_runner = TaskRunner(inline=True)
    monkeypatch.setattr(documents, "task_runner", lambda: inline_runner)
    result = documents.prepare_document(document.id, session=session)
    assert result["status"] == "PREPARING"
    prepared = session.get(SourceDocumentModel, document.id)
    assert prepared.status == "UPLOADED"
    assert prepared.block_count == 1


def test_confirming_block_clears_pending_state():
    session = make_session()
    document, block, _ = make_document(session)
    block.content_json = {**block.content_json, "human_required": True, "structure_warnings": ["请核对字段长度"]}
    session.commit()

    result = documents.adjust_structure(
        document.id,
        block.id,
        {"human_required": False, "editor": "测试审核员"},
        session=session,
    )

    assert result["content_json"]["human_required"] is False
    architecture = session.query(DocumentArchitectureModel).filter(DocumentArchitectureModel.document_id == document.id).one()
    assert architecture.warnings_json == []
    assert architecture.trace_json["editor"] == "测试审核员"


def test_assets_can_be_scoped_to_selected_document():
    session = make_session()
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

    assert [row["id"] for row in metadata.list_tables(document_id=first.id, session=session)] == [first_table.id]
    assert [row["id"] for row in metadata.list_tables(document_id=second.id, session=session)] == [second_table.id]
    assert [row["id"] for row in code_sets.list_code_sets(document_id=first.id, session=session)] == [first_set.id]
    assert [row["id"] for row in code_sets.list_code_sets(document_id=second.id, session=session)] == [second_set.id]


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
    inline_runner = TaskRunner(inline=True)
    monkeypatch.setattr(documents, "task_runner", lambda: inline_runner)

    result = documents.extract_document_parallel(
        document.id,
        documents.ParallelExtractRequest(block_ids=[block.id]),
        session=session,
    )

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
    inline_runner = TaskRunner(inline=True)
    monkeypatch.setattr(documents, "task_runner", lambda: inline_runner)

    documents.extract_document_parallel(
        document.id,
        documents.ParallelExtractRequest(block_ids=[block.id]),
        session=session,
    )
    assert session.get(SourceDocumentModel, document.id).status == "EXTRACTION_FAILED"


def test_ingestion_run_persists_eventlog_budget_cancel_recovery_and_isolates_from_generation_runs(monkeypatch):
    """P2 回归：Step 1 文档解析纳入 kernel.run，支持 EventLog 重启还原、BudgetGuard 记账、取消/回收，且不污染 Step 2 批次列表。"""
    from app.api.runs import list_runs
    from app.engine.ingestion_run import (
        cancel_document_run,
        list_document_runs,
        recover_interrupted_ingestion_runs,
    )
    from app.ingestion.import_progress import forget as progress_forget
    from app.kernel.run import create_run as kernel_create_run
    from app.policy.llm_guard import call_llm_validated

    session = make_session()
    monkeypatch.setattr(documents, "SessionLocal", lambda: session)
    document, block, table = make_document(session)
    monkeypatch.setattr(documents, "analyze_document_structure", lambda *args: None)

    class UsageLlm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            return {"fields": []}, {
                "provider": "test",
                "model": "test",
                "elapsed_seconds": 0.25,
                "usage": {"prompt_tokens": 200, "completion_tokens": 50, "total_tokens": 250},
            }

    def fake_extract_parallel(block_ids, settings, targets, on_progress=None):
        # 通过 llm_execution_scope 自动接入 Run 级 BudgetGuard
        call_llm_validated(UsageLlm(), "assign_metadata", "instr", {})
        if on_progress:
            on_progress(1, 1, 1)
        return {"table_ids": [table.id], "code_set_ids": [], "errors": [], "resolved_reference_count": 0}

    monkeypatch.setattr(documents, "extract_parallel_for_blocks", fake_extract_parallel)
    inline_runner = TaskRunner(inline=True)
    monkeypatch.setattr(documents, "task_runner", lambda: inline_runner)

    res = documents.extract_document_parallel(
        document.id,
        documents.ParallelExtractRequest(block_ids=[block.id]),
        session=session,
    )
    assert res["run_id"]

    # 1. Ingestion Run 完整落入 EventLog，含哈希链与 BudgetGuard 消耗统计
    doc_runs = list_document_runs(session, document.id)
    assert len(doc_runs) == 1
    assert doc_runs[0]["id"] == res["run_id"]
    assert doc_runs[0]["status"] == "COMPLETED"
    assert doc_runs[0]["chain_valid"] is True
    assert doc_runs[0]["budget_usage"]["llm_calls"] == 1
    assert doc_runs[0]["budget_usage"]["total_tokens"] == 250

    # 2. 模拟进程重启清空内存 import_progress -> GET /progress 从 EventLog 无损还原
    progress_forget(document.id)
    prog = documents.document_progress(document.id, session=session)
    assert prog["run_id"] == res["run_id"]
    assert prog["status"] == "DONE"
    assert prog["stage"] == 4
    assert prog["budget_usage"]["total_tokens"] == 250
    assert len(prog["logs"]) >= 1

    # 3. 隔离性：Step 1 Ingestion Run 不得混入 Step 2 验证批次列表
    assert list_runs(session=session) == []

    # 4. 取消 + 重启僵尸回收
    hung = kernel_create_run(
        session,
        document.id,
        initial_node="extract",
        initial_payload={"run_kind": "INGESTION", "document_id": document.id, "file_name": document.file_name, "phase": "extract", "stage": 4},
    )
    doc_row = session.get(SourceDocumentModel, document.id)
    doc_row.status = "EXTRACTING"
    session.commit()
    cancel_res = cancel_document_run(session, document.id, reason="测试取消")
    assert cancel_res["cancelled"] is True
    assert session.get(SourceDocumentModel, document.id).status == "EXTRACTION_FAILED"

    crashed = kernel_create_run(
        session,
        document.id,
        initial_node="prepare",
        initial_payload={"run_kind": "INGESTION", "document_id": document.id, "file_name": document.file_name, "phase": "prepare", "stage": 2},
    )
    doc_row = session.get(SourceDocumentModel, document.id)
    doc_row.status = "EXTRACTING"
    session.commit()
    recovered = recover_interrupted_ingestion_runs(session)
    assert crashed.id in recovered
    assert hung.id not in recovered
    assert session.get(SourceDocumentModel, document.id).status == "EXTRACTION_FAILED"

