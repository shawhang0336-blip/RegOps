"""Interrupt 协议测试。

ProdAgent 的 Interrupt 三要素：question / resume_node / pending_input，
外加 interrupt_id 让回答事件指回具体中断。
验证：落账完整、回答关联、幂等重入、多次中断可区分。
"""

import uuid

import pytest

from app.events import (
    append_event,
    interrupt_payload,
    latest_interrupt,
    matching_answer,
    pending_interrupt,
    project,
)
from app.models import EventModel, RunModel
from app.errors import WorkflowError
from .helpers import make_session


def _run(session):
    run = RunModel(id=str(uuid.uuid4()), spec_id=str(uuid.uuid4()))
    session.add(run)
    session.flush()
    append_event(session, run, "RUN_CREATED", "created", {})
    session.commit()
    return run


def test_interrupt_payload_has_three_elements():
    p = interrupt_payload(
        "APPROVAL_REQUIRED",
        "请批准或拒绝 3 条规则",
        {"pending_count": 3, "pending_rule_hashes": ["a", "b", "c"]},
        {"operator": "zhangsan"},
    )
    assert p["question"] == "请批准或拒绝 3 条规则"
    assert p["resume_node"] == "human_gate"
    assert p["pending_input"]["pending_count"] == 3
    assert p["interrupt_id"]
    assert p["operator"] == "zhangsan"


def test_interrupt_payload_rejects_unknown_kind():
    with pytest.raises(ValueError, match="未知中断类型"):
        interrupt_payload("MADE_UP", "q", {})


def test_pending_vs_latest_interrupt():
    session = make_session()
    run = _run(session)
    p1 = interrupt_payload("APPROVAL_REQUIRED", "第一次", {"n": 1})
    append_event(session, run, "APPROVAL_REQUIRED", "human_gate", p1)
    session.commit()

    assert pending_interrupt(session, run, "APPROVAL_REQUIRED").payload_json["question"] == "第一次"
    assert latest_interrupt(session, run, "APPROVAL_REQUIRED").payload_json["question"] == "第一次"

    # 回答后：pending 没了，latest 仍在（供幂等判定）
    append_event(session, run, "HUMAN_DECISIONS_RECORDED", "human_gate", {
        "interrupt_id": p1["interrupt_id"],
        "decisions": [{"rule_hash": "a", "decision": "APPROVED"}],
        "all_decided": True,
    })
    session.commit()
    assert pending_interrupt(session, run, "APPROVAL_REQUIRED") is None
    assert latest_interrupt(session, run, "APPROVAL_REQUIRED").payload_json["question"] == "第一次"


def test_matching_answer_by_fingerprint():
    session = make_session()
    run = _run(session)
    p = interrupt_payload("TEST_CONFIRMATION_REQUIRED", "确认结果", {})
    append_event(session, run, "TEST_CONFIRMATION_REQUIRED", "test_confirmation", p)
    append_event(session, run, "TEST_RESULTS_CONFIRMED", "test_confirmation", {
        "interrupt_id": p["interrupt_id"], "answer_fingerprint": "fp1", "approver": "a",
    })
    session.commit()

    assert matching_answer(session, run, "TEST_RESULTS_CONFIRMED", p["interrupt_id"], "fp1") is not None
    assert matching_answer(session, run, "TEST_RESULTS_CONFIRMED", p["interrupt_id"], "fp2") is None


def test_two_interrupts_are_distinguishable():
    """自愈回边会二次进入人工门，两次中断必须能区分。"""
    session = make_session()
    run = _run(session)
    p1 = interrupt_payload("APPROVAL_REQUIRED", "首轮确认", {"origin": "rule_binder"})
    append_event(session, run, "APPROVAL_REQUIRED", "human_gate", p1)
    append_event(session, run, "HUMAN_DECISIONS_RECORDED", "human_gate", {
        "interrupt_id": p1["interrupt_id"], "decisions": [], "all_decided": True,
        "answer_fingerprint": "fp1",
    })
    p2 = interrupt_payload("APPROVAL_REQUIRED", "自愈后确认", {"origin": "self_heal", "attempt": 2})
    append_event(session, run, "APPROVAL_REQUIRED", "human_gate", p2)
    session.commit()

    pending = pending_interrupt(session, run, "APPROVAL_REQUIRED")
    assert pending.payload_json["question"] == "自愈后确认"
    assert pending.payload_json["pending_input"]["origin"] == "self_heal"
    # 第一次的回答不会误配到第二次中断
    assert matching_answer(session, run, "HUMAN_DECISIONS_RECORDED", p2["interrupt_id"], "fp1") is None
