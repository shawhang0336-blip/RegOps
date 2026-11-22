"""Layer 6 delivery/ 深度审计回归测试。

覆盖全部 7 项生产交付与业务库只读连接器修复（MySQL 8 / Oracle 23 / 规则防篡改 / 幂等 / LOB / 大小写）。
"""

import hashlib
import io
import json
import uuid
import zipfile
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from app.delivery import business_db as business_db_mod
from app.delivery.business_db import BusinessDatabaseError, ReadOnlyBusinessDatabase
from app.delivery.production import (
    compile_production_mysql_ddl,
    compile_production_oracle_ddl,
    compile_production_rule_query,
    inline_sql_literals,
    sql_literal,
)
from app.delivery.production_service import build_production_bundle
from app.generation.core.binding import SchemaBinding, compile_schema_bindings
from app.generation.workflow.runtime import _upsert_rule
from app.infra.blob_store import LocalBlobStore
from app.infra.config import Settings
from app.infra.contracts import Evidence, FieldSpec, RuleDefinition, SpecDocument, canonical_json
from app.infra.db_tables import (
    ArtifactModel,
    CodeSetModel,
    CodeValueModel,
    FieldCodeReferenceModel,
    FieldMetadataModel,
    RuleModel,
    RunModel,
    SourceDocumentModel,
    SpecificationModel,
    TableMetadataModel,
)
from app.infra.errors import WorkflowError
from app.kernel.event_log import append_event
from .helpers import make_session


def _make_rule(table_code: str, key: str, field: str, op: str, params: dict, rationale: str = "test") -> RuleDefinition:
    return RuleDefinition(
        rule_key=key,
        table_code=table_code,
        field=field,
        operator=op,
        params=params,
        source="DOC_EXPLICIT",
        evidence=Evidence(section="6.1.3", page="1", field=field, quote="test"),
        rationale=rationale,
        rule_hash=uuid.uuid4().hex + uuid.uuid4().hex,
    )


def test_sql_literal_mysql_backslash_and_inline_preserves_quoted_colons():
    # MySQL 8 必须转义反斜杠，否则正则里的 \d / \. 会被 MySQL 字符串字面量吃掉
    assert sql_literal(r"^\d{4}\.\d{2}$", "mysql") == r"'^\\d{4}\\.\\d{2}$'"
    assert sql_literal(r"^\d{4}\.\d{2}$", "oracle") == r"'^\d{4}\.\d{2}$'"

    # 字符串字面量里出现 :mi / :ss 时绝不能被当成绑定参数误替换或报 Missing SQL parameter
    sql = "SELECT TO_CHAR(SYSDATE, 'HH24:MI:SS'), :compiled_rule_key FROM t WHERE a = ':not_a_param' AND b = :val"
    inlined = inline_sql_literals(sql, {"compiled_rule_key": "R1", "val": "OK"}, dialect="oracle")
    assert "'HH24:MI:SS'" in inlined
    assert "':not_a_param'" in inlined
    assert "'R1'" in inlined and "'OK'" in inlined


def test_production_ddl_respects_oracle23_and_mysql8_physical_limits():
    wide_schemas = [
        SchemaBinding(field=f"COL_{i:02d}", storage_type="TEXT", length=1000, decimal_places=0)
        for i in range(25)
    ] + [
        SchemaBinding(field="HUGE_NOTE", storage_type="TEXT", length=5000, decimal_places=0),
        SchemaBinding(field="BIG_NUM", storage_type="NUMBER", length=50, decimal_places=40),
    ]
    oracle_ddl = compile_production_oracle_ddl("T_WIDE", wide_schemas)
    assert "record_id" not in oracle_ddl
    assert '"HUGE_NOTE" CLOB NULL' in oracle_ddl
    assert '"BIG_NUM" NUMBER(38,38) NULL' in oracle_ddl

    mysql_ddl = compile_production_mysql_ddl("T_WIDE", wide_schemas)
    assert "record_id" not in mysql_ddl
    assert "`BIG_NUM` DECIMAL(50,30) NULL" in mysql_ddl
    # 25 * 1000 * 4 = 100,000 字节 > 60,000 预算，必须有若干列自动升格 TEXT 防 Error 1118
    assert mysql_ddl.count(" TEXT NULL") >= 10


def _seed_completed_run(session, tmp_path, *, with_code_set: bool = True, blob_only_schema: bool = False):
    spec = SpecDocument(
        version="V1",
        table_code="T_DELIV",
        table_name="交付测试表",
        source_name="spec.pdf",
        source_sha256="b" * 64,
        section="6.1",
        fields=[
            FieldSpec(name="TXCCY", content="币种", data_type="VARCHAR", length=3, check_text="必填，见币种码表", evidence_page="1"),
            FieldSpec(name="EMAIL", content="邮箱", data_type="VARCHAR", length=64, check_text="邮箱格式", evidence_page="1"),
            FieldSpec(name="REMARK", content="备注", data_type="VARCHAR", length=10, check_text="非必填", evidence_page="1"),
        ],
        terms_text="",
        dictionary_text="",
    )
    spec_id = str(uuid.uuid4())
    session.add(
        SpecificationModel(
            id=spec_id,
            version="V1",
            table_code="SNAP_DELIV",
            source_name=spec.source_name,
            source_sha256=spec.source_sha256,
            content_json={"selection_confirmed": True, "tables": [spec.model_dump()]},
        )
    )
    if with_code_set:
        doc = SourceDocumentModel(
            id=str(uuid.uuid4()),
            file_name=spec.source_name,
            file_type="PDF",
            sha256=spec.source_sha256,
            blob_uri=f"blob://{spec.source_sha256}",
            spec_version="V1",
            status="READY",
            block_count=1,
        )
        session.add(doc)
        session.flush()
        tbl = TableMetadataModel(
            id=str(uuid.uuid4()),
            document_id=doc.id,
            block_id=str(uuid.uuid4()),
            table_code=spec.table_code,
            table_name=spec.table_name,
            table_definition="",
            status="PUBLISHED",
            current_version=1,
            published_version=1,
        )
        session.add(tbl)
        session.flush()
        fm = FieldMetadataModel(
            id=str(uuid.uuid4()),
            table_id=tbl.id,
            field_cn="币种",
            field_en="TXCCY",
            field_definition="",
            check_text_raw="",
            data_type="VARCHAR",
            length=3,
            decimal_places=0,
            required=True,
            code_reference_text="CS_CCY",
            confidence=1.0,
            evidence_json={},
            sort_order=0,
            ai_value_json={},
        )
        cs = CodeSetModel(
            id=str(uuid.uuid4()),
            code="CS_CCY",
            name="币种代码表\n严禁随意删改",
            definition="",
            source_type="DOCUMENT_TABLE",
            status="PUBLISHED",
            current_version=1,
            published_version=1,
        )
        session.add_all([fm, cs])
        session.flush()
        for idx, (v, lbl) in enumerate([("CNY", "人民币"), ("USD", "美元")]):
            session.add(
                CodeValueModel(
                    id=str(uuid.uuid4()),
                    code_set_id=cs.id,
                    value=v,
                    label_cn=lbl,
                    label_en="",
                    definition="",
                    sort_order=idx,
                    enabled=True,
                    source_locator={},
                )
            )
        session.add(
            FieldCodeReferenceModel(
                id=str(uuid.uuid4()),
                field_id=fm.id,
                code_set_id=cs.id,
                reference_text="CS_CCY",
                status="BOUND",
                condition_json={},
                confidence=1.0,
            )
        )
    run = RunModel(id=str(uuid.uuid4()), spec_id=spec_id)
    session.add(run)
    session.flush()

    r_ccy = _make_rule("T_DELIV", "R_CCY_ENUM", "TXCCY", "ENUM", {"values": ["CNY", "USD"]}, rationale="币种白名单")
    r_mail = _make_rule(
        "T_DELIV",
        "R_EMAIL_RE",
        "EMAIL",
        "REGEXP",
        {"pattern": r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"},
        rationale="邮箱格式校验\n第二行绝不能逸出注释导致SQL语法报错",
    )
    r_len = _make_rule("T_DELIV", "R_REMARK_LEN", "REMARK", "LENGTH_MAX", {"length": 10}, rationale="备注最多10字符")
    rules = [r_ccy, r_mail, r_len]
    for r in rules:
        _upsert_rule(session, run, r, "APPROVED")

    blobs = LocalBlobStore(str(tmp_path / "blobs"))
    schema_tables = {"T_DELIV": [item.model_dump() for item in compile_schema_bindings(spec)]}
    schema_payload = (
        {"tables_blob": blobs.put(canonical_json(schema_tables).encode("utf-8"))}
        if blob_only_schema
        else {"tables": schema_tables}
    )
    append_event(session, run, "RUN_CREATED", "created", {})
    append_event(session, run, "SCHEMA_ASSIGNED", "schema_barrier", schema_payload)
    append_event(session, run, "AUDIT_STARTED", "oracle_executor", {"attempt": 1})
    append_event(
        session,
        run,
        "AUDIT_COMPLETED",
        "oracle_executor",
        {
            "result": {
                "active_rule_hashes": sorted(r.rule_hash for r in rules),
                "audit_summary": {
                    "all_passed": True,
                    "coverage_complete": True,
                    "code_set_versions": {"CS_CCY": 1} if with_code_set else {},
                },
            }
        },
    )
    append_event(session, run, "TEST_RESULTS_CONFIRMED", "test_confirmation", {"approver": "tester"})
    session.commit()
    return run, rules


def test_production_bundle_exports_full_mysql_and_oracle_scripts_checksums_and_is_idempotent(tmp_path):
    session = make_session()
    run, _ = _seed_completed_run(session, tmp_path, with_code_set=True, blob_only_schema=True)
    blob_dir = str(tmp_path / "blobs")

    _, content1 = build_production_bundle(session, run, blob_dir)
    _, content2 = build_production_bundle(session, run, blob_dir)
    # 重复下载幂等：只保留 1 条 ArtifactModel 记录
    bundle_rows = session.query(ArtifactModel).filter_by(run_id=run.id, artifact_type="PRODUCTION_BUNDLE", version=1).all()
    assert len(bundle_rows) == 1

    with zipfile.ZipFile(io.BytesIO(content2)) as zf:
        names = set(zf.namelist())
        expected_files = {
            "oracle/01_report_tables.sql",
            "oracle/02_code_set_tables.sql",
            "oracle/03_code_values_insert.sql",
            "oracle/04_code_values_merge.sql",
            "oracle/05_validation_rules.sql",
            "mysql/01_report_tables.sql",
            "mysql/02_code_set_tables.sql",
            "mysql/03_code_values_upsert.sql",
            "mysql/04_validation_rules.sql",
            "manifest.json",
            "rules.json",
        }
        assert expected_files.issubset(names)

        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["dialects"] == ["oracle", "mysql"]
        for path, expected_sha in manifest["file_checksums"].items():
            actual_sha = hashlib.sha256(zf.read(path)).hexdigest()
            assert actual_sha == expected_sha

        # 注释换行已清洗：绝不出现裸中文脱离 `-- ` 注释
        mysql_rules_sql = zf.read("mysql/04_validation_rules.sql").decode("utf-8")
        assert "\n第二行绝不能逸出注释" not in mysql_rules_sql
        assert "CHAR_LENGTH(" in mysql_rules_sql
        assert "REGEXP_LIKE(" in mysql_rules_sql
        assert r"\\." in mysql_rules_sql  # MySQL 反斜杠正确转义
        assert "NOT EXISTS (SELECT 1 FROM `REG_CS_" in mysql_rules_sql

        mysql_cs_ddl = zf.read("mysql/02_code_set_tables.sql").decode("utf-8")
        assert "\n严禁随意删改" not in mysql_cs_ddl
        assert "CREATE TABLE IF NOT EXISTS `REG_CS_" in mysql_cs_ddl
        assert "ON DUPLICATE KEY UPDATE" in zf.read("mysql/03_code_values_upsert.sql").decode("utf-8")


def test_production_bundle_blocks_post_audit_rule_tampering(tmp_path):
    session = make_session()
    run, _ = _seed_completed_run(session, tmp_path, with_code_set=False)

    # 模拟审计通过后有人手工篡改/删除一条 APPROVED 规则
    row = session.query(RuleModel).filter_by(run_id=run.id, rule_key="R_REMARK_LEN").one()
    row.status = "REJECTED"
    session.commit()

    with pytest.raises(WorkflowError, match="Approved rules changed after the audited run"):
        build_production_bundle(session, run, str(tmp_path / "blobs"))


def test_readonly_business_db_limit_lowercase_lob_and_readonly_tx(tmp_path, monkeypatch):
    # 1) SQLite 真实库验证：保留小写表名 + limit 上下界守卫
    db_file = tmp_path / "biz.db"
    eng = create_engine(f"sqlite:///{db_file}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE `safe_lower_tbl` (id INT, note TEXT)"))
        conn.execute(text("INSERT INTO `safe_lower_tbl` VALUES (1, 'ok'), (2, 'two')"))
    eng.dispose()

    settings = Settings(business_database_url=f"sqlite:///{db_file}", business_allowed_tables="safe_lower_tbl")
    db = ReadOnlyBusinessDatabase(settings)
    rows = db.preview("SAFE_LOWER_TBL", limit=1)
    assert rows == [{"id": 1, "note": "ok"}]

    with pytest.raises(BusinessDatabaseError, match="between 1 and 500"):
        db.preview("safe_lower_tbl", limit=0)
    with pytest.raises(BusinessDatabaseError, match="between 1 and 500"):
        db.preview("safe_lower_tbl", limit=501)

    # 2) 模拟 Oracle 23 连接：验证 SET TRANSACTION READ ONLY、rollback、以及连接关闭前读取 CLOB + bytes
    executed = []
    rolled_back = {"called": False}

    class _FakeOracleConn:
        dialect = SimpleNamespace(name="oracle")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, stmt, params=None):
            executed.append(str(stmt))
            if "SELECT" in str(stmt):
                lob = SimpleNamespace(read=lambda: "oracle-clob-content")
                return SimpleNamespace(
                    mappings=lambda: SimpleNamespace(all=lambda: [{"ID": 1, "CLOB_COL": lob, "RAW_COL": b"raw-bytes"}])
                )
            return SimpleNamespace()

        def rollback(self):
            rolled_back["called"] = True

    class _FakeEngine:
        def connect(self):
            return _FakeOracleConn()

        def dispose(self):
            pass

    monkeypatch.setattr(business_db_mod, "create_engine", lambda *a, **kw: _FakeEngine())
    ora_db = ReadOnlyBusinessDatabase(
        Settings(business_database_url="oracle+oracledb://mock", business_allowed_tables="SAFE_6_1")
    )
    ora_rows = ora_db.preview("safe_6_1", limit=10)
    assert ora_rows == [{"ID": 1, "CLOB_COL": "oracle-clob-content", "RAW_COL": "raw-bytes"}]
    assert "SET TRANSACTION READ ONLY" in executed[0]
    assert 'SELECT * FROM "SAFE_6_1" WHERE ROWNUM <= :row_limit' in executed[1]
    assert rolled_back["called"] is True
