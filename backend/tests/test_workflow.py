import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Base
from app.events import fold_events
from app.models import EventModel, RunModel, SpecificationModel
from app.workflow import record_decisions, start_run


def make_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def test_approval_resume_blob_export_and_replay(organization_spec, tmp_path):
    session = make_session()
    spec_id = str(uuid.uuid4())
    session.add(
        SpecificationModel(
            id=spec_id,
            version=organization_spec.version,
            table_code=organization_spec.table_code,
            source_name=organization_spec.source_name,
            source_sha256=organization_spec.source_sha256,
            content_json=organization_spec.model_dump(),
        )
    )
    session.commit()
    settings = Settings(blob_dir=str(tmp_path / "blobs"))

    run = start_run(session, spec_id, "scripted", settings)
    assert run.status == "WAITING_APPROVAL"
    pending = [rule for rule in run_view_rules(session, run.id) if rule.status == "PENDING"]
    assert len(pending) == 1

    run = record_decisions(
        session,
        run,
        "测试审批员",
        {pending[0].rule_hash: "APPROVED"},
        settings,
        "scripted",
    )
    assert run.status == "COMPLETED"
    assert run.result_json["audit_summary"]["all_passed"] is True
    assert run.result_json["audit_summary"]["active_rule_count"] == 13
    for key in ("ddl_blob", "validation_sql_blob", "audit_report_blob"):
        assert (tmp_path / "blobs" / run.result_json[key][7:9] / run.result_json[key][7:]).is_file()

    events = session.query(EventModel).filter(EventModel.run_id == run.id).order_by(EventModel.seq).all()
    replayed = RunModel(id=run.id, spec_id=spec_id, status="NEW", current_node="created")
    fold_events(replayed, events)
    assert replayed.status == run.status
    assert replayed.current_node == run.current_node
    assert replayed.result_json == run.result_json


def run_view_rules(session, run_id):
    from app.models import RuleModel

    return session.query(RuleModel).filter(RuleModel.run_id == run_id).all()
