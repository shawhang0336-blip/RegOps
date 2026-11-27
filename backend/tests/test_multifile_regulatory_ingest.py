"""多文件异构监管规范摄取与融合全流程回归测试（覆盖 SAFE / 一表通 2.0 / EAST 5.0 / 1104）。"""

import io
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.api.catalog import packages_service
from app.api.catalog.documents_service import ingest_uploaded_file
from app.api.common import db_session
from app.catalog.extraction import (
    extract_parallel_for_blocks,
    reconcile_package_assets,
)
from app.catalog.tables.mutations import publish_table
from app.catalog.tables.service import metadata_to_spec
from app.generation.core.planning import needs_semantic_rule_binding
from app.infra.config import Settings
from app.infra.contracts import FieldSpec, SpecDocument
from app.infra.db import Base
from app.infra.db_tables import (
    CodeSetModel,
    MetadataValidationModel,
    RawBlockModel,
    SourceDocumentModel,
    TableMetadataModel,
    TableRuleEvidenceModel,
)
from app.ingestion.parsers.extractors import (
    convert_legacy_office_bytes,
    extract_docx,
    extract_pdf,
    extract_xlsx,
    unpack_zip_archive,
)
from app.main import app

from .multifile_sample_builder import (
    build_1104_bundle,
    build_east5_zip_bundle,
    build_safe_codeset_xlsx,
    build_safe_word_docx,
    build_ybt_excel_dict_xlsx,
    build_ybt_rules_xlsx,
    build_ybt_word_docx,
)


def _wal_factory(tmp_path: Path, name: str):
    db_file = tmp_path / f"{name}.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return engine, factory


def test_word_excel_zip_heterogeneous_parsers(tmp_path):
    """验证 Word 合并表头/表后口径、Excel 双模式公式/单 Sheet 多表、ZIP 防穿越与 1104 矩阵识别。"""
    # 1) SAFE Word：修订表被忽略、BOP_A 横跨 banner + 两级表头 + 表后校验口径被完整绑定
    safe_docx = tmp_path / "SAFE_涉外收入接口规范.docx"
    safe_docx.write_bytes(build_safe_word_docx())
    _, docx_blocks = extract_docx(safe_docx)
    tbl_blocks = [b for b in docx_blocks if b["block_type"] == "DOCX_TABLE"]
    assert len(tbl_blocks) == 1
    bop = tbl_blocks[0]
    assert bop["content_json"].get("table_code") == "BOP_A"
    assert "长度_总长" in bop["content_json"]["headers"]
    assert "长度_小数位" in bop["content_json"]["headers"]
    assert "CUSTYPE=C" in bop["content_json"]["context_after"]
    assert "前 12 位为金融机构标识码" in bop["raw_content"]

    # 2) SAFE Excel：单 Sheet 内两张独立码表完整拆分为 2 个 CODE_SET 块
    safe_xlsx = tmp_path / "SAFE_公共代码表.xlsx"
    safe_xlsx.write_bytes(build_safe_codeset_xlsx())
    _, cs_blocks = extract_xlsx(safe_xlsx)
    assert len(cs_blocks) == 2
    assert all(b["content_json"].get("candidate_role") == "CODE_SET" for b in cs_blocks)
    assert {len(b["content_json"]["code_set_spec"]["values"]) for b in cs_blocks} == {3, 2}

    # 3) 1104：表样被精准识别为 MATRIX_REPORT（不进建表候选），公式与校验公式表完整留痕
    bundle_1104 = build_1104_bundle()
    mat_path = tmp_path / "1104_G01_表样模板.xlsx"
    mat_path.write_bytes(bundle_1104["1104_G01_表样模板.xlsx"])
    _, mat_blocks = extract_xlsx(mat_path)
    assert len(mat_blocks) == 1
    assert mat_blocks[0]["block_type"] == "MATRIX_REPORT"
    assert mat_blocks[0]["content_json"]["eligible"] is False
    assert mat_blocks[0]["content_json"]["matrix_profile"]["indicator_count"] >= 3

    chk_path = tmp_path / "1104_校验关系表.xlsx"
    chk_path.write_bytes(bundle_1104["1104_校验关系表.xlsx"])
    _, chk_blocks = extract_xlsx(chk_path)
    assert len(chk_blocks) == 1
    assert chk_blocks[0]["content_json"]["candidate_role"] == "RULE_EVIDENCE"
    assert len(chk_blocks[0]["content_json"]["rule_rows"]) == 2
    assert chk_blocks[0]["content_json"]["rule_rows"][1]["scope"] == "CROSS_TABLE"

    # 4) ZIP 安全拆包与 Zip Slip 防御
    unpacked = unpack_zip_archive(build_east5_zip_bundle())
    assert len(unpacked) == 3

    bad_zip = io.BytesIO()
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("../../evil.xlsx", b"PK")
    with pytest.raises(ValueError, match="Zip Slip"):
        unpack_zip_archive(bad_zip.getvalue())

    with pytest.raises(ValueError, match="LibreOffice"):
        convert_legacy_office_bytes("old.doc", b"dummy")

    # 5) 真实银行填报表单 Excel 回归：横向合并横幅去重、非字典表单降级为 NARRATIVE（不误建表报错）、交易代码文件角色识别
    from openpyxl import Workbook as _Wb
    from app.ingestion.parsers import infer_document_role

    wb_form = _Wb()
    ws_f = wb_form.active
    ws_f.title = "企业申请表"
    ws_f.append(["电子商业汇票系统用户建立申请表格", None, None, None])
    ws_f.merge_cells("A1:D1")
    ws_f.append(["开户行名称", "测试银行分行", None, None])
    ws_f.append(["公司名称", "", None, None])
    form_xlsx = tmp_path / "bank_form.xlsx"
    wb_form.save(form_xlsx)
    _, form_blocks = extract_xlsx(form_xlsx)
    assert len(form_blocks) == 1
    assert form_blocks[0]["content_json"]["candidate_role"] == "NARRATIVE"
    assert form_blocks[0]["content_json"]["eligible"] is False
    assert form_blocks[0]["content_json"]["sheet_notes"].count("电子商业汇票系统用户建立申请表格") == 1
    assert infer_document_role("SAFE_涉外收支交易代码_支出.pdf", []) == "CODE_SET_APPENDIX"


def test_real_safe_pdfs_parse_when_present():
    """直接对 data/03_国家外汇局_SAFE/ 下真实 SAFE PDF（含主采集规范与 2024 核查规则）执行离线解析抽块验证。"""
    specs_dir = (
        Path(__file__).resolve().parents[2]
        / "data"
        / "03_国家外汇局_SAFE"
        / "03_对外金融资产负债及交易统计"
    )
    if not specs_dir.exists():
        pytest.skip("data/03_国家外汇局_SAFE/03_对外金融资产负债及交易统计 not present")
    rule_pdf = specs_dir / "对外金融资产负债及交易统计核查规则_2024年版.pdf"
    if rule_pdf.exists():
        _, rule_blocks = extract_pdf(rule_pdf)
        assert len(rule_blocks) > 0
        assert any(
            b["block_type"] == "RULE_EVIDENCE" or b["content_json"].get("candidate_role") == "RULE_EVIDENCE"
            for b in rule_blocks
        )


def test_multifile_ybt_package_reconcile_isolation_and_step2_bridge(tmp_path, monkeypatch):
    """端到端验证 一表通 2.0 多文件规范包（Word+Excel+Rules）、跨文件合并冲突、包级码表隔离与 Step 2 规则喂入。"""
    engine, SessionFactory = _wal_factory(tmp_path, "pkg_e2e")
    settings = Settings(blob_dir=str(tmp_path / "blobs"), llm_provider="scripted", llm_api_key="")
    monkeypatch.setattr("app.catalog.extraction.SessionLocal", SessionFactory)
    monkeypatch.setattr("app.catalog.tables.service.SessionLocal", SessionFactory)
    monkeypatch.setattr("app.catalog.code_sets.service.SessionLocal", SessionFactory)
    monkeypatch.setattr("app.api.catalog.documents_service.SessionLocal", SessionFactory)
    monkeypatch.setattr("app.ingestion.structure_service.SessionLocal", SessionFactory)
    monkeypatch.setattr("app.api.catalog.packages_service.SessionLocal", SessionFactory)
    monkeypatch.setattr("app.api.catalog.packages_service.get_settings", lambda: settings)

    # 离线模式让结构体检快速无 AI，保留真实解析切块
    def _override_db():
        with SessionFactory() as s:
            yield s

    app.dependency_overrides[db_session] = _override_db
    try:
        client = TestClient(app)

        # 1) 创建 一表通 2.0 规范包 + 上传 Word 口径 + Excel 字典 + Excel 检核规则
        r_pkg = client.post("/api/packages", json={"name": "一表通2.0试点包", "regulator": "YBT", "spec_version": "2.0"})
        assert r_pkg.status_code == 201
        ybt_pkg_id = r_pkg.json()["id"]

        files_payload = [
            ("files", ("一表通2.0_业务与填报口径规范.docx", build_ybt_word_docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
            ("files", ("一表通2.0_数据字典与代码表.xlsx", build_ybt_excel_dict_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
            ("files", ("一表通2.0_可信区检核规则清单.xlsx", build_ybt_rules_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ]
        with SessionFactory() as s:
            res_up = packages_service.upload_files_to_package(
                s,
                ybt_pkg_id,
                [(name, data) for _, (name, data, _) in files_payload],
                prepare=False,
                session_factory=SessionFactory,
                get_settings_fn=lambda: settings,
            )
            assert len(res_up["uploaded"]) == 3
            from app.api.catalog.documents_service import store_initial_blocks

            for d in res_up["uploaded"]:
                store_initial_blocks(d["id"], session_factory=SessionFactory, get_settings_fn=lambda: settings)

        # 验证 Doc Role 自动识别准确
        r_detail = client.get(f"/api/packages/{ybt_pkg_id}")
        assert r_detail.status_code == 200
        role_by_name = {d["file_name"]: d["doc_role"] for d in r_detail.json()["documents"]}
        assert role_by_name["一表通2.0_业务与填报口径规范.docx"] == "MAIN_SPEC"
        assert role_by_name["一表通2.0_数据字典与代码表.xlsx"] == "SCHEMA_APPENDIX"
        assert role_by_name["一表通2.0_可信区检核规则清单.xlsx"] == "RULE_SUPPLEMENT"

        # 2) 包级三轨提取（绕过 LLM 架构分析，直接全块抽取）+ 跨文件融合
        with SessionFactory() as s:
            doc_ids = [
                r[0]
                for r in s.query(SourceDocumentModel.id).filter(SourceDocumentModel.package_id == ybt_pkg_id).all()
            ]
            b_ids = [
                r[0]
                for r in s.query(RawBlockModel.id)
                .filter(RawBlockModel.document_id.in_(doc_ids))
                .order_by(RawBlockModel.document_id, RawBlockModel.ordinal)
                .all()
            ]
        ext_res = extract_parallel_for_blocks(b_ids, settings, targets=["metadata", "code_sets", "rule_evidence"])
        assert ext_res["rule_evidence_count"] == 3

        with SessionFactory() as s:
            rec = reconcile_package_assets(s, ybt_pkg_id)
            s.commit()
            assert rec["merged_groups"] >= 1
            # Word(VARCHAR 10) vs Excel(VARCHAR 4) 制造的 CUST_TYPE 冲突必须被明确捕获且置为 NEEDS_REVIEW
            assert rec["conflict_count"] == 1
            assert any(c["field_en"] == "CUST_TYPE" and c["code"] == "CROSS_FILE_CONFLICT" for c in rec["conflicts"])

            # 验证 YBT_CUST_BASE 融合了双方字段（含 Excel 的 USCC 与 Word 的 COUNTRY_CD），旧副表标 MERGED
            r_tbls = client.get(f"/api/metadata/tables?package_id={ybt_pkg_id}")
            assert r_tbls.status_code == 200
            tbl_codes = sorted(t["table_code"] for t in r_tbls.json())
            assert tbl_codes == ["YBT_CUST_BASE", "YBT_LOAN_RECEIPT", "YBT_PROD_INFO"]

            cust_row = next(t for t in r_tbls.json() if t["table_code"] == "YBT_CUST_BASE")
            assert cust_row["status"] == "NEEDS_REVIEW"
            r_td = client.get(f"/api/metadata/tables/{cust_row['id']}")
            td_json = r_td.json()
            field_ens = {f["field_en"] for f in td_json["fields"]}
            assert {"CUST_ID", "CUST_NAME", "CUST_TYPE", "COUNTRY_CD", "USCC"} <= field_ens
            assert td_json["rule_evidence_count"] >= 2  # 单表 YBT_CUST_001 + 跨表 YBT_LOAN_002

            # 将 YBT_LOAN_RECEIPT 发布并转为 Step 2 SpecDocument，验证 supplementary_rules 打通至 Step 2 规则门控
            loan_row = next(t for t in r_tbls.json() if t["table_code"] == "YBT_LOAN_RECEIPT")
            loan_orm = s.get(TableMetadataModel, loan_row["id"])
            publish_table(s, loan_orm, "tester", loan_orm.current_version)
            s.commit()
            spec = metadata_to_spec(s, loan_orm)
            assert len(spec.supplementary_rules) >= 2
            assert any(sr["rule_code"] == "YBT_LOAN_002" for sr in spec.supplementary_rules)

            # 即便一个 VARCHAR 字段本身 check_text 为空，只要命中 supplementary_rules，Step 2 门控也必须放行 AI 生成规则
            probe_field = FieldSpec(name="CUSTOM_TAG", content="自定义标签", data_type="VARCHAR", length=32, check_text="", evidence_page="1")
            probe_spec = SpecDocument(
                version="2.0",
                table_code="T_DEMO",
                table_name="测试表",
                source_name="demo.xlsx",
                source_sha256="abc",
                section="1",
                fields=[probe_field],
                terms_text="",
                dictionary_text="",
                supplementary_rules=[{"rule_code": "R1", "target_fields": ["CUSTOM_TAG"], "rule_text": "CUSTOM_TAG 必须符合规范"}],
            )
            need_ai, reason = needs_semantic_rule_binding(probe_spec, probe_field)
            assert need_ai is True
            assert "补充证据" in reason

        # 3) 验证 EAST 5.0 ZIP 包与多项目包级码表彻底隔离（YBT 包和 EAST 包同名码表互不串库）
        r_east = client.post("/api/packages", json={"name": "EAST5.0包", "regulator": "EAST", "spec_version": "5.0"})
        east_pkg_id = r_east.json()["id"]
        with SessionFactory() as s:
            res_east = packages_service.upload_files_to_package(
                s,
                east_pkg_id,
                [("EAST5.0_标准规范包.zip", build_east5_zip_bundle())],
                prepare=False,
                session_factory=SessionFactory,
                get_settings_fn=lambda: settings,
            )
            assert len(res_east["uploaded"]) == 3
            from app.api.catalog.documents_service import store_initial_blocks

            for d in res_east["uploaded"]:
                store_initial_blocks(d["id"], session_factory=SessionFactory, get_settings_fn=lambda: settings)

            east_b_ids = [
                r[0]
                for r in s.query(RawBlockModel.id)
                .filter(RawBlockModel.document_id.in_([d["id"] for d in res_east["uploaded"]]))
                .all()
            ]
        extract_parallel_for_blocks(east_b_ids, settings, targets=["metadata", "code_sets", "rule_evidence"])
        with SessionFactory() as s:
            reconcile_package_assets(s, east_pkg_id)
            s.commit()

        ybt_codes = {cs["name"] for cs in client.get(f"/api/code-sets?package_id={ybt_pkg_id}").json()}
        east_codes = {cs["name"] for cs in client.get(f"/api/code-sets?package_id={east_pkg_id}").json()}
        assert "一表通客户类型代码表" in ybt_codes
        assert "一表通客户类型代码表" not in east_codes
        assert "证件类型代码表" in east_codes
        assert "证件类型代码表" not in ybt_codes

        # 4) 验证 1104 包识别为 MATRIX_REPORT + 校验关系入库，且不误建明细关系表
        r_1104 = client.post("/api/packages", json={"name": "1104包", "regulator": "1104", "spec_version": "251"})
        pkg_1104_id = r_1104.json()["id"]
        with SessionFactory() as s:
            res_1104 = packages_service.upload_files_to_package(
                s,
                pkg_1104_id,
                list(build_1104_bundle().items()),
                prepare=False,
                session_factory=SessionFactory,
                get_settings_fn=lambda: settings,
            )
            from app.api.catalog.documents_service import store_initial_blocks

            for d in res_1104["uploaded"]:
                store_initial_blocks(d["id"], session_factory=SessionFactory, get_settings_fn=lambda: settings)

        r_1104_detail = client.get(f"/api/packages/{pkg_1104_id}")
        assert r_1104_detail.json()["matrix_report_count"] == 1
        assert r_1104_detail.json()["rule_evidence_count"] == 2
        assert client.get(f"/api/metadata/tables?package_id={pkg_1104_id}").json() == []

        # 5) 验证包级版本 Diff API (/api/packages/diff)
        r_diff = client.get(f"/api/packages/diff?base_id={ybt_pkg_id}&target_id={east_pkg_id}")
        assert r_diff.status_code == 200
        assert r_diff.json()["summary"]["rule_evidences"]["base"] >= 3
        assert r_diff.json()["summary"]["rule_evidences"]["target"] >= 1

        # 6) 验证自定义监管项目、包内单文件替换(PUT)/移除(DELETE)、以及 Step 2 /artifacts?package_id= 筛选
        r_cust = client.post("/api/packages", json={"name": "上海自贸区监测_2025", "regulator": "上海自贸区", "spec_version": "2025"})
        assert r_cust.status_code == 201
        assert r_cust.json()["regulator"] == "上海自贸区"
        cust_pkg_id = r_cust.json()["id"]

        r_up_one = client.post(
            f"/api/packages/{cust_pkg_id}/files",
            files={"files": ("ftz_v1.xlsx", build_ybt_rules_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            data={"doc_role": "RULE_SUPPLEMENT", "prepare": "false"},
        )
        assert r_up_one.status_code == 201
        old_doc_id = r_up_one.json()["uploaded"][0]["id"]

        r_rep = client.put(
            f"/api/packages/{cust_pkg_id}/files/{old_doc_id}",
            files={"file": ("ftz_v2.xlsx", build_safe_codeset_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            data={"prepare": "false"},
        )
        assert r_rep.status_code == 200
        new_doc_id = r_rep.json()["document"]["id"]
        assert new_doc_id != old_doc_id
        assert r_rep.json()["document"]["doc_role"] == "RULE_SUPPLEMENT"  # 继承旧文件角色

        r_del_file = client.delete(f"/api/packages/{cust_pkg_id}/files/{new_doc_id}")
        assert r_del_file.status_code == 200
        assert r_del_file.json()["package"]["document_count"] == 0

        # 验证 /api/artifacts 与 /api/artifacts/rules 支持按 package_id 精准隔离
        r_art_ybt = client.get(f"/api/artifacts?package_id={ybt_pkg_id}")
        assert r_art_ybt.status_code == 200
        art_codes = {t["table_code"] for t in r_art_ybt.json()["tables"]}
        assert art_codes == {"YBT_CUST_BASE", "YBT_LOAN_RECEIPT", "YBT_PROD_INFO"}
        assert all(t["regulator"] == "YBT" and t["package_id"] == ybt_pkg_id for t in r_art_ybt.json()["tables"])

        r_art_east = client.get(f"/api/artifacts?package_id={east_pkg_id}")
        assert r_art_east.status_code == 200
        assert {t["table_code"] for t in r_art_east.json()["tables"]} == {"A_CORP_BASIC", "S_LOAN_JJ"}

        r_rules_ybt = client.get(f"/api/artifacts/rules?package_id={ybt_pkg_id}")
        assert r_rules_ybt.status_code == 200

        # 验证 /api/runs 支持按 package_id 与 regulator 过滤
        r_runs_ybt = client.get(f"/api/runs?package_id={ybt_pkg_id}&regulator=YBT")
        assert r_runs_ybt.status_code == 200
        assert isinstance(r_runs_ybt.json(), list)
    finally:
        app.dependency_overrides.pop(db_session, None)
        engine.dispose()


def test_alembic_0007_migration_runs_cleanly(tmp_path):
    """验证 Alembic 迁移 0007 可从旧库升级并正确创建 spec_packages / table_rule_evidences 与新列。"""
    from alembic import command
    from alembic.config import Config

    db_file = tmp_path / "mig0007.db"
    backend_dir = Path(__file__).resolve().parents[1]
    cfg = Config(str(backend_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_dir / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")

    command.upgrade(cfg, "0006_mysql_column_widths")
    command.upgrade(cfg, "0007_spec_packages")

    engine = create_engine(f"sqlite:///{db_file}")
    try:
        with engine.connect() as conn:
            tables = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
            assert {"spec_packages", "table_rule_evidences"} <= tables
            doc_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(source_documents)")).fetchall()}
            assert {"package_id", "doc_role"} <= doc_cols
    finally:
        engine.dispose()
