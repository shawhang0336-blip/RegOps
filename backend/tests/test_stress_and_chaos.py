"""后端并发竞态、规模扩展性与故障注入（Stress & Chaos）回归测试套件。"""

from concurrent.futures import ThreadPoolExecutor
import io
from pathlib import Path
import threading
import time
from typing import Any, Callable, Dict, List, Tuple
import uuid

from fastapi import HTTPException
import httpx
from openpyxl import Workbook
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.catalog import metadata as metadata_api
from app.api.catalog.code_sets_service import (
    add_code_value_record,
    edit_code_value_record,
    publish_code_set_record,
)
from app.api.catalog.documents_service import (
    ingest_uploaded_file_with_flag,
    prepare_parallel_extraction,
    soft_delete_document,
)
from app.api.catalog.metadata_service import create_table_field
from app.api.generation.artifacts_service import (
    list_all_table_rules,
    list_artifacts_status,
    update_rule_params,
)
from app.catalog.code_sets.linking import auto_link_all_references
from app.catalog.tables import service as table_service
from app.catalog.tables.mutations import update_field
from app.delivery.workflow import (
    approve_generic_rules,
    confirm_generic_test_results,
    delete_generic_run,
    reaudit_generic_run,
    retry_generic_run,
)
from app.generation.core.rules import RuleDefinition, RuleValidationError, calculate_rule_hash
from app.infra.config import Settings
from app.infra.contracts import Evidence, FieldSpec, SpecDocument
from app.infra.db import Base
from app.infra.db_tables import (
    CodeSetModel,
    CodeValueModel,
    EventModel,
    FieldCodeReferenceModel,
    FieldMetadataModel,
    RawBlockModel,
    RuleModel,
    RunModel,
    SourceDocumentModel,
    SpecificationModel,
    TableMetadataModel,
)
from app.infra.errors import WorkflowError
from app.infra.llm import (
    LlmTimeoutError,
    call_llm_validated,
    llm_execution_scope,
    submit_with_context,
)
from app.kernel.bus import Bus, MAX_BUS_HISTORY
from app.kernel.event_log import append_event, verify_chain
from app.kernel.run import (
    BudgetExceededError,
    BudgetGuard,
    BudgetLimits,
    CancellationToken,
    RunCancelledError,
    create_run as kernel_create_run,
)
from app.policy.regops_bus import default_bus
from app.policy.regops_plan import interrupt_payload, project

from .helpers import make_session


def _make_stress_db(tmp_path: Path, name: str) -> Tuple[Engine, sessionmaker]:
    """构造与生产一致的并发 SQLite WAL 引擎与跨线程 Session 工厂。"""
    db_path = tmp_path / f"{name}.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return engine, session_factory


def _run_concurrent(
    tasks: List[Callable[[], Any]], *, barrier: bool = True
) -> Tuple[List[Any], List[Any]]:
    """在同一起跑栅栏后并发执行一组任务，收集每个线程的返回值或异常。"""
    gate = threading.Barrier(len(tasks)) if barrier and len(tasks) > 1 else None
    results: List[Any] = [None] * len(tasks)
    errors: List[Any] = [None] * len(tasks)

    def _worker(idx: int, fn: Callable[[], Any]) -> None:
        if gate is not None:
            gate.wait(timeout=10)
        try:
            results[idx] = fn()
        except Exception as exc:  # noqa: BLE001 - 压力测试需原样回收异常分类统计
            errors[idx] = exc

    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = [pool.submit(_worker, idx, fn) for idx, fn in enumerate(tasks)]
        for fut in futures:
            fut.result(timeout=30)
    return results, errors


def _xlsx_bytes(sheet_title: str = "字段字典") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    ws.append(["字段中文名", "字段名", "字段校验"])
    ws.append(["机构编码", "BRANCH_CODE", "必填项，字符型，12"])
    ws.append(["常驻国家", "COUNTRY_CODE", "必填项，见国家和地区代码表"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _sample_spec(table_code: str = "T_STRESS") -> SpecDocument:
    return SpecDocument(
        version="1.3",
        table_code=table_code,
        table_name="压力测试表",
        section="6.1",
        source_name="stress.pdf",
        source_sha256="a" * 64,
        terms_text="",
        dictionary_text="",
        fields=[
            FieldSpec(name="CODE", content="编码", data_type="VARCHAR", length=18, check_text="必填", evidence_page="1"),
            FieldSpec(name="AMOUNT", content="金额", data_type="NUMBER", length=18, decimal_places=2, check_text="大于0", evidence_page="1"),
        ],
    )


def _add_pending_rule(
    session: Session,
    spec_model: SpecificationModel,
    run: RunModel,
    rule_key: str = "R_STRESS",
    status: str = "PENDING",
) -> RuleModel:
    spec = SpecDocument(**spec_model.content_json["tables"][0])
    defn = RuleDefinition(
        rule_key=rule_key,
        table_code=spec.table_code,
        field="CODE",
        operator="REGEXP",
        params={"pattern": "^[A-Z0-9]{1,8}$"},
        source="AI_EXPANDED",
        evidence=Evidence(section="6.1", page="1", field="CODE", quote="编码规则"),
        rationale="压力测试规则",
    )
    defn.rule_hash = calculate_rule_hash(defn, spec.source_sha256)
    row = RuleModel(
        row_id=str(uuid.uuid4()),
        run_id=run.id,
        spec_id=spec_model.id,
        table_code=spec.table_code,
        rule_key=defn.rule_key,
        rule_hash=defn.rule_hash,
        source=defn.source,
        status=status,
        definition_json=defn.model_dump(),
    )
    session.add(row)
    return row


def _seed_spec_and_run(
    session: Session,
    *,
    approval_gate: bool = False,
    rule_status: str = "PENDING",
) -> Tuple[SpecificationModel, RunModel, RuleModel]:
    spec = _sample_spec()
    spec_model = SpecificationModel(
        id=str(uuid.uuid4()),
        version="METADATA_PUBLISHED",
        table_code="METADATA_SNAPSHOT",
        source_name=spec.source_name,
        source_sha256=spec.source_sha256,
        content_json={"table_name": "confirmed", "selection_confirmed": True, "tables": [spec.model_dump()]},
    )
    session.add(spec_model)
    session.flush()
    run = kernel_create_run(session, spec_model.id)
    append_event(session, run, "SCHEMA_ASSIGNED", "schema_mapper", {"tables": 1})
    rule = _add_pending_rule(session, spec_model, run, status=rule_status)
    if approval_gate:
        append_event(session, run, "RULES_BOUND", "rule_binder", {"pending_count": 1})
        append_event(
            session,
            run,
            "APPROVAL_REQUIRED",
            "human_gate",
            interrupt_payload("APPROVAL_REQUIRED", "请审批规则", {"pending_count": 1}),
        )
    session.commit()
    return spec_model, run, rule


def test_stress_concurrent_eventlog_hash_chain_and_rule_edits(tmp_path):
    """12 线程并发写 EventLog + 4 线程并发改同 Run 规则参数：链式哈希严丝合缝且零分叉。"""
    engine, Session = _make_stress_db(tmp_path, "eventlog_race")
    try:
        with Session() as seed:
            _, run, rule = _seed_spec_and_run(seed, approval_gate=True)
            run_id, row_id = run.id, rule.row_id

        def _append_worker(worker_idx: int) -> None:
            for step in range(5):
                with Session() as s:
                    r = s.get(RunModel, run_id)
                    append_event(s, r, "HEARTBEAT_STEP", f"worker_{worker_idx}", {"step": step})
                    s.commit()

        def _edit_worker(edit_idx: int) -> Dict[str, Any]:
            with Session() as s:
                res = update_rule_params(
                    s,
                    row_id,
                    params={"pattern": f"^[A-Z0-9]{{{edit_idx + 1},12}}$"},
                    editor=f"op_{edit_idx}",
                    reason=f"concurrent edit {edit_idx}",
                )
                return res

        tasks: List[Callable[[], Any]] = []
        for w in range(12):
            tasks.append(lambda idx=w: _append_worker(idx))
        for e in range(4):
            tasks.append(lambda idx=e: _edit_worker(idx))

        _, errors = _run_concurrent(tasks)
        assert [e for e in errors if e is not None] == []

        with Session() as verify:
            run_obj = verify.get(RunModel, run_id)
            events = (
                verify.query(EventModel)
                .filter(EventModel.run_id == run_id)
                .order_by(EventModel.seq.asc())
                .all()
            )
            # RUN_CREATED + SCHEMA_ASSIGNED + RULES_BOUND + APPROVAL_REQUIRED + 12*5 + 4 RULE_PARAMS_UPDATED
            assert len(events) == 4 + 60 + 4
            prev_hashes = [ev.prev_hash for ev in events]
            assert len(prev_hashes) == len(set(prev_hashes))
            assert verify_chain(verify, run_obj) is True
    finally:
        engine.dispose()


def test_stress_concurrent_same_file_upload_and_parallel_extraction_gate(tmp_path):
    """10 线程并发上传同 SHA256 文件零崩溃；6 线程并发抽取/删档门禁严格互斥。"""
    engine, Session = _make_stress_db(tmp_path, "upload_gate")
    content = _xlsx_bytes()
    settings = Settings(blob_dir=str(tmp_path / "blobs"))
    fake_block = {
        "ordinal": 0,
        "block_type": "XLSX_SHEET",
        "raw_content": "字段中文名 | 字段名 | 字段校验\n机构编码 | BRANCH_CODE | 必填",
        "source_locator": {"sheet": "字段字典"},
        "content_json": {"rows": [["字段中文名", "字段名", "字段校验"], ["机构编码", "BRANCH_CODE", "必填"]]},
        "raw_hash": "hash0",
    }

    try:
        def _upload() -> str:
            doc, _ = ingest_uploaded_file_with_flag(
                "same.xlsx",
                content,
                prepare=False,
                session_factory=Session,
                get_settings_fn=lambda: settings,
                extract_document_fn=lambda _p, _t: ("TEST", [fake_block]),
            )
            return doc.id

        doc_ids, upload_errors = _run_concurrent([_upload for _ in range(10)])
        assert [e for e in upload_errors if e is not None] == []
        assert len(set(doc_ids)) == 1
        doc_id = doc_ids[0]

        with Session() as seed:
            blk = RawBlockModel(
                id=str(uuid.uuid4()),
                document_id=doc_id,
                ordinal=0,
                block_type="XLSX_SHEET",
                raw_content=fake_block["raw_content"],
                source_locator=fake_block["source_locator"],
                content_json=fake_block["content_json"],
                raw_hash="hash0",
            )
            doc = seed.get(SourceDocumentModel, doc_id)
            doc.status = "UPLOADED"
            doc.block_count = 1
            seed.add(blk)
            seed.commit()
            block_id = blk.id
            assert seed.query(SourceDocumentModel).count() == 1

        def _claim_extract():
            with Session() as s:
                res = prepare_parallel_extraction(s, doc_id, [block_id])
                s.commit()
                return res

        results, extract_errors = _run_concurrent([_claim_extract for _ in range(6)])
        ok_count = sum(1 for r in results if r is not None)
        conflict_count = sum(1 for e in extract_errors if isinstance(e, HTTPException) and e.status_code == 409)
        unexpected = [e for e in extract_errors if e is not None and not (isinstance(e, HTTPException) and e.status_code == 409)]
        assert unexpected == []
        assert ok_count == 1
        assert conflict_count == 5

        # 重置回可抽取状态后，让 3 个抽取线程与 3 个删除线程同时竞争原子状态 CAS
        with Session() as reset:
            doc = reset.get(SourceDocumentModel, doc_id)
            doc.status = "UPLOADED"
            reset.commit()

        def _soft_delete():
            with Session() as s:
                return soft_delete_document(s, doc_id)

        mix_tasks: List[Callable[[], Any]] = [_claim_extract for _ in range(3)] + [_soft_delete for _ in range(3)]
        mix_res, mix_err = _run_concurrent(mix_tasks)
        extract_wins = sum(1 for r in mix_res[:3] if r is not None)
        assert extract_wins <= 1
        # 不能出现既成功启动抽取又成功删档的双赢竞态
        with Session() as check:
            final_doc = check.get(SourceDocumentModel, doc_id)
            assert final_doc.status in {"EXTRACTING", "DELETED"}
            if final_doc.status == "DELETED":
                assert extract_wins == 0
            unexpected_mix = [
                e for e in mix_err if e is not None and not (isinstance(e, HTTPException) and e.status_code in {404, 409})
            ]
            assert unexpected_mix == []
    finally:
        engine.dispose()


def test_stress_concurrent_metadata_extraction_and_code_set_auto_linking(monkeypatch, tmp_path):
    """6 线程并发抽取同一 RawBlock + 8 线程并发全量码表 auto_link：零 Unique 冲突且结果完全幂等。"""
    engine, Session = _make_stress_db(tmp_path, "meta_codeset_race")
    settings = Settings(blob_dir=str(tmp_path / "blobs"), llm_api_key="test-key")

    class StubMetaLlm:
        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            if tool_name == "assign_metadata":
                time.sleep(0.01)
                return {
                    "table_code": "T_CUST",
                    "table_name": "客户信息表",
                    "table_definition": "客户基础信息",
                    "fields": [
                        {
                            "field_cn": "常驻国家代码",
                            "field_en": "COUNTRY_CODE",
                            "field_definition": "常驻国家代码",
                            "check_text_raw": "必填项，见国家和地区代码表",
                            "data_type": "VARCHAR",
                            "length": 3,
                            "decimal_places": 0,
                            "required": True,
                            "code_reference_text": "见国家和地区代码表",
                            "confidence": 0.96,
                            "evidence_json": {"row": 1},
                        },
                        {
                            "field_cn": "账户状态",
                            "field_en": "ACCT_STATUS",
                            "field_definition": "状态",
                            "check_text_raw": "必填，1-正常，2-冻结",
                            "data_type": "VARCHAR",
                            "length": 1,
                            "decimal_places": 0,
                            "required": True,
                            "code_reference_text": "1-正常，2-冻结",
                            "confidence": 0.95,
                            "evidence_json": {"row": 2},
                        },
                    ],
                }, {"provider": "STUB", "model": "stub"}
            if tool_name == "review_metadata":
                return {"issues": []}, {"provider": "STUB", "model": "stub"}
            raise AssertionError(f"Unexpected tool: {tool_name}")

    monkeypatch.setattr(table_service, "SessionLocal", Session)
    monkeypatch.setattr("app.catalog.tables.service.RealDesignLlm", lambda _s: StubMetaLlm())
    monkeypatch.setattr("app.catalog.code_sets.service.SessionLocal", Session)

    try:
        with Session() as seed:
            doc = SourceDocumentModel(
                id=str(uuid.uuid4()),
                file_name="meta.xlsx",
                file_type="xlsx",
                sha256=uuid.uuid4().hex,
                blob_uri="blob://meta",
                spec_version="1.3",
                status="UPLOADED",
                block_count=1,
            )
            block = RawBlockModel(
                id=str(uuid.uuid4()),
                document_id=doc.id,
                ordinal=0,
                block_type="XLSX_SHEET",
                raw_content="常驻国家代码 | COUNTRY_CODE | 必填项，见国家和地区代码表",
                source_locator={"sheet": "T_CUST"},
                content_json={
                    "headers": ["字段中文名", "字段名", "字段校验"],
                    "rows": [
                        ["字段中文名", "字段名", "字段校验"],
                        ["常驻国家代码", "COUNTRY_CODE", "必填项，见国家和地区代码表"],
                        ["账户状态", "ACCT_STATUS", "必填，1-正常，2-冻结"],
                    ],
                },
                raw_hash="blk0",
            )
            cs = CodeSetModel(
                id=str(uuid.uuid4()),
                code="CS_COUNTRY",
                name="国家和地区代码表",
                definition="",
                source_type="DOCUMENT_TABLE",
                status="PUBLISHED",
                current_version=1,
                published_version=1,
            )
            cv = CodeValueModel(
                id=str(uuid.uuid4()),
                code_set_id=cs.id,
                value="CHN",
                label_cn="中国",
                sort_order=0,
                enabled=True,
                source_locator={},
            )
            seed.add_all([doc, block, cs, cv])
            seed.commit()
            doc_id = doc.id
            block_id = block.id

        results, ext_errors = _run_concurrent(
            [lambda: table_service.extract_metadata_for_block(block_id, settings, auto_link=True) for _ in range(6)]
        )
        assert [e for e in ext_errors if e is not None] == []
        assert len(set(results)) == 1

        # 再挂 3 张共享同一内联枚举/码表引用的新表，8 线程并发 auto_link_all_references
        with Session() as extra:
            for idx in range(3):
                b = RawBlockModel(
                    id=str(uuid.uuid4()),
                    document_id=doc_id,
                    ordinal=idx + 1,
                    block_type="XLSX_SHEET",
                    raw_content="共享状态",
                    source_locator={"sheet": f"T_EXTRA_{idx}"},
                    content_json={"rows": []},
                    raw_hash=f"blk_{idx + 1}",
                )
                t = TableMetadataModel(
                    id=str(uuid.uuid4()),
                    document_id=doc_id,
                    block_id=b.id,
                    table_code=f"T_EXTRA_{idx}",
                    table_name=f"附表{idx}",
                    table_definition="测试",
                    status="PUBLISHED",
                    current_version=1,
                    published_version=1,
                )
                f = FieldMetadataModel(
                    id=str(uuid.uuid4()),
                    table_id=t.id,
                    field_cn="共享枚举",
                    field_en="SHARED_STATE",
                    field_definition="状态",
                    check_text_raw="1-有效，2-注销",
                    data_type="VARCHAR",
                    length=1,
                    decimal_places=0,
                    required=True,
                    code_reference_text="1-有效，2-注销",
                    confidence=0.9,
                    evidence_json={},
                    sort_order=0,
                    ai_value_json={},
                )
                extra.add_all([b, t, f])
            extra.commit()

        def _link_once() -> int:
            with Session() as s:
                count = auto_link_all_references(s)
                s.commit()
                return count

        _, link_errors = _run_concurrent([_link_once for _ in range(8)])
        assert [e for e in link_errors if e is not None] == []

        with Session() as verify:
            # auto_link_all_references 返回 RESOLVED 引用总数；8 线程并发重跑后应保持精确 5 条、零重复引用
            assert auto_link_all_references(verify) == 5
            assert auto_link_all_references(verify) == 5
            code_sets = verify.query(CodeSetModel).all()
            assert len({(c.code, c.current_version) for c in code_sets}) == len(code_sets)
            refs = verify.query(FieldCodeReferenceModel).all()
            assert len(refs) == 5
            assert len({r.field_id for r in refs}) == len(refs)
    finally:
        engine.dispose()


def test_stress_optimistic_locking_atomic_cas(tmp_path):
    """8 线程并发带相同 expected_version 更新/新增元数据与码表：仅 1 个成功，7 个 409/VERSION_CONFLICT。"""
    engine, Session = _make_stress_db(tmp_path, "cas_race")
    try:
        with Session() as seed:
            doc = SourceDocumentModel(
                id=str(uuid.uuid4()),
                file_name="cas.xlsx",
                file_type="xlsx",
                sha256=uuid.uuid4().hex,
                blob_uri="blob://cas",
                spec_version="1.3",
                status="UPLOADED",
                block_count=2,
            )
            b1 = RawBlockModel(
                id=str(uuid.uuid4()), document_id=doc.id, ordinal=0, block_type="XLSX_SHEET",
                raw_content="t1", source_locator={}, content_json={}, raw_hash="h1",
            )
            b2 = RawBlockModel(
                id=str(uuid.uuid4()), document_id=doc.id, ordinal=1, block_type="XLSX_SHEET",
                raw_content="t2", source_locator={}, content_json={}, raw_hash="h2",
            )
            t1 = TableMetadataModel(
                id=str(uuid.uuid4()), document_id=doc.id, block_id=b1.id, table_code="T_CAS_1",
                table_name="表1", table_definition="", status="PUBLISHED", current_version=1, published_version=1,
            )
            t2 = TableMetadataModel(
                id=str(uuid.uuid4()), document_id=doc.id, block_id=b2.id, table_code="T_CAS_2",
                table_name="表2", table_definition="", status="PUBLISHED", current_version=1, published_version=1,
            )
            f1 = FieldMetadataModel(
                id=str(uuid.uuid4()), table_id=t1.id, field_cn="原名称", field_en="F_CAS",
                field_definition="", check_text_raw="必填", data_type="VARCHAR", length=32,
                decimal_places=0, required=True, code_reference_text="", confidence=0.9,
                evidence_json={}, sort_order=0, ai_value_json={},
            )
            cs = CodeSetModel(
                id=str(uuid.uuid4()), code="CS_CAS", name="CAS码表", definition="",
                source_type="CUSTOM", status="DRAFT", current_version=1,
            )
            cv = CodeValueModel(
                id=str(uuid.uuid4()), code_set_id=cs.id, value="01", label_cn="初始值",
                sort_order=0, enabled=True, source_locator={},
            )
            seed.add_all([doc, b1, b2, t1, t2, f1, cs, cv])
            seed.commit()
            t1_id, t2_id, f1_id, cs_id, cv_id = t1.id, t2.id, f1.id, cs.id, cv.id

        # 1) update_field 8 并发
        def _edit_meta(idx: int):
            with Session() as s:
                t = s.get(TableMetadataModel, t1_id)
                f = s.get(FieldMetadataModel, f1_id)
                update_field(s, t, f, {"field_cn": f"并发名_{idx}"}, 1, f"u_{idx}", "cas")
                s.commit()
                return t.current_version

        res_u, err_u = _run_concurrent([lambda idx=i: _edit_meta(idx) for i in range(8)])
        assert sum(1 for r in res_u if r == 2) == 1
        assert sum(1 for e in err_u if isinstance(e, ValueError) and "VERSION_CONFLICT" in str(e)) == 7

        # 2) create_table_field 8 并发
        def _add_meta_field(idx: int):
            with Session() as s:
                res = create_table_field(
                    s,
                    t2_id,
                    expected_version=1,
                    field_payload={"field_cn": f"新增_{idx}", "field_en": f"NEW_{idx}", "data_type": "VARCHAR", "length": 16},
                    editor=f"u_{idx}",
                    reason="cas add",
                )
                s.commit()
                return res

        res_c, err_c = _run_concurrent([lambda idx=i: _add_meta_field(idx) for i in range(8)])
        assert sum(1 for r in res_c if r is not None) == 1
        assert sum(1 for e in err_c if isinstance(e, HTTPException) and e.status_code == 409) == 7

        # 3) edit_code_value_record 8 并发 (v1 -> v2)
        def _edit_cv(idx: int):
            with Session() as s:
                return edit_code_value_record(s, cs_id, cv_id, expected_version=1, changes={"label_cn": f"并发码值_{idx}"})

        res_ecv, err_ecv = _run_concurrent([lambda idx=i: _edit_cv(idx) for i in range(8)])
        assert sum(1 for r in res_ecv if r is not None) == 1
        assert sum(1 for e in err_ecv if isinstance(e, HTTPException) and e.status_code == 409) == 7

        # 4) add_code_value_record 8 并发 (v2 -> v3)
        def _add_cv(idx: int):
            with Session() as s:
                return add_code_value_record(
                    s, cs_id, expected_version=2, editor=f"u_{idx}",
                    value=f"V{idx}", label_cn=f"新增_{idx}", label_en="", definition="",
                )

        res_acv, err_acv = _run_concurrent([lambda idx=i: _add_cv(idx) for i in range(8)])
        assert sum(1 for r in res_acv if r is not None) == 1
        assert sum(1 for e in err_acv if isinstance(e, HTTPException) and e.status_code == 409) == 7

        # 5) publish_code_set_record 8 并发 (v3 -> v3 published)
        def _pub_cs():
            with Session() as s:
                return publish_code_set_record(s, cs_id, expected_version=3)

        _, err_pub = _run_concurrent([_pub_cs for _ in range(8)])
        assert [e for e in err_pub if e is not None and not (isinstance(e, HTTPException) and e.status_code == 409)] == []

        with Session() as verify:
            assert verify.get(TableMetadataModel, t1_id).current_version == 2
            assert verify.get(TableMetadataModel, t2_id).current_version == 2
            assert verify.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == t2_id).count() == 1
            assert verify.get(CodeSetModel, cs_id).current_version == 3
            assert verify.query(CodeValueModel).filter(CodeValueModel.code_set_id == cs_id).count() == 2
    finally:
        engine.dispose()


def test_stress_double_click_approval_reaudit_retry_and_delete(tmp_path):
    """审批、测试确认、重审、断点重跑、删除并发双击竞态：严格单次落账、零 SAWarning。"""
    engine, Session = _make_stress_db(tmp_path, "lifecycle_race")
    settings = Settings(blob_dir=str(tmp_path / "blobs"), oracle_sandbox_url="")
    try:
        with Session() as seed:
            spec_m1, run1, rule1 = _seed_spec_and_run(seed, approval_gate=True)
            run1_id, rule1_hash = run1.id, rule1.rule_hash

            _, run2, _ = _seed_spec_and_run(seed, approval_gate=False, rule_status="APPROVED")
            append_event(seed, run2, "CROSS_RULES_ASSIGNED", "rule_binder", {"count": 1})
            append_event(
                seed,
                run2,
                "AUDIT_COMPLETED",
                "oracle_executor",
                {
                    "node": "oracle_executor",
                    "result": {
                        "audit_report_blob": "blob://stress-report",
                        "audit_summary": {"all_passed": True, "coverage_complete": True},
                    },
                },
            )
            append_event(
                seed,
                run2,
                "TEST_CONFIRMATION_REQUIRED",
                "test_confirmation",
                interrupt_payload("TEST_CONFIRMATION_REQUIRED", "请确认测试结果", {"passed": True}),
            )
            seed.commit()
            run2_id = run2.id

            _, run3, _ = _seed_spec_and_run(seed, approval_gate=False)
            append_event(
                seed, run3, "RUN_FAILED", "data_generator_binder",
                {"error": "boom", "node": "data_generator_binder", "resume_from_node": "oracle_asset_builder", "resume_when": "no_pending_rules"},
            )
            seed.commit()
            run3_id = run3.id

            _, run4, _ = _seed_spec_and_run(seed, approval_gate=False)
            append_event(seed, run4, "RUN_FAILED", "schema_mapper", {"error": "boom"})
            seed.commit()
            run4_id = run4.id

        # 1) 8 并发 approve_generic_rules
        def _approve(idx: int):
            with Session() as s:
                r = s.get(RunModel, run1_id)
                return approve_generic_rules(
                    s, r, f"approver_{idx}", {rule1_hash: "APPROVED"}, settings, defer_continuation=True,
                )

        _, err_app = _run_concurrent([lambda idx=i: _approve(idx) for i in range(8)])
        assert [e for e in err_app if e is not None] == []

        # 2) 8 并发 confirm_generic_test_results（同人双击重放幂等）
        def _confirm():
            with Session() as s:
                r = s.get(RunModel, run2_id)
                return confirm_generic_test_results(s, r, "测试审核员")

        _, err_conf = _run_concurrent([_confirm for _ in range(8)])
        assert [e for e in err_conf if e is not None] == []

        # 3) 6 并发 reaudit_generic_run
        def _reaudit():
            with Session() as s:
                r = s.get(RunModel, run2_id)
                return reaudit_generic_run(s, r, settings, defer_continuation=True)

        res_re, err_re = _run_concurrent([_reaudit for _ in range(6)])
        assert sum(1 for r in res_re if r is not None) == 1
        assert sum(1 for e in err_re if isinstance(e, WorkflowError)) == 5

        # 4) 6 并发 retry_generic_run
        def _retry():
            with Session() as s:
                r = s.get(RunModel, run3_id)
                return retry_generic_run(s, r, settings, defer_continuation=True)

        res_rt, err_rt = _run_concurrent([_retry for _ in range(6)])
        assert sum(1 for r in res_rt if r is not None) == 1
        assert sum(1 for e in err_rt if isinstance(e, WorkflowError)) == 5

        # 5) 6 并发 delete_generic_run：模拟 6 个请求同时越过存在性检查后再竞态删除（绝不触发 SAWarning）
        delete_gate = threading.Barrier(6)

        def _delete():
            with Session() as s:
                r = s.get(RunModel, run4_id)
                assert r is not None
                delete_gate.wait(timeout=10)
                return delete_generic_run(s, r, settings)

        res_del, err_del = _run_concurrent([_delete for _ in range(6)], barrier=False)
        assert sum(1 for r in res_del if isinstance(r, dict) and r.get("deleted_id") == run4_id) == 1
        assert sum(1 for e in err_del if isinstance(e, WorkflowError)) == 5

        with Session() as verify:
            for rid, expected_kind, expected_status in (
                (run1_id, "HUMAN_DECISIONS_RECORDED", "READY_TO_AUDIT"),
                (run2_id, "REAUDIT_REQUESTED", "READY_TO_AUDIT"),
                (run3_id, "RUN_RESUMED", "RUNNING"),
            ):
                run_obj = verify.get(RunModel, rid)
                assert project(verify, run_obj).status == expected_status
                kinds = [ev.kind for ev in verify.query(EventModel).filter(EventModel.run_id == rid).all()]
                assert kinds.count(expected_kind) == 1
                assert verify_chain(verify, run_obj) is True

            assert verify.get(RunModel, run4_id) is None
            assert verify.query(EventModel).filter(EventModel.run_id == run4_id).count() == 0
    finally:
        engine.dispose()


def test_stress_150_tables_scale_queries_and_bus_20k_event_storm():
    """150 张表 × 1500 字段零 N+1 查询膨胀；20,000 事件高压风暴 + 慢消费者/频繁摘挂零泄漏。"""
    session = make_session()
    doc = SourceDocumentModel(
        id=str(uuid.uuid4()),
        file_name="scale_150.pdf",
        file_type="pdf",
        sha256="c" * 64,
        blob_uri="blob://scale150",
        spec_version="1.3",
        status="EXTRACTED",
        block_count=150,
    )
    session.add(doc)
    session.flush()

    spec_tables = []
    blocks = []
    tables = []
    fields = []
    for idx in range(150):
        code = f"SAFE_SCALE_{idx:03d}"
        blk_id = str(uuid.uuid4())
        tbl_id = str(uuid.uuid4())
        blocks.append(
            RawBlockModel(
                id=blk_id, document_id=doc.id, ordinal=idx, block_type="PDF_SECTION",
                raw_content=f"table {code}", source_locator={"section": f"6.{idx}"},
                content_json={}, raw_hash=f"raw_{idx}",
            )
        )
        tables.append(
            TableMetadataModel(
                id=tbl_id, document_id=doc.id, block_id=blk_id, table_code=code,
                table_name=f"规模表_{idx}", table_definition="", status="PUBLISHED",
                current_version=1, published_version=1, source_ordinal=idx,
            )
        )
        spec_fields = []
        for f_idx in range(10):
            fname = f"COL_{f_idx}"
            fields.append(
                FieldMetadataModel(
                    id=str(uuid.uuid4()), table_id=tbl_id, field_cn=f"字段{f_idx}", field_en=fname,
                    field_definition="", check_text_raw="必填", data_type="VARCHAR", length=32,
                    decimal_places=0, required=True, code_reference_text="", confidence=0.9,
                    evidence_json={}, sort_order=f_idx, ai_value_json={},
                )
            )
            spec_fields.append(FieldSpec(name=fname, content=f"字段{f_idx}", evidence_page="1").model_dump())
        spec_tables.append(
            SpecDocument(
                version="1.3", table_code=code, table_name=f"规模表_{idx}", section=f"6.{idx}",
                source_name="scale_150.pdf", source_sha256="c" * 64,
                fields=[FieldSpec(**item) for item in spec_fields], terms_text="", dictionary_text="",
            ).model_dump()
        )

    spec_model = SpecificationModel(
        id=str(uuid.uuid4()), version="METADATA_PUBLISHED", table_code="SCALE_SNAPSHOT",
        source_name="scale_150.pdf", source_sha256="c" * 64, content_json={"tables": spec_tables},
    )
    session.add_all(blocks + tables + fields + [spec_model])
    session.flush()
    run = kernel_create_run(session, spec_model.id)
    append_event(session, run, "APPROVAL_REQUIRED", "human_gate", {"pending_count": 150})
    rules = []
    for idx in range(150):
        code = f"SAFE_SCALE_{idx:03d}"
        defn = RuleDefinition(
            rule_key=f"{code}_REQ", table_code=code, field="COL_0", operator="REQUIRED",
            params={}, source="DOC_EXPLICIT",
            evidence=Evidence(section=f"6.{idx}", page="1", field="COL_0", quote="必填"),
            rationale="必填",
        )
        defn.rule_hash = calculate_rule_hash(defn, "c" * 64)
        rules.append(
            RuleModel(
                row_id=str(uuid.uuid4()), run_id=run.id, spec_id=spec_model.id, table_code=code,
                rule_key=defn.rule_key, rule_hash=defn.rule_hash, source=defn.source,
                status="PENDING", definition_json=defn.model_dump(),
            )
        )
    session.add_all(rules)
    session.commit()

    sql_counts = {"artifacts": 0, "rules": 0, "tables": 0}
    bucket = {"name": None}

    @event.listens_for(session.bind, "before_cursor_execute")
    def _count_sql(_conn, _cursor, _stmt, _params, _ctx, _many):
        if bucket["name"]:
            sql_counts[bucket["name"]] += 1

    try:
        bucket["name"] = "artifacts"
        art_res = list_artifacts_status(session, document_id=doc.id)
        bucket["name"] = "rules"
        rule_res = list_all_table_rules(session, document_id=doc.id)
        bucket["name"] = "tables"
        tbl_res = metadata_api.list_tables(status="PUBLISHED", document_id=doc.id, session=session)
    finally:
        bucket["name"] = None
        event.remove(session.bind, "before_cursor_execute", _count_sql)

    assert len(art_res["tables"]) == 150
    assert len(rule_res["tables"]) == 150
    assert len(tbl_res) == 150
    assert sql_counts["artifacts"] <= 10
    assert sql_counts["rules"] <= 10
    assert sql_counts["tables"] <= 10

    # Part B: Bus 20,000 事件高压风暴 + 慢队列 drop + 订阅频繁开关零残留
    bus = Bus()
    with bus.stream(topic="*", maxsize=8, overflow="drop") as slow_sub:
        def _producer(start: int) -> None:
            for offset in range(10000):
                bus.fire("storm.tick", {"seq": start + offset})

        def _churn() -> None:
            for _ in range(100):
                with bus.stream(topic="*", maxsize=32, overflow="drop"):
                    pass

        _, storm_errors = _run_concurrent(
            [lambda: _producer(0), lambda: _producer(10000), _churn, _churn]
        )
        assert [e for e in storm_errors if e is not None] == []
        assert slow_sub.dropped > 0

    assert len(bus._subscribers.get("*", [])) == 0
    assert len(bus._history) == MAX_BUS_HISTORY


def test_chaos_llm_faults_cancellation_and_budget_breach():
    """混沌故障注入：超时/断连/无工具调用/幻觉字段自动修复；取消立即唤醒；并发 BudgetGuard 精准熔断。"""
    # 1) 连续混合故障注入 + default_bus 门禁校验修复
    valid_fields = {"CODE", "AMOUNT"}

    class ChaosLlm:
        def __init__(self) -> None:
            self.calls = 0
            self.feedbacks: List[Any] = []

        def assign(self, tool_name, instruction, payload, feedback=None, timeout=None):
            self.calls += 1
            self.feedbacks.append(feedback)
            if self.calls == 1:
                raise LlmTimeoutError("chaos read timeout")
            if self.calls == 2:
                raise httpx.ConnectError("chaos connection reset")
            if self.calls == 3:
                raise RuntimeError("Model returned no tool call: text only")
            if self.calls == 4:
                return {"rules": [{"field": "HALLUCINATED_COL", "operator": "REQUIRED"}]}, {"provider": "chaos", "model": "chaos"}
            return {"rules": [{"field": "CODE", "operator": "REQUIRED"}]}, {
                "provider": "chaos",
                "model": "chaos",
                "usage": {"total_tokens": 100},
            }

    def _validate(args: Dict[str, Any]) -> Dict[str, Any]:
        for rule in args.get("rules", []):
            if rule.get("field") not in valid_fields:
                raise RuleValidationError(f"unknown field {rule.get('field')}")
        return args

    chaos_llm = ChaosLlm()
    bus = default_bus()
    seen_topics: List[str] = []
    bus.subscribe("*", lambda ev: seen_topics.append(ev["topic"]))

    out, trace = call_llm_validated(
        chaos_llm,
        "assign_rules",
        "extract rules",
        {"table_code": "T_CHAOS", "field_names": ["CODE", "AMOUNT"], "fields": [{"name": "CODE"}]},
        validator=_validate,
        timeout=5,
        backoff=(0, 0, 0),
        bus=bus,
        transient_attempts=5,
        validation_attempts=3,
    )
    assert out["rules"][0]["field"] == "CODE"
    assert chaos_llm.calls == 5
    assert trace["attempts"] >= 2
    assert chaos_llm.feedbacks[-1] and "HALLUCINATED_COL" in chaos_llm.feedbacks[-1]
    assert "tool.rejected" in seen_topics
    assert "tool.after" in seen_topics

    # 2) CancellationToken 在长退避期间必须毫秒级唤醒而非傻等 30s
    token = CancellationToken()

    class AlwaysTimeoutLlm:
        def assign(self, *_args, **_kwargs):
            raise LlmTimeoutError("always timeout")

    timer = threading.Timer(0.1, lambda: token.cancel("operator cancelled chaos"))
    timer.start()
    started = time.monotonic()
    try:
        with llm_execution_scope(cancellation=token):
            with pytest.raises(RunCancelledError):
                call_llm_validated(
                    AlwaysTimeoutLlm(),
                    "assign_rules",
                    "instr",
                    {},
                    backoff=(30.0, 30.0),
                )
    finally:
        timer.cancel()
    assert time.monotonic() - started < 1.5

    # 3) 多线程共享 BudgetGuard 并发压测：严格截断并触发 budget.exceeded
    budget_bus = default_bus()
    exceeded_events: List[Dict[str, Any]] = []
    budget_bus.subscribe("budget.exceeded", lambda ev: exceeded_events.append(ev))
    guard = BudgetGuard(BudgetLimits(max_llm_calls=3, max_total_tokens=1000), run_id="chaos-budget").attach(budget_bus)

    class MeteredLlm:
        def assign(self, *_args, **_kwargs):
            time.sleep(0.02)
            return {"rules": []}, {
                "provider": "metered",
                "model": "metered",
                "elapsed_seconds": 0.02,
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            }

    metered = MeteredLlm()
    ok_calls = 0
    tripped = 0
    with llm_execution_scope(bus=budget_bus):
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = [
                submit_with_context(pool, call_llm_validated, metered, "assign_rules", "instr", {})
                for _ in range(8)
            ]
            for fut in futs:
                try:
                    fut.result(timeout=10)
                    ok_calls += 1
                except BudgetExceededError:
                    tripped += 1

    assert ok_calls == 3
    assert tripped == 5
    assert guard.snapshot()["llm_calls"] == 3
    assert len(exceeded_events) >= 1
