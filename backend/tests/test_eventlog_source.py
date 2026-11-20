"""EventLog 唯一事实源的回归测试。

验证三件事：
1. Run 状态完全由事件折叠产生，不依赖任何存储列；
2. 事件投影可重放（同一事件流任意次折叠结果一致）；
3. 哈希链能检测出事件被手工篡改。
"""

import uuid

from app.events import append_event, fold, project, verify_chain
from app.models import EventModel, RunModel
from .helpers import make_session


def _new_run(session):
    run = RunModel(id=str(uuid.uuid4()), spec_id=str(uuid.uuid4()))
    session.add(run)
    session.flush()
    return run


def test_state_is_folded_from_events_only():
    session = make_session()
    run = _new_run(session)

    assert project(session, run).status == "NEW"

    append_event(session, run, "RUN_CREATED", "table_confirmation", {})
    assert project(session, run).status == "RUNNING"
    assert project(session, run).current_node == "bind_rules"

    append_event(session, run, "RULES_BOUND", "rule_binder", {"pending_count": 2})
    state = project(session, run)
    assert state.status == "WAITING_APPROVAL"
    assert state.current_node == "human_gate"

    append_event(
        session,
        run,
        "HUMAN_DECISIONS_RECORDED",
        "human_gate",
        {"all_decided": True, "decisions": []},
    )
    assert project(session, run).status == "READY_TO_AUDIT"

    append_event(session, run, "AUDIT_STARTED", "oracle_executor", {"attempt": 1})
    assert project(session, run).attempt == 1
    assert project(session, run).status == "AUDITING"

    append_event(session, run, "AUDIT_COMPLETED", "oracle_executor", {"result": {"ok": True}})
    state = project(session, run)
    assert state.status == "COMPLETED"
    assert state.result_json == {"ok": True}


def test_projection_is_replayable():
    session = make_session()
    run = _new_run(session)
    append_event(session, run, "RUN_CREATED", "table_confirmation", {})
    append_event(session, run, "RULES_BOUND", "rule_binder", {"pending_count": 0})
    append_event(session, run, "AUDIT_COMPLETED", "oracle_executor", {"result": {"n": 1}})

    events = session.query(EventModel).all()
    for _ in range(3):
        assert fold(events) == project(session, run)


def test_hash_chain_detects_tampering():
    session = make_session()
    run = _new_run(session)
    append_event(session, run, "RUN_CREATED", "table_confirmation", {})
    append_event(session, run, "RULES_BOUND", "rule_binder", {"pending_count": 1})
    session.commit()

    assert verify_chain(session, run) is True

    first = session.query(EventModel).order_by(EventModel.seq).first()
    first.payload_json = {"tampered": True}
    session.commit()
    session.expire_all()

    assert verify_chain(session, run) is False
