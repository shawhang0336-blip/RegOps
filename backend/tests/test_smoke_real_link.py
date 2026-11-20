"""真实链路冒烟测试：需要真实的 GLM 与 Oracle 沙箱，默认跳过。

常规 pytest 会 collect 这个用例但自动 skip，不消耗 token、不连 Oracle。

手动运行（会真实调用模型 + 真实建 Oracle 表，消耗额度）：

    RUN_REAL_SMOKE=1 \\
    LLM_API_KEY=<key> \\
    ORACLE_SANDBOX_URL='oracle+oracledb://system:<pwd>@127.0.0.1:1521/?service_name=FREEPDB1' \\
    pytest tests/test_smoke_real_link.py -v -s

可选环境变量：LLM_MODEL（默认 glm-4-plus）、LLM_TIMEOUT（默认 120）。

注意：测试会在 Oracle 沙箱创建 DQ_* 临时表，结束后不自动删除，按需手工清理。
"""

import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.asset_models  # noqa: F401  确保所有 ORM 模型注册到 Base.metadata
from app.events import project
from app.config import Settings
from app.db import Base
from app.generic_workflow import approve_generic_rules, confirm_generic_test_results, start_generic_run
from app.models import RuleModel, SpecificationModel

pytestmark = pytest.mark.smoke

RUN_REAL_SMOKE = os.environ.get("RUN_REAL_SMOKE") == "1"


@pytest.mark.skipif(
    not RUN_REAL_SMOKE,
    reason="set RUN_REAL_SMOKE=1 (needs real GLM + Oracle) to run the real-link smoke test",
)
def test_real_link_organization_end_to_end(organization_spec, tmp_path):
    assert os.environ.get("LLM_API_KEY"), "LLM_API_KEY is required for the real-link smoke test"
    assert os.environ.get("ORACLE_SANDBOX_URL"), "ORACLE_SANDBOX_URL is required for the real-link smoke test"

    settings = Settings(
        oracle_sandbox_url=os.environ["ORACLE_SANDBOX_URL"],
        llm_api_key=os.environ["LLM_API_KEY"],
        llm_model=os.environ.get("LLM_MODEL", "glm-4-plus"),
        llm_timeout=int(os.environ.get("LLM_TIMEOUT", "120")),
        blob_dir=str(tmp_path / "blobs"),
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    with session_factory() as session:
        spec_id = str(uuid.uuid4())
        session.add(
            SpecificationModel(
                id=spec_id,
                version=organization_spec.version,
                table_code=organization_spec.table_code,
                source_name=organization_spec.source_name,
                source_sha256=organization_spec.source_sha256,
                content_json={
                    "table_name": organization_spec.table_name,
                    "selection_confirmed": True,
                    "tables": [organization_spec.model_dump()],
                },
            )
        )
        session.commit()

        # 阶段2：plan -> schema -> rules -> Oracle 建表 + 空跑，停在规则审批 gate
        run = start_generic_run(session, spec_id, settings)
        assert project(session, run).status == "WAITING_APPROVAL", project(session, run).result_json

        pending = [
            rule
            for rule in session.query(RuleModel).filter(RuleModel.run_id == run.id).all()
            if rule.status == "PENDING"
        ]
        assert pending, "expected AI-expanded rules pending approval"

        # 阶段3：审批 -> 生成测试数据 -> 审计用例 -> Oracle 真实测试
        run = approve_generic_rules(
            session, run, "smoke-approver", {rule.rule_hash: "APPROVED" for rule in pending}, settings
        )
        assert project(session, run).status == "WAITING_TEST_APPROVAL", project(session, run).result_json

        summary = project(session, run).result_json["audit_summary"]
        assert summary["all_passed"] is True, summary
        assert summary["false_positive_count"] == 0, summary
        assert summary["false_negative_count"] == 0, summary

        # 测试确认 -> 完成，可导出生产部署包
        run = confirm_generic_test_results(session, run, "smoke-approver")
        assert project(session, run).status == "COMPLETED"
