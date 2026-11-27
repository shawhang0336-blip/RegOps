"""版本演进与变更中心接口测试（/api/metadata/revisions、增强版 packages/diff 与 runs/diff）。"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.common import db_session as api_db_session
from app.infra.db import Base
from app.infra.db_tables import (
    CodeSetModel,
    CodeValueModel,
    FieldMetadataModel,
    MetadataRevisionModel,
    RawBlockModel,
    RunModel,
    SourceDocumentModel,
    SpecPackageModel,
    SpecificationModel,
    TableMetadataModel,
    TableRuleEvidenceModel,
)
from app.kernel.event_log import append_event
from app.main import app


def _make_threadsafe_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _make_client(session_factory):
    def _override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[api_db_session] = _override_db
    return TestClient(app)


def test_metadata_revisions_feed_and_table_revision_stats():
    sf = _make_threadsafe_session_factory()
    session = sf()
    try:
        pkg_east = SpecPackageModel(id=str(uuid.uuid4()), name="EAST 5.0", regulator="EAST", spec_version="5.0", status="ACTIVE")
        pkg_del = SpecPackageModel(id=str(uuid.uuid4()), name="已删包", regulator="EAST", spec_version="4.0", status="DELETED")
        doc = SourceDocumentModel(
            id=str(uuid.uuid4()),
            package_id=pkg_east.id,
            file_name="east5.xlsx",
            file_type="XLSX",
            sha256="1" * 64,
            blob_uri="blob://e5",
            spec_version="5.0",
            status="EXTRACTED",
        )
        blk_a = RawBlockModel(
            id=str(uuid.uuid4()),
            document_id=doc.id,
            ordinal=1,
            block_type="SPEC_TABLE",
            raw_content="表1",
            source_locator={},
            content_json={},
            raw_hash="ha",
        )
        blk_b = RawBlockModel(
            id=str(uuid.uuid4()),
            document_id=doc.id,
            ordinal=2,
            block_type="SPEC_TABLE",
            raw_content="表2",
            source_locator={},
            content_json={},
            raw_hash="hb",
        )
        tbl = TableMetadataModel(
            id=str(uuid.uuid4()),
            document_id=doc.id,
            block_id=blk_a.id,
            package_id=pkg_east.id,
            table_code="S_LOAN_JJ",
            table_name="贷款借据表",
            table_definition="",
            status="PUBLISHED",
            current_version=2,
            published_version=2,
        )
        tbl_del = TableMetadataModel(
            id=str(uuid.uuid4()),
            document_id=doc.id,
            block_id=blk_b.id,
            package_id=pkg_del.id,
            table_code="OLD_DEL",
            table_name="已删表",
            table_definition="",
            status="DRAFT",
            current_version=1,
        )
        session.add_all([pkg_east, pkg_del, doc, blk_a, blk_b, tbl, tbl_del])
        session.flush()

        session.add_all(
            [
                MetadataRevisionModel(
                    id=str(uuid.uuid4()),
                    table_id=tbl.id,
                    revision_kind="AI_EXTRACTION",
                    old_value_json=None,
                    new_value_json={"draft": {"table_code": "S_LOAN_JJ"}},
                    editor="AI",
                    reason="初始解析",
                ),
                MetadataRevisionModel(
                    id=str(uuid.uuid4()),
                    table_id=tbl.id,
                    revision_kind="HUMAN_EDIT",
                    old_value_json={"field_en": "AMT", "length": 16},
                    new_value_json={"field_en": "AMT", "length": 20},
                    editor="张治理",
                    reason="放宽金额列宽",
                ),
                MetadataRevisionModel(
                    id=str(uuid.uuid4()),
                    table_id=tbl.id,
                    revision_kind="PUBLISHED",
                    old_value_json={"version": 1},
                    new_value_json={"version": 2},
                    editor="张治理",
                    reason="发布 v2",
                ),
                MetadataRevisionModel(
                    id=str(uuid.uuid4()),
                    table_id=tbl_del.id,
                    revision_kind="HUMAN_EDIT",
                    old_value_json={},
                    new_value_json={},
                    editor="ghost",
                    reason="已删包改动不应出现",
                ),
            ]
        )
        session.commit()

        client = _make_client(sf)

        # 1. 全量动态：自动排除 DELETED 包
        resp = client.get("/api/metadata/revisions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"]["total"] == 3
        assert body["summary"]["human_edits"] == 1
        assert body["summary"]["publishes"] == 1
        assert body["summary"]["ai_events"] == 1
        assert len(body["revisions"]) == 3

        # 2. human_only=true 去噪，只保留人工修改与发布
        resp_h = client.get("/api/metadata/revisions", params={"human_only": "true", "regulator": "NFRA"})
        assert resp_h.status_code == 200
        kinds = {r["revision_kind"] for r in resp_h.json()["revisions"]}
        assert kinds == {"HUMAN_EDIT", "PUBLISHED"}

        # 3. 报表列表返回修订计数统计
        tbls = client.get("/api/metadata/tables", params={"package_id": pkg_east.id}).json()
        assert len(tbls) == 1
        assert tbls[0]["revision_count"] == 3
        assert tbls[0]["human_revision_count"] == 1
        assert tbls[0]["last_revised_at"] is not None
    finally:
        app.dependency_overrides.pop(api_db_session, None)
        session.close()


def test_package_diff_includes_code_set_values_and_rule_evidence_details():
    sf = _make_threadsafe_session_factory()
    session = sf()
    try:
        pkg1 = SpecPackageModel(id=str(uuid.uuid4()), name="EAST 5.0", regulator="EAST", spec_version="5.0", status="ACTIVE")
        pkg2 = SpecPackageModel(
            id=str(uuid.uuid4()),
            name="EAST 5.1",
            regulator="EAST",
            spec_version="5.1",
            parent_package_id=pkg1.id,
            status="ACTIVE",
        )
        doc1 = SourceDocumentModel(
            id=str(uuid.uuid4()),
            package_id=pkg1.id,
            file_name="east50.xlsx",
            file_type="XLSX",
            sha256="2" * 64,
            blob_uri="blob://e50",
            spec_version="5.0",
            status="EXTRACTED",
        )
        doc2 = SourceDocumentModel(
            id=str(uuid.uuid4()),
            package_id=pkg2.id,
            file_name="east51.xlsx",
            file_type="XLSX",
            sha256="3" * 64,
            blob_uri="blob://e51",
            spec_version="5.1",
            status="EXTRACTED",
        )
        blk1 = RawBlockModel(
            id=str(uuid.uuid4()),
            document_id=doc1.id,
            ordinal=1,
            block_type="RULE_TABLE",
            raw_content="规则1",
            source_locator={},
            content_json={},
            raw_hash="h1",
        )
        blk2 = RawBlockModel(
            id=str(uuid.uuid4()),
            document_id=doc2.id,
            ordinal=1,
            block_type="RULE_TABLE",
            raw_content="规则2",
            source_locator={},
            content_json={},
            raw_hash="h2",
        )
        t1 = TableMetadataModel(
            id=str(uuid.uuid4()),
            document_id=doc1.id,
            block_id=blk1.id,
            package_id=pkg1.id,
            table_code="S_LOAN_JJ",
            table_name="贷款借据表",
            table_definition="",
            status="PUBLISHED",
            current_version=1,
        )
        t2 = TableMetadataModel(
            id=str(uuid.uuid4()),
            document_id=doc2.id,
            block_id=blk2.id,
            package_id=pkg2.id,
            table_code="S_LOAN_JJ",
            table_name="贷款借据表",
            table_definition="",
            status="PUBLISHED",
            current_version=1,
        )
        session.add_all([pkg1, pkg2, doc1, doc2, blk1, blk2, t1, t2])
        session.flush()

        session.add_all(
            [
                FieldMetadataModel(
                    id=str(uuid.uuid4()),
                    table_id=t1.id,
                    field_cn="币种",
                    field_en="CURR_CD",
                    field_definition="",
                    check_text_raw="",
                    data_type="VARCHAR",
                    length=3,
                    decimal_places=0,
                    required=True,
                    evidence_json={},
                    sort_order=1,
                    ai_value_json={},
                ),
                FieldMetadataModel(
                    id=str(uuid.uuid4()),
                    table_id=t2.id,
                    field_cn="币种",
                    field_en="CURR_CD",
                    field_definition="",
                    check_text_raw="",
                    data_type="VARCHAR",
                    length=3,
                    decimal_places=0,
                    required=True,
                    evidence_json={},
                    sort_order=1,
                    ai_value_json={},
                ),
            ]
        )

        # 码表：旧版 {CNY, USD} -> 新版 {CNY(改名), EUR(新增)}，移除 USD 应判 BREAKING
        cs1 = CodeSetModel(
            id=str(uuid.uuid4()),
            package_id=pkg1.id,
            code="CS_CURR",
            name="币种代码表",
            source_type="DOCUMENT_REFERENCE",
            status="PUBLISHED",
            current_version=1,
        )
        cs2 = CodeSetModel(
            id=str(uuid.uuid4()),
            package_id=pkg2.id,
            code="CS_CURR",
            name="币种代码表",
            source_type="DOCUMENT_REFERENCE",
            status="PUBLISHED",
            current_version=1,
        )
        session.add_all([cs1, cs2])
        session.flush()
        session.add_all(
            [
                CodeValueModel(id=str(uuid.uuid4()), code_set_id=cs1.id, value="CNY", label_cn="人民币", sort_order=1, enabled=True, source_locator={}),
                CodeValueModel(id=str(uuid.uuid4()), code_set_id=cs1.id, value="USD", label_cn="美元", sort_order=2, enabled=True, source_locator={}),
                CodeValueModel(id=str(uuid.uuid4()), code_set_id=cs2.id, value="CNY", label_cn="中国人民币", sort_order=1, enabled=True, source_locator={}),
                CodeValueModel(id=str(uuid.uuid4()), code_set_id=cs2.id, value="EUR", label_cn="欧元", sort_order=2, enabled=True, source_locator={}),
            ]
        )

        # 补充核查规则：R001 修改条文，R002 新增
        session.add_all(
            [
                TableRuleEvidenceModel(
                    id=str(uuid.uuid4()),
                    package_id=pkg1.id,
                    document_id=doc1.id,
                    block_id=blk1.id,
                    table_code="S_LOAN_JJ",
                    table_name="贷款借据表",
                    rule_code="R001",
                    rule_name="余额非负",
                    severity="HARD",
                    scope="SINGLE_TABLE",
                    target_fields_json=["BAL"],
                    rule_text="余额应 >= 0",
                    rule_hash="rh1",
                ),
                TableRuleEvidenceModel(
                    id=str(uuid.uuid4()),
                    package_id=pkg2.id,
                    document_id=doc2.id,
                    block_id=blk2.id,
                    table_code="S_LOAN_JJ",
                    table_name="贷款借据表",
                    rule_code="R001",
                    rule_name="余额非负",
                    severity="HARD",
                    scope="SINGLE_TABLE",
                    target_fields_json=["BAL"],
                    rule_text="余额应 > 0 且 <= 合同金额",
                    rule_hash="rh1_v2",
                ),
                TableRuleEvidenceModel(
                    id=str(uuid.uuid4()),
                    package_id=pkg2.id,
                    document_id=doc2.id,
                    block_id=blk2.id,
                    table_code="S_LOAN_JJ",
                    table_name="贷款借据表",
                    rule_code="R002",
                    rule_name="币种必填",
                    severity="HARD",
                    scope="SINGLE_TABLE",
                    target_fields_json=["CURR_CD"],
                    rule_text="CURR_CD 不得为空",
                    rule_hash="rh2",
                ),
            ]
        )
        session.commit()

        client = _make_client(sf)
        resp = client.get("/api/packages/diff", params={"base_id": pkg1.id, "target_id": pkg2.id})
        assert resp.status_code == 200
        diff = resp.json()

        assert diff["summary"]["code_sets"]["modified"] == 1
        assert diff["summary"]["code_sets"]["added_values"] == 1
        assert diff["summary"]["code_sets"]["removed_values"] == 1
        cs_diff = diff["code_sets"][0]
        assert cs_diff["code"] == "CS_CURR"
        assert cs_diff["impact"] == "BREAKING"
        assert [v["value"] for v in cs_diff["added_values"]] == ["EUR"]
        assert [v["value"] for v in cs_diff["removed_values"]] == ["USD"]
        assert cs_diff["modified_values"][0]["value"] == "CNY"

        assert diff["summary"]["rule_evidences"]["added"] == 1
        assert diff["summary"]["rule_evidences"]["modified"] == 1
        assert diff["summary"]["rule_evidences"]["removed"] == 0
        assert diff["rule_evidences"]["modified"][0]["rule_code"] == "R001"
        assert diff["rule_evidences"]["added"][0]["rule_code"] == "R002"
    finally:
        app.dependency_overrides.pop(api_db_session, None)
        session.close()


def test_run_diff_returns_tables_and_shared_tables():
    sf = _make_threadsafe_session_factory()
    session = sf()
    try:
        spec1 = SpecificationModel(
            id=str(uuid.uuid4()),
            table_code="S_LOAN_JJ",
            source_name="east.xlsx",
            source_sha256="a" * 64,
            version="5.0",
            content_json={"regulator": "EAST", "tables": [{"table_code": "S_LOAN_JJ", "table_name": "贷款借据表"}]},
        )
        spec2 = SpecificationModel(
            id=str(uuid.uuid4()),
            table_code="S_LOAN_JJ",
            source_name="east.xlsx",
            source_sha256="a" * 64,
            version="5.1",
            content_json={"regulator": "EAST", "tables": [{"table_code": "S_LOAN_JJ", "table_name": "贷款借据表"}]},
        )
        r1 = RunModel(id=str(uuid.uuid4()), spec_id=spec1.id)
        r2 = RunModel(id=str(uuid.uuid4()), spec_id=spec2.id)
        session.add_all([spec1, spec2, r1, r2])
        session.flush()
        append_event(session, r1, "RUN_CREATED", "table_confirmation", {"spec_id": spec1.id})
        append_event(session, r2, "RUN_CREATED", "table_confirmation", {"spec_id": spec2.id})
        session.commit()

        client = _make_client(sf)
        resp = client.get("/api/runs/diff", params={"base_run_id": r1.id, "target_run_id": r2.id})
        assert resp.status_code == 200
        body = resp.json()
        assert body["shared_tables"] == ["S_LOAN_JJ"]
        assert body["base_run"]["tables"][0]["table_code"] == "S_LOAN_JJ"
        assert body["target_run"]["tables"][0]["table_code"] == "S_LOAN_JJ"
    finally:
        app.dependency_overrides.pop(api_db_session, None)
        session.close()
