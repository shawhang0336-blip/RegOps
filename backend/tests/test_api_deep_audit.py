"""Layer 7（backend/app/api/* + main.py）生产级深审回归测试。"""

import asyncio
import csv
import io
import uuid
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from openpyxl import Workbook
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app.api import runs as runs_api
from app.api import system as system_api
from app.api.deps import RunRequest
from app.api.v2 import artifacts as artifacts_api
from app.api.v2 import code_sets as code_sets_api
from app.api.v2 import documents as documents_api
from app.api.v2 import generation as generation_api
from app.api.v2 import metadata as metadata_api
from app.api.v2 import settings as settings_api
from app.infra.blob_store import LocalBlobStore
from app.infra.config import Settings
from app.infra.contracts import Evidence, FieldSpec, RuleDefinition, SpecDocument, calculate_rule_hash
from app.infra.db import Base
from app.infra.db_tables import (
    CodeSetModel,
    CodeValueModel,
    EventModel,
    FieldCodeReferenceModel,
    FieldMetadataModel,
    MetadataRevisionModel,
    RawBlockModel,
    RuleModel,
    RunModel,
    SourceDocumentModel,
    SpecificationModel,
    TableMetadataModel,
)
from app.kernel.event_log import append_event, verify_chain
from app.kernel.run import create_run as kernel_create_run

from .helpers import make_session


def _make_strict_fk_session():
    """启用 PRAGMA foreign_keys=ON 的内存 SQLite，严格模拟 MySQL 8 外键约束。"""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _set_fk(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed_doc_block_table(session, *, table_code="T_DEMO", status="DRAFT", version=1, ordinal=1):
    doc = SourceDocumentModel(
        id=str(uuid.uuid4()),
        file_name="spec.xlsx",
        file_type="xlsx",
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        blob_uri="blob://uploads/spec.xlsx",
        spec_version="1.3",
        status="EXTRACTED",
        block_count=1,
    )
    block = RawBlockModel(
        id=str(uuid.uuid4()),
        document_id=doc.id,
        ordinal=ordinal,
        block_type="XLSX_SHEET",
        raw_content="字段名 | 校验",
        source_locator={"sheet": table_code},
        content_json={"rows": []},
        raw_hash=uuid.uuid4().hex,
    )
    table = TableMetadataModel(
        id=str(uuid.uuid4()),
        document_id=doc.id,
        block_id=block.id,
        table_code=table_code,
        table_name=f"测试表_{table_code}",
        table_definition="定义",
        status=status,
        current_version=version,
        published_version=version if status == "PUBLISHED" else None,
        source_ordinal=ordinal,
    )
    session.add(doc)
    session.flush()
    session.add(block)
    session.flush()
    session.add(table)
    session.flush()
    return doc, block, table


def _make_xlsx_bytes(rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_delete_field_under_strict_foreign_keys_cleans_refs_and_revisions():
    """Fix 1: MySQL 8 强外键下删除字段（含码表引用 + 历史修订）不再触发 1451/1452 崩溃。"""
    session = _make_strict_fk_session()
    _, _, table = _seed_doc_block_table(session)
    code_set = CodeSetModel(
        id=str(uuid.uuid4()),
        code="CCY_DEL",
        name="币种",
        definition="",
        source_type="MANUAL",
        status="PUBLISHED",
        current_version=1,
        published_version=1,
    )
    field = FieldMetadataModel(
        id=str(uuid.uuid4()),
        table_id=table.id,
        field_cn="币种代码",
        field_en="TXCCY",
        field_definition="",
        check_text_raw="必填",
        type_basis="SOURCE",
        data_type="VARCHAR",
        length=3,
        decimal_places=0,
        required=True,
        code_reference_text="币种代码表",
        confidence=1.0,
        evidence_json={},
        sort_order=0,
        ai_value_json={},
    )
    session.add_all([code_set, field])
    session.flush()
    ref = FieldCodeReferenceModel(
        id=str(uuid.uuid4()),
        field_id=field.id,
        code_set_id=code_set.id,
        reference_text="币种代码表",
        status="RESOLVED",
        condition_json={},
        confidence=1.0,
    )
    prior_rev = MetadataRevisionModel(
        id=str(uuid.uuid4()),
        table_id=table.id,
        field_id=field.id,
        revision_kind="HUMAN_EDIT",
        old_value_json={"length": 2},
        new_value_json={"length": 3},
        editor="alice",
        reason="fix len",
    )
    session.add_all([ref, prior_rev])
    session.commit()

    res = metadata_api.delete_field(
        table.id,
        field.id,
        metadata_api.EditRequest(expected_version=1, editor="bob", reason="冗余字段", changes={}),
        session=session,
    )
    assert res["fields"] == []
    assert res["current_version"] == 2
    assert session.query(FieldCodeReferenceModel).filter_by(field_id=field.id).count() == 0
    revisions = session.query(MetadataRevisionModel).filter_by(table_id=table.id).all()
    assert len(revisions) == 2
    assert all(rev.field_id is None for rev in revisions)
    delete_rev = next(r for r in revisions if r.revision_kind == "HUMAN_DELETE")
    assert delete_rev.old_value_json["field_id"] == field.id


def test_add_field_validation_duplicate_and_manual_type_basis():
    """Fix 6a: add_field 捕获校验错(422)/重名(409)/MERGED(409)，且标记 type_basis=MANUAL。"""
    session = make_session()
    _, _, table = _seed_doc_block_table(session)
    session.commit()

    with pytest.raises(HTTPException) as exc_bad:
        metadata_api.add_field(
            table.id,
            metadata_api.FieldCreateRequest(expected_version=1, field={"field_cn": "", "field_en": ""}),
            session=session,
        )
    assert exc_bad.value.status_code == 422

    detail = metadata_api.add_field(
        table.id,
        metadata_api.FieldCreateRequest(
            expected_version=1,
            field={"field_cn": "金额", "field_en": "TXAMT", "data_type": "NUMBER", "length": 18, "decimal_places": 2},
        ),
        session=session,
    )
    assert len(detail["fields"]) == 1
    assert detail["fields"][0]["type_basis"] == "MANUAL"

    with pytest.raises(HTTPException) as exc_dup:
        metadata_api.add_field(
            table.id,
            metadata_api.FieldCreateRequest(
                expected_version=2,
                field={"field_cn": "重复金额", "field_en": "txamt", "data_type": "NUMBER", "length": 18},
            ),
            session=session,
        )
    assert exc_dup.value.status_code == 409

    table.status = "MERGED"
    session.commit()
    with pytest.raises(HTTPException) as exc_merged:
        metadata_api.add_field(
            table.id,
            metadata_api.FieldCreateRequest(
                expected_version=2,
                field={"field_cn": "字段2", "field_en": "F2", "data_type": "VARCHAR", "length": 10},
            ),
            session=session,
        )
    assert exc_merged.value.status_code == 409


def test_create_draft_optimistic_lock_clones_metadata_and_blocks_duplicate():
    """Fix 6b: create_draft 强制乐观锁、保留 source_ordinal/type_basis/码表引用、防重复草稿 500。"""
    session = make_session()
    _, _, table = _seed_doc_block_table(session, status="PUBLISHED", version=1, ordinal=7)
    code_set = CodeSetModel(
        id=str(uuid.uuid4()),
        code="CS_CLONE",
        name="克隆测试",
        definition="",
        source_type="MANUAL",
        status="PUBLISHED",
        current_version=1,
        published_version=1,
    )
    field = FieldMetadataModel(
        id=str(uuid.uuid4()),
        table_id=table.id,
        field_cn="币种",
        field_en="CCY",
        field_definition="",
        check_text_raw="",
        type_basis="SOURCE",
        data_type="VARCHAR",
        length=3,
        decimal_places=0,
        required=True,
        code_reference_text="CS_CLONE",
        confidence=1.0,
        evidence_json={},
        sort_order=0,
        ai_value_json={},
    )
    session.add_all([code_set, field])
    session.flush()
    session.add(
        FieldCodeReferenceModel(
            id=str(uuid.uuid4()),
            field_id=field.id,
            code_set_id=code_set.id,
            reference_text="CS_CLONE",
            status="RESOLVED",
            condition_json={},
            confidence=0.95,
        )
    )
    session.commit()

    with pytest.raises(HTTPException) as exc_lock:
        metadata_api.create_draft(table.id, metadata_api.PublishRequest(expected_version=99), session=session)
    assert exc_lock.value.status_code == 409

    draft = metadata_api.create_draft(table.id, metadata_api.PublishRequest(expected_version=1), session=session)
    draft_row = session.get(TableMetadataModel, draft["id"])
    assert draft_row.source_ordinal == 7
    assert draft["fields"][0]["type_basis"] == "SOURCE"
    cloned_refs = session.query(FieldCodeReferenceModel).filter_by(field_id=draft["fields"][0]["id"]).all()
    assert len(cloned_refs) == 1
    assert cloned_refs[0].code_set_id == code_set.id

    with pytest.raises(HTTPException) as exc_dup:
        metadata_api.create_draft(table.id, metadata_api.PublishRequest(expected_version=1), session=session)
    assert exc_dup.value.status_code == 409


def test_metadata_template_csv_escaping_corrupt_xlsx_and_publish_batch_excludes_merged():
    """Fix 8: CSV 模板 RFC4180 转义 + 排除 MERGED；坏 xlsx 返回 422；批量发布跳过 MERGED。"""
    session = make_session()
    doc, block, active_table = _seed_doc_block_table(session, table_code="T_ACT", status="DRAFT", ordinal=1)
    merged_block = RawBlockModel(
        id=str(uuid.uuid4()),
        document_id=doc.id,
        ordinal=2,
        block_type="XLSX_SHEET",
        raw_content="",
        source_locator={},
        content_json={},
        raw_hash=uuid.uuid4().hex,
    )
    merged_table = TableMetadataModel(
        id=str(uuid.uuid4()),
        document_id=doc.id,
        block_id=merged_block.id,
        table_code="T_MERGED_FRAG",
        table_name="碎片表",
        table_definition="",
        status="MERGED",
        current_version=1,
        source_ordinal=2,
    )
    session.add_all([merged_block, merged_table])
    session.flush()
    session.add_all(
        [
            FieldMetadataModel(
                id=str(uuid.uuid4()),
                table_id=active_table.id,
                field_cn='含,逗号与"引号"\n换行名',
                field_en="F_SPECIAL",
                field_definition="",
                check_text_raw="",
                type_basis="SOURCE",
                data_type="VARCHAR",
                length=32,
                decimal_places=0,
                required=True,
                code_reference_text="",
                confidence=1.0,
                evidence_json={},
                sort_order=0,
                ai_value_json={},
            ),
            FieldMetadataModel(
                id=str(uuid.uuid4()),
                table_id=merged_table.id,
                field_cn="碎片字段",
                field_en="F_FRAG",
                field_definition="",
                check_text_raw="",
                type_basis="SOURCE",
                data_type="VARCHAR",
                length=16,
                decimal_places=0,
                required=True,
                code_reference_text="",
                confidence=1.0,
                evidence_json={},
                sort_order=0,
                ai_value_json={},
            ),
        ]
    )
    session.commit()

    resp = metadata_api.download_type_template(document_id=doc.id, session=session)
    rows = list(csv.reader(io.StringIO(resp.body.decode("utf-8-sig"))))
    assert len(rows) == 2
    assert rows[1][0] == "T_ACT"
    assert rows[1][2] == '含,逗号与"引号"\n换行名'

    with pytest.raises(HTTPException) as exc_bad:
        metadata_api._read_import_rows("broken.xlsx", b"not a zip archive")
    assert exc_bad.value.status_code == 422

    with pytest.raises(HTTPException) as exc_big:
        metadata_api._read_import_rows("big.csv", b"a" * (30 * 1024 * 1024 + 1))
    assert exc_big.value.status_code == 413

    batch = metadata_api.publish_batch(
        metadata_api.PublishBatchRequest(table_ids=[active_table.id, merged_table.id], scope="ddl_only", dry_run=True),
        session=session,
    )
    assert any(item["table_code"] == "T_MERGED_FRAG" and item["reason"] == "MERGED_TABLE" for item in batch["skipped_details"])


def test_code_sets_conflicts_and_import_ci_dedup_short_rows_empty_labels():
    """Fix 7: 码表重名 409、码值忽略大小写重复 409、导入短行/大小写冲突/空含义/坏 xlsx 安全处理。"""
    session = make_session()
    created = code_sets_api.create_code_set(
        code_sets_api.CodeSetRequest(code="ccy_test", name="币种测试"),
        session=session,
    )
    with pytest.raises(HTTPException) as exc_dup_cs:
        code_sets_api.create_code_set(
            code_sets_api.CodeSetRequest(code="CCY_TEST", name="重复币种"),
            session=session,
        )
    assert exc_dup_cs.value.status_code == 409

    cs_id = created["id"]
    code_sets_api.add_value(
        cs_id,
        code_sets_api.CodeValueRequest(expected_version=1, value="CNY", label_cn="人民币"),
        session=session,
    )
    with pytest.raises(HTTPException) as exc_ci:
        code_sets_api.add_value(
            cs_id,
            code_sets_api.CodeValueRequest(expected_version=2, value="cny", label_cn="小写人民币"),
            session=session,
        )
    assert exc_ci.value.status_code == 409

    # 坏 xlsx -> 422
    bad_upload = UploadFile(filename="bad.xlsx", file=io.BytesIO(b"corrupt"))
    with pytest.raises(HTTPException) as exc_bad_xlsx:
        asyncio.run(code_sets_api.import_values(cs_id, expected_version=2, file=bad_upload, session=session))
    assert exc_bad_xlsx.value.status_code == 422

    # 正常导入包含：大小写重复(cny)、空中文含义(JPY, "")、短行("ONLY_CODE",)、以及合法 USD
    wb_bytes = _make_xlsx_bytes(
        [
            ["码值", "中文含义", "英文名称"],
            ["cny", "小写冲突"],
            ["JPY", ""],
            ["ONLY_CODE"],
            ["USD", "美元", "US Dollar"],
        ]
    )
    good_upload = UploadFile(filename="codes.xlsx", file=io.BytesIO(wb_bytes))
    res = asyncio.run(code_sets_api.import_values(cs_id, expected_version=2, file=good_upload, session=session))
    assert res["imported"] == 1
    assert res["skipped_duplicate"] == 1
    assert res["skipped_empty_label"] == 2
    values = [item["value"] for item in res["code_set"]["values"]]
    assert values == ["CNY", "USD"]


def test_upload_deleted_document_passes_reimport_deleted_true_and_lifecycle_guards(monkeypatch, tmp_path):
    """Fix 2 & 11: 重传 DELETED 文档透传 reimport_deleted=True；删除/解析/调结构门禁齐全。"""
    session = make_session()
    content = _make_xlsx_bytes([["字段名", "校验"], ["A", "必填"]])
    digest = documents_api.hashlib.sha256(content).hexdigest()
    doc = SourceDocumentModel(
        id=str(uuid.uuid4()),
        file_name="deleted_doc.xlsx",
        file_type="xlsx",
        sha256=digest,
        blob_uri=f"blob://uploads/{digest}.xlsx",
        spec_version="1.3",
        status="DELETED",
        block_count=1,
    )
    session.add(doc)
    session.commit()

    class _SessionCtx:
        def __enter__(self):
            return session

        def __exit__(self, *args):
            return False

    settings = Settings(blob_dir=str(tmp_path / "blobs"))
    monkeypatch.setattr(documents_api, "SessionLocal", lambda: _SessionCtx())
    monkeypatch.setattr(documents_api, "get_settings", lambda: settings)
    captured = {}

    def _fake_start_prepare(doc_id, file_name, _settings, **kwargs):
        captured["doc_id"] = doc_id
        captured["reimport_deleted"] = kwargs.get("reimport_deleted")
        return "run-prepare-1"

    monkeypatch.setattr(documents_api, "start_document_prepare_run", _fake_start_prepare)
    up = UploadFile(filename="deleted_doc.xlsx", file=io.BytesIO(content))
    resp = asyncio.run(documents_api.upload_document(up))
    assert resp["processing"] is True
    assert captured["doc_id"] == doc.id
    assert captured["reimport_deleted"] is True

    # 活跃解析态禁止软删除
    doc.status = "EXTRACTING"
    session.commit()
    with pytest.raises(HTTPException) as exc_del:
        documents_api.delete_document(doc.id, session=session)
    assert exc_del.value.status_code == 409

    # extract_document_parallel 拒绝跨文档 block_id 与无效状态
    doc.status = "UPLOADED"
    other_doc, other_block, _ = _seed_doc_block_table(session, table_code="T_OTHER")
    session.commit()
    with pytest.raises(HTTPException) as exc_blk:
        documents_api.extract_document_parallel(
            doc.id,
            documents_api.ParallelExtractRequest(block_ids=[other_block.id]),
            session=session,
        )
    assert exc_blk.value.status_code == 422

    # adjust_structure 拒绝非法 block_type
    with pytest.raises(HTTPException) as exc_bt:
        documents_api.adjust_structure(other_doc.id, other_block.id, {"block_type": "HACKED_TYPE"}, session=session)
    assert exc_bt.value.status_code == 422
    ok_adj = documents_api.adjust_structure(other_doc.id, other_block.id, {"block_type": "CODE_SET"}, session=session)
    assert ok_adj["block_type"] == "CODE_SET"


def test_artifacts_excludes_merged_and_avoids_n_plus_1():
    """Fix 5: GET /artifacts 与 /artifacts/rules 排除 MERGED 表且批量折叠无 N+1 风暴。"""
    session = make_session()
    doc = SourceDocumentModel(
        id=str(uuid.uuid4()),
        file_name="multi.pdf",
        file_type="pdf",
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        blob_uri="blob://multi.pdf",
        spec_version="1.3",
        status="EXTRACTED",
        block_count=11,
    )
    session.add(doc)
    session.flush()

    spec_tables = []
    for i in range(8):
        code = f"T_BATCH_{i}"
        blk = RawBlockModel(
            id=str(uuid.uuid4()),
            document_id=doc.id,
            ordinal=i,
            block_type="TABLE_SPEC",
            raw_content="",
            source_locator={},
            content_json={},
            raw_hash=uuid.uuid4().hex,
        )
        tbl = TableMetadataModel(
            id=str(uuid.uuid4()),
            document_id=doc.id,
            block_id=blk.id,
            table_code=code,
            table_name=f"表{i}",
            table_definition="",
            status="PUBLISHED",
            current_version=1,
            published_version=1,
            source_ordinal=i,
        )
        session.add_all([blk, tbl])
        spec_tables.append(
            SpecDocument(
                version="1.3",
                table_code=code,
                table_name=f"表{i}",
                section="6.1",
                source_name="multi.pdf",
                source_sha256="a" * 64,
                terms_text="",
                dictionary_text="",
                fields=[FieldSpec(name="F1", content="字段1", data_type="VARCHAR", length=10, check_text="必填", evidence_page="1")],
            ).model_dump()
        )
    # 额外塞一张 MERGED 碎片表
    mblk = RawBlockModel(
        id=str(uuid.uuid4()),
        document_id=doc.id,
        ordinal=99,
        block_type="TABLE_SPEC",
        raw_content="",
        source_locator={},
        content_json={},
        raw_hash=uuid.uuid4().hex,
    )
    mtbl = TableMetadataModel(
        id=str(uuid.uuid4()),
        document_id=doc.id,
        block_id=mblk.id,
        table_code="T_GHOST_MERGED",
        table_name="已合并碎片",
        table_definition="",
        status="MERGED",
        current_version=1,
        source_ordinal=99,
    )
    session.add_all([mblk, mtbl])
    spec = SpecificationModel(
        id=str(uuid.uuid4()),
        version="METADATA_PUBLISHED",
        table_code="METADATA_SNAPSHOT",
        source_name="multi.pdf",
        source_sha256="a" * 64,
        content_json={"tables": spec_tables},
    )
    session.add(spec)
    session.flush()
    run = kernel_create_run(session, spec.id)
    append_event(session, run, "APPROVAL_REQUIRED", "human_gate", {"pending_count": 8})
    for tbl_dict in spec_tables:
        code = tbl_dict["table_code"]
        defn = RuleDefinition(
            rule_key=f"{code}_F1_REQ",
            table_code=code,
            field="F1",
            operator="REQUIRED",
            params={},
            source="DOC_EXPLICIT",
            evidence=Evidence(section="6.1", page="1", field="F1", quote="必填"),
            rationale="必填",
        )
        defn.rule_hash = calculate_rule_hash(defn, "a" * 64)
        session.add(
            RuleModel(
                row_id=str(uuid.uuid4()),
                run_id=run.id,
                spec_id=spec.id,
                table_code=code,
                rule_key=defn.rule_key,
                rule_hash=defn.rule_hash,
                source=defn.source,
                status="PENDING",
                definition_json=defn.model_dump(),
            )
        )
    session.commit()

    sql_counts = {"artifacts": 0, "rules": 0}
    active_bucket = {"name": None}

    @event.listens_for(session.bind, "before_cursor_execute")
    def _count_sql(_conn, _cursor, _stmt, _params, _ctx, _many):
        if active_bucket["name"]:
            sql_counts[active_bucket["name"]] += 1

    active_bucket["name"] = "artifacts"
    status_res = artifacts_api.artifacts_status(session=session)
    active_bucket["name"] = "rules"
    rules_res = artifacts_api.all_table_rules(session=session)
    active_bucket["name"] = None

    assert "T_GHOST_MERGED" not in {t["table_code"] for t in status_res["tables"]}
    assert "T_GHOST_MERGED" not in {t["table"]["table_code"] for t in rules_res["tables"]}
    assert len(status_res["tables"]) == 8
    assert len(rules_res["tables"]) == 8
    # 8 张表全量查询必须远低于旧 N+1 水平（<= 10 条 SQL）
    assert sql_counts["artifacts"] <= 10
    assert sql_counts["rules"] <= 10


def test_edit_rule_active_lock_oracle_regexp_fk_codeset_dup_and_event_log():
    """Fix 3: PATCH /artifacts/rules/{id} 运行锁、Oracle 正则规范化、外键/码表校验、重哈希拦截与 EventLog 留痕。"""
    session = make_session()
    spec_main = SpecDocument(
        version="1.3",
        table_code="T_MAIN",
        table_name="主表",
        section="6.1",
        source_name="spec.pdf",
        source_sha256="b" * 64,
        terms_text="",
        dictionary_text="",
        fields=[
            FieldSpec(name="CODE", content="编码", data_type="VARCHAR", length=18, check_text="字母数字或短横线", evidence_page="1"),
            FieldSpec(name="CCY", content="币种", data_type="VARCHAR", length=3, check_text="币种枚举", evidence_page="1"),
            FieldSpec(name="PARENT_ID", content="关联号", data_type="VARCHAR", length=18, check_text="外键", evidence_page="1"),
        ],
    )
    spec_ref = SpecDocument(
        version="1.3",
        table_code="T_PARENT",
        table_name="父表",
        section="6.0",
        source_name="spec.pdf",
        source_sha256="b" * 64,
        terms_text="",
        dictionary_text="",
        fields=[FieldSpec(name="ID", content="主键", data_type="VARCHAR", length=18, check_text="主键", evidence_page="1")],
    )
    spec_model = SpecificationModel(
        id=str(uuid.uuid4()),
        version="METADATA_PUBLISHED",
        table_code="METADATA_SNAPSHOT",
        source_name="spec.pdf",
        source_sha256="b" * 64,
        content_json={"tables": [spec_main.model_dump(), spec_ref.model_dump()]},
    )
    session.add(spec_model)
    session.flush()
    run = kernel_create_run(session, spec_model.id)  # 初始为 RUNNING

    def _add_rule(key, field, op, params, status="PENDING"):
        d = RuleDefinition(
            rule_key=key,
            table_code="T_MAIN",
            field=field,
            operator=op,
            params=params,
            source="AI_EXPANDED",
            evidence=Evidence(section="6.1", page="1", field=field, quote="原文"),
            rationale="测试",
        )
        d.rule_hash = calculate_rule_hash(d, "b" * 64)
        row = RuleModel(
            row_id=str(uuid.uuid4()),
            run_id=run.id,
            spec_id=spec_model.id,
            table_code="T_MAIN",
            rule_key=key,
            rule_hash=d.rule_hash,
            source=d.source,
            status=status,
            definition_json=d.model_dump(),
        )
        session.add(row)
        return row

    r_regex = _add_rule("R_REGEX", "CODE", "REGEXP", {"pattern": "^[A-Z0-9]+$"})
    r_regex2 = _add_rule("R_REGEX", "CODE", "REGEXP", {"pattern": "^[A-Z0-9-]+$"}, status="SUPERSEDED")
    r_enum = _add_rule("R_ENUM", "CCY", "ENUM", {"values": ["CNY", "USD"]})
    r_fk = _add_rule("R_FK", "PARENT_ID", "FOREIGN_KEY", {"references_table": "T_PARENT", "references_field": "ID"})
    r_sup = _add_rule("R_SUP", "CODE", "REQUIRED", {}, status="SUPERSEDED")
    session.commit()

    # 1) RUNNING 状态禁止修改规则 -> 409
    with pytest.raises(HTTPException) as exc_running:
        artifacts_api.edit_rule(
            r_regex.row_id,
            artifacts_api.RuleEditRequest(params={"pattern": r"^[A-Z0-9\-]+$"}),
            session=session,
        )
    assert exc_running.value.status_code == 409

    # 推进至 WAITING_APPROVAL（非活跃态）
    append_event(session, run, "APPROVAL_REQUIRED", "human_gate", {"pending_count": 4})
    session.commit()

    # 2) SUPERSEDED 规则禁止修改 -> 409
    with pytest.raises(HTTPException) as exc_sup:
        artifacts_api.edit_rule(r_sup.row_id, artifacts_api.RuleEditRequest(params={}), session=session)
    assert exc_sup.value.status_code == 409

    # 3) Oracle POSIX 规范化 + 重复哈希冲突拦截 -> 409（与 r_regex2 撞车！）
    with pytest.raises(HTTPException) as exc_dup:
        artifacts_api.edit_rule(
            r_regex.row_id,
            artifacts_api.RuleEditRequest(params={"pattern": r"^[A-Z0-9\-]+$"}),
            session=session,
        )
    assert exc_dup.value.status_code == 409

    # 4) 合法 Oracle 正则修改 -> 规范化为 [A-Z0-9_-] 并写 EventLog 且哈希链完整
    edited = artifacts_api.edit_rule(
        r_regex.row_id,
        artifacts_api.RuleEditRequest(params={"pattern": r"^[A-Z\d_\-]+$"}, editor="qa", reason="补下划线"),
        session=session,
    )
    assert edited["rule"]["params"]["pattern"] == "^[A-Z0-9_-]+$"
    edit_events = session.query(EventModel).filter_by(run_id=run.id, kind="RULE_EDITED").all()
    assert len(edit_events) == 1
    assert verify_chain(session, run) is True

    # 5) ENUM 指定未发布码表 -> 422
    with pytest.raises(HTTPException) as exc_cs:
        artifacts_api.edit_rule(
            r_enum.row_id,
            artifacts_api.RuleEditRequest(params={"values": ["CNY"], "code_set": "NON_EXISTENT_CS"}),
            session=session,
        )
    assert exc_cs.value.status_code == 422

    # 6) FOREIGN_KEY 指定不存在字段 -> 422
    with pytest.raises(HTTPException) as exc_fk:
        artifacts_api.edit_rule(
            r_fk.row_id,
            artifacts_api.RuleEditRequest(params={"references_table": "T_PARENT", "references_field": "NO_SUCH_COL"}),
            session=session,
        )
    assert exc_fk.value.status_code == 422


def test_settings_execution_budget_validation_blocks_poison_values():
    """Fix 4: execution_budget 拒绝负数、0 并发、bool、字符串、未知键，防止毒化全站任务。"""
    session = make_session()
    bad_payloads = [
        {"max_llm_concurrency": 0},
        {"max_llm_concurrency": 999},
        {"max_retries": -1},
        {"max_rules_per_table": True},
        {"max_llm_calls": "120"},
        {"unknown_key": 10},
    ]
    for bad in bad_payloads:
        with pytest.raises(HTTPException) as exc:
            settings_api.save_setting(
                "execution_budget",
                settings_api.SettingRequest(value=bad),
                session=session,
            )
        assert exc.value.status_code == 422

    ok = settings_api.save_setting(
        "execution_budget",
        settings_api.SettingRequest(value={"max_llm_concurrency": 6, "max_retries": 2}),
        session=session,
    )
    assert ok == {"key": "execution_budget", "saved": True}


def test_generation_dedups_tables_blocks_merged_and_sanitizes_bundle(monkeypatch, tmp_path):
    """Fix 10: 发起生成自动去重 table_ids、拦 MERGED 防止被复活发布、打包异常回滚并净化下载文件名。"""
    session = make_session()
    doc, _, merged_tbl = _seed_doc_block_table(session, table_code="T_OLD_MERGED", status="MERGED", ordinal=1)
    session.commit()

    settings = Settings(blob_dir=str(tmp_path / "blobs"))
    with pytest.raises(HTTPException) as exc_merged:
        generation_api._prepare_generation_run(
            session,
            generation_api.GenerationRequest(table_ids=[merged_tbl.id, merged_tbl.id], approver="ops"),
            settings,
        )
    assert exc_merged.value.status_code == 409
    assert session.get(TableMetadataModel, merged_tbl.id).status == "MERGED"

    # 正常单表重复传 2 次，应自动去重为 1 张表进入快照
    _, _, valid_tbl = _seed_doc_block_table(session, table_code="T_VALID_DEDUP", status="PUBLISHED", ordinal=2)
    session.add(
        FieldMetadataModel(
            id=str(uuid.uuid4()),
            table_id=valid_tbl.id,
            field_cn="编号",
            field_en="RPTNO",
            field_definition="",
            check_text_raw="必填",
            type_basis="SOURCE",
            data_type="VARCHAR",
            length=20,
            decimal_places=0,
            required=True,
            code_reference_text="",
            confidence=1.0,
            evidence_json={},
            sort_order=0,
            ai_value_json={},
        )
    )
    session.commit()
    run, _, _, _ = generation_api._prepare_generation_run(
        session,
        generation_api.GenerationRequest(table_ids=[valid_tbl.id, valid_tbl.id], approver="ops"),
        settings,
    )
    spec = session.get(SpecificationModel, run.spec_id)
    assert len(spec.content_json["tables"]) == 1

    # 未完成审计时下载生产包 -> 409 且 session 干净可用
    monkeypatch.setattr(generation_api, "get_settings", lambda: settings)
    with pytest.raises(HTTPException) as exc_bundle:
        generation_api.download_production_bundle(run.id, session=session)
    assert exc_bundle.value.status_code == 409


def test_system_blob_rejects_binary_zip_and_oversize_preview(monkeypatch, tmp_path):
    """Fix 9: GET /api/blobs/{digest} 对二进制 ZIP / 非 UTF-8 / 超大文本返回 422/413，不再 500。"""
    settings = Settings(blob_dir=str(tmp_path / "blobs"))
    monkeypatch.setattr(system_api, "get_settings", lambda: settings)
    store = LocalBlobStore(settings.blob_dir)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("readme.txt", "hello")
    zip_uri = store.put(buf.getvalue())
    zip_digest = zip_uri.removeprefix("blob://")
    with pytest.raises(HTTPException) as exc_zip:
        system_api.get_blob(zip_digest)
    assert exc_zip.value.status_code == 422

    bad_utf8_uri = store.put(b"\xff\xfe\xfd\x80")
    with pytest.raises(HTTPException) as exc_utf8:
        system_api.get_blob(bad_utf8_uri.removeprefix("blob://"))
    assert exc_utf8.value.status_code == 422

    big_uri = store.put(b"x" * (5 * 1024 * 1024 + 16))
    with pytest.raises(HTTPException) as exc_big:
        system_api.get_blob(big_uri.removeprefix("blob://"))
    assert exc_big.value.status_code == 413

    json_uri = store.put(b'{"ok": true}')
    ok = system_api.get_blob(json_uri.removeprefix("blob://"))
    assert ok["json"] == {"ok": True}


def test_runs_oracle_preview_unconfigured_422_and_clean_messages(monkeypatch):
    """Fix 11 & 12: 未配 Oracle 沙箱返回 422、无过时 GLM 文案、CORS 过滤空源。"""
    session = make_session()
    with pytest.raises(HTTPException) as exc_live:
        runs_api.create_run(RunRequest(spec_id="s1", provider="scripted"), session=session)
    assert exc_live.value.status_code == 422
    assert "GLM" not in exc_live.value.detail

    run = kernel_create_run(session, "spec-1")
    append_event(
        session,
        run,
        "ORACLE_ASSETS_PREPARED",
        "oracle_asset_builder",
        {"result": {"oracle_tables": {"T1": "DQ_T1"}}},
    )
    session.commit()
    monkeypatch.setattr(runs_api, "get_settings", lambda: Settings(oracle_sandbox_url=None))
    with pytest.raises(HTTPException) as exc_ora:
        runs_api.oracle_preview(run.id, table_code="T1", limit=10, dataset="audit", session=session)
    assert exc_ora.value.status_code == 422

    from app.main import app as fastapi_app

    cors_mw = next(mw for mw in fastapi_app.user_middleware if mw.cls.__name__ == "CORSMiddleware")
    assert all(origin.strip() for origin in cors_mw.kwargs["allow_origins"])


def test_deleted_document_and_superseded_block_tables_excluded_from_artifacts_and_readiness():
    """已软删（DELETED）文档和 superseded 块的脏表不得泄漏进第二步 /artifacts、/rules 与未决码值引用。"""
    session = make_session()
    active_doc, _, active_tbl = _seed_doc_block_table(session, table_code="T_LIVE_DOC", status="PUBLISHED", ordinal=1)
    del_doc, _, del_tbl = _seed_doc_block_table(session, table_code="T_DELETED_DIRTY", status="PUBLISHED", ordinal=2)
    del_doc.status = "DELETED"

    sup_blk = RawBlockModel(
        id=str(uuid.uuid4()),
        document_id=active_doc.id,
        ordinal=9,
        block_type="XLSX_SHEET",
        raw_content="",
        source_locator={},
        content_json={"superseded": True},
        raw_hash=uuid.uuid4().hex,
    )
    session.add(sup_blk)
    session.flush()
    sup_tbl = TableMetadataModel(
        id=str(uuid.uuid4()),
        document_id=active_doc.id,
        block_id=sup_blk.id,
        table_code="T_SUP_ORPHAN",
        table_name="失效块孤儿表",
        table_definition="",
        status="DRAFT",
        current_version=1,
        source_ordinal=9,
    )
    session.add(sup_tbl)
    session.commit()

    all_art = artifacts_api.artifacts_status(session=session)
    codes = [t["table_code"] for t in all_art["tables"]]
    assert codes == ["T_LIVE_DOC"]

    scoped_art = artifacts_api.artifacts_status(document_id=active_doc.id, session=session)
    assert [t["table_code"] for t in scoped_art["tables"]] == ["T_LIVE_DOC"]

    empty_art = artifacts_api.artifacts_status(document_id=del_doc.id, session=session)
    assert empty_art["tables"] == []

