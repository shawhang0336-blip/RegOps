"""全链路合规审计中心接口测试（/api/runs/audit-overview 与 /api/runs/{id}/audit-detail）。"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.common import db_session as api_db_session
from app.infra.db import Base
from app.infra.db_tables import (
    EventModel,
    RunModel,
    SourceDocumentModel,
    SpecPackageModel,
    SpecificationModel,
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


def _seed_generation_run(session, *, regulator: str = "EAST", package_name: str = "EAST 5.0 规范包"):
    pkg_id = str(uuid.uuid4())
    spec_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    pkg = SpecPackageModel(
        id=pkg_id,
        name=package_name,
        regulator=regulator,
        spec_version="5.0",
        status="ACTIVE",
    )
    spec = SpecificationModel(
        id=spec_id,
        table_code="S_LOAN_JJ",
        source_name="EAST5_DEMO.xlsx",
        source_sha256="a" * 64,
        version="5.0",
        content_json={
            "package_id": pkg_id,
            "package_name": package_name,
            "regulator": regulator,
            "tables": [
                {
                    "table_code": "S_LOAN_JJ",
                    "table_name": "贷款借据表",
                    "source_name": "EAST5_DEMO.xlsx",
                    "source_sha256": "a" * 64,
                    "version": "5.0",
                }
            ],
        },
    )
    run = RunModel(id=run_id, spec_id=spec_id)
    session.add_all([pkg, spec, run])
    session.flush()

    append_event(
        session,
        run,
        "RUN_CREATED",
        "table_confirmation",
        {"spec_id": spec_id, "provider": "live", "operator": "王审"},
    )
    append_event(
        session,
        run,
        "BUS_REJECTED",
        "rule_barrier",
        {"tool": "propose_rules", "reason": "幻觉字段 FAKE_COL 不存在"},
    )
    append_event(
        session,
        run,
        "GO_REDIRECT",
        "oracle_executor",
        {"target": "rule_binder", "reason": "自动回边修复规则参数"},
    )
    append_event(
        session,
        run,
        "HUMAN_DECISIONS_RECORDED",
        "human_gate",
        {"approver": "王审", "decisions": [{"rule_hash": "r1", "decision": "APPROVED"}]},
    )
    append_event(
        session,
        run,
        "AUDIT_COMPLETED",
        "oracle_executor",
        {
            "result": {
                "audit_summary": {"all_passed": True},
                "audit_report_blob": "blob://audit-demo",
                "budget_usage": {
                    "llm_calls": 3,
                    "total_tokens": 2500,
                    "prompt_tokens": 1800,
                    "completion_tokens": 700,
                    "elapsed_seconds": 4.2,
                    "total_llm_seconds": 3.8,
                },
            }
        },
    )
    append_event(
        session,
        run,
        "TEST_RESULTS_CONFIRMED",
        "test_confirmation",
        {"approver": "李核", "object_hash": "b" * 64},
    )
    session.commit()
    return pkg_id, run_id


def _seed_ingestion_run(session, *, pkg_id: str):
    doc_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    doc = SourceDocumentModel(
        id=doc_id,
        package_id=pkg_id,
        file_name="EAST5_SPEC.pdf",
        file_type="PDF",
        doc_role="SPEC_MAIN",
        sha256="c" * 64,
        blob_uri="blob://doc-demo",
        status="EXTRACTED",
        spec_version="5.0",
    )
    run = RunModel(id=run_id, spec_id=doc_id)
    session.add_all([doc, run])
    session.flush()

    append_event(
        session,
        run,
        "RUN_CREATED",
        "extract",
        {"run_kind": "INGESTION", "phase": "extract", "file_name": "EAST5_SPEC.pdf"},
    )
    append_event(
        session,
        run,
        "INGESTION_STAGE",
        "parallel_extraction",
        {"stage": "parallel_extraction", "message": "分块解析完成"},
    )
    append_event(
        session,
        run,
        "INGESTION_COMPLETED",
        "parallel_extraction",
        {"result": {"tables_created": 1}},
    )
    session.commit()
    return run_id


def _make_test_client(session_factory):
    def _override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[api_db_session] = _override_db
    return TestClient(app)


def test_audit_overview_and_detail_full_chain():
    session_factory = _make_threadsafe_session_factory()
    session = session_factory()
    try:
        pkg_id, gen_run_id = _seed_generation_run(session)
        ing_run_id = _seed_ingestion_run(session, pkg_id=pkg_id)
        client = _make_test_client(session_factory)

        # 1. 总览：同时包含 GENERATION 和 INGESTION 批次，且全部哈希链完整
        resp = client.get("/api/runs/audit-overview")
        assert resp.status_code == 200
        body = resp.json()
        summary = body["summary"]
        assert summary["total_runs"] == 2
        assert summary["generation_runs"] == 1
        assert summary["ingestion_runs"] == 1
        assert summary["chain_valid_runs"] == 2
        assert summary["chain_broken_runs"] == 0
        assert summary["total_bus_rejections"] == 1
        assert summary["total_self_heals"] == 1
        assert summary["total_human_confirmations"] == 2
        assert summary["total_llm_calls"] == 3
        assert summary["total_tokens"] == 2500

        # 2. 按 run_kind 与机构级 regulator（NFRA 包含 EAST）筛选
        resp_ing = client.get("/api/runs/audit-overview", params={"run_kind": "INGESTION"})
        assert resp_ing.status_code == 200
        assert [r["run_id"] for r in resp_ing.json()["runs"]] == [ing_run_id]

        resp_nfra = client.get("/api/runs/audit-overview", params={"regulator": "NFRA"})
        assert resp_nfra.status_code == 200
        assert resp_nfra.json()["summary"]["total_runs"] == 2

        resp_pboc = client.get("/api/runs/audit-overview", params={"regulator": "PBOC"})
        assert resp_pboc.status_code == 200
        assert resp_pboc.json()["summary"]["total_runs"] == 0

        # 3. 单批次明细：哈希链、瀑布、门禁拦截、人工确权、自愈回边、指纹齐全
        detail_resp = client.get(f"/api/runs/{gen_run_id}/audit-detail")
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert detail["chain_valid"] is True
        assert detail["broken_seq"] is None
        assert len(detail["events"]) == 6
        assert all(ev["chain_ok"] is True for ev in detail["events"])
        assert len(detail["bus_rejections"]) == 1
        assert detail["bus_rejections"][0]["tool"] == "propose_rules"
        assert len(detail["human_actions"]) == 2
        assert len(detail["self_heals"]) == 1
        assert detail["self_heals"][0]["kind"] == "GO_REDIRECT"
        assert len(detail["trace"]["spans"]) >= 4
        assert detail["fingerprints"]["source_documents"][0]["sha256"] == "a" * 64
        assert detail["fingerprints"]["audit_report_blob"] == "blob://audit-demo"
    finally:
        app.dependency_overrides.pop(api_db_session, None)
        session.close()


def test_audit_detects_direct_database_tampering():
    session_factory = _make_threadsafe_session_factory()
    session = session_factory()
    try:
        _, gen_run_id = _seed_generation_run(session)
        client = _make_test_client(session_factory)

        # 模拟绕过应用层直接篡改数据库中的某条事件载荷（未重算 event_hash）
        target_ev = (
            session.query(EventModel)
            .filter(EventModel.run_id == gen_run_id, EventModel.kind == "HUMAN_DECISIONS_RECORDED")
            .first()
        )
        assert target_ev is not None
        target_ev.payload_json = {"approver": "伪造审批人", "decisions": []}
        session.commit()

        # 总览应立即报警 chain_broken_runs == 1
        ov = client.get("/api/runs/audit-overview").json()
        assert ov["summary"]["chain_broken_runs"] == 1
        assert ov["summary"]["chain_valid_runs"] == 0
        row = next(r for r in ov["runs"] if r["run_id"] == gen_run_id)
        assert row["chain_valid"] is False
        assert row["broken_seq"] == target_ev.seq

        # 明细应精准指出断裂序号及期望哈希
        detail = client.get(f"/api/runs/{gen_run_id}/audit-detail").json()
        assert detail["chain_valid"] is False
        assert detail["broken_seq"] == target_ev.seq
        tampered_row = next(ev for ev in detail["events"] if ev["seq"] == target_ev.seq)
        assert tampered_row["chain_ok"] is False
        assert tampered_row["expected_hash"] is not None
        assert tampered_row["expected_hash"] != tampered_row["event_hash"]
    finally:
        app.dependency_overrides.pop(api_db_session, None)
        session.close()
