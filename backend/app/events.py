"""EventLog 是 Run 状态的唯一事实源。

RunModel 只保存身份（id/spec_id/时间戳）。状态（status/current_node/attempt/
result_json）由 append-only 的事件账本折叠产生，任何时刻都能通过 project() 重算。
不存在第二份可写状态，也不再需要"事件投影 vs 存储投影"的一致性自检。
"""

import hashlib
import uuid
from datetime import datetime
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from .domain import canonical_json
from .models import EventModel, RunModel
from .workflow import DEFAULT_PLAN

GENESIS_HASH = "0" * 64

# 中断类型 → 恢复节点（与 Plan 节点名严格一致）
INTERRUPT_RESUME_NODES = {
    "APPROVAL_REQUIRED": "human_gate",
    "TEST_CONFIRMATION_REQUIRED": "test_confirmation",
}


def interrupt_payload(
    kind: str,
    question: str,
    pending_input: Dict[str, Any],
    identity: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构造一条 Interrupt 事件的 payload。

    ProdAgent 的 Interrupt 三要素全部落账：
      - question：问人什么（人可读，不再硬编码在前端）
      - resume_node：回答后从哪个 Plan 节点恢复
      - pending_input：挂起时节点的输入摘要，供审计与恢复核对
      - interrupt_id：本次中断的唯一标识，回答事件用 interrupt_seq 指回它
    """
    if kind not in INTERRUPT_RESUME_NODES:
        raise ValueError(f"未知中断类型：{kind}")
    payload: Dict[str, Any] = {
        "question": question,
        "resume_node": INTERRUPT_RESUME_NODES[kind],
        "pending_input": pending_input,
        "interrupt_id": uuid.uuid4().hex,
    }
    if identity:
        payload.update(identity)
    return payload


def matching_answer(
    session: Session,
    run: RunModel,
    answer_kind: str,
    interrupt_id: str,
    answer_fingerprint: str,
) -> Optional[EventModel]:
    """同一中断 + 同样的回答内容 → 认为是一次幂等重入，直接返回原回答事件。"""
    for event in (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id, EventModel.kind == answer_kind)
        .order_by(EventModel.seq)
        .all()
    ):
        payload = event.payload_json or {}
        if payload.get("interrupt_id") == interrupt_id and payload.get("answer_fingerprint") == answer_fingerprint:
            return event
    return None


def latest_interrupt(session: Session, run: RunModel, kind: str) -> Optional[EventModel]:
    """最近一条指定类型的中断事件，不论是否已回答。用于幂等重入判定。"""
    return (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id, EventModel.kind == kind)
        .order_by(EventModel.seq.desc())
        .first()
    )


def pending_interrupt(session: Session, run: RunModel, kind: str) -> Optional[EventModel]:
    """找最近一条尚未被回答的指定类型中断事件。"""
    answers = {"APPROVAL_REQUIRED": "HUMAN_DECISIONS_RECORDED", "TEST_CONFIRMATION_REQUIRED": "TEST_RESULTS_CONFIRMED"}
    answer_kind = answers.get(kind)
    if not answer_kind:
        return None
    answered_ids = {
        (event.payload_json or {}).get("interrupt_id")
        for event in session.query(EventModel)
        .filter(EventModel.run_id == run.id, EventModel.kind == answer_kind)
        .all()
    }
    for event in (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id, EventModel.kind == kind)
        .order_by(EventModel.seq.desc())
        .all()
    ):
        interrupt_id = (event.payload_json or {}).get("interrupt_id")
        if interrupt_id and interrupt_id not in answered_ids:
            return event
    return None


@dataclass(frozen=True)
class RunState:
    """Run 在某一事件序号处的状态。纯值对象，不落库。"""

    status: str = "NEW"
    current_node: str = "created"
    attempt: int = 0
    result_json: Optional[Dict[str, Any]] = None


# 事件 → Plan 节点推进的条件标签。拓扑后继由 DEFAULT_PLAN.next() 查得，
# 这里只声明"发生了哪类条件"，节点名不再散落在状态机里硬编码。
_EVENT_CONDITIONS = {
    "RULES_BOUND": "rules_bound",
    "HUMAN_DECISIONS_RECORDED": "human_decided",
}


def _apply(state: RunState, kind: str, payload: Dict[str, Any]) -> RunState:
    """把一条事件折进状态。

    current_node 表示"最近完成的阶段节点"。执行器读它 + status，
    再查 DEFAULT_PLAN 的后继边决定下一步——拓扑只存在于 Plan 里。
    """
    if kind == "RUN_CREATED":
        return replace(state, status="RUNNING", current_node="created")
    if kind == "GENERATION_PLANNED":
        return replace(state, current_node="generation_planner")
    if kind == "SCHEMA_ASSIGNED":
        return replace(state, current_node="schema_mapper")
    if kind == "RULES_BOUND":
        pending = bool(payload.get("pending_count"))
        return replace(
            state,
            status="WAITING_APPROVAL" if pending else "READY_TO_AUDIT",
            current_node="rule_binder",
        )
    if kind == "ORACLE_ASSETS_PREPARED":
        return replace(state, current_node="oracle_asset_builder", result_json=payload.get("result"))
    if kind == "APPROVAL_REQUIRED":
        return replace(state, status="WAITING_APPROVAL", current_node="human_gate")
    if kind == "HUMAN_DECISIONS_RECORDED":
        decided = bool(payload.get("all_decided"))
        return replace(
            state,
            status="READY_TO_AUDIT" if decided else "WAITING_APPROVAL",
            current_node="human_gate",
        )
    if kind == "REAUDIT_REQUESTED":
        # 人工改参后只重跑造数与验证，规则集合不动；执行器从 data_generator_binder 起
        return replace(state, status="READY_TO_AUDIT", current_node="human_gate")
    if kind == "GENERATORS_ASSIGNED":
        return replace(state, current_node="data_generator_binder")
    if kind == "AUDIT_CASES_ASSIGNED":
        return replace(state, current_node="audit_case_binder")
    if kind == "TEST_CONFIRMATION_REQUIRED":
        return replace(state, status="WAITING_TEST_APPROVAL", current_node="oracle_executor")
    if kind == "TEST_RESULTS_CONFIRMED":
        return replace(state, status="COMPLETED", current_node="test_confirmation")
    if kind == "AUDIT_STARTED":
        return replace(
            state,
            status="AUDITING",
            current_node="oracle_executor",
            attempt=payload.get("attempt", state.attempt),
        )
    if kind == "GO_REDIRECT":
        target = payload["target"]
        if not any(node.name == target for node in DEFAULT_PLAN.nodes):
            raise ValueError(f"GO_REDIRECT 目标 {target} 不在 Plan 中")
        return replace(state, status="RUNNING", current_node=target)
    if kind == "AUDIT_COMPLETED":
        return replace(
            state, status="COMPLETED", current_node="oracle_executor", result_json=payload.get("result")
        )
    if kind in {"RUN_FAILED", "AUDIT_REJECTED"}:
        return replace(
            state,
            status="FAILED",
            current_node=payload.get("node", state.current_node),
            result_json=payload.get("result"),
        )
    return state

def fold(events: Iterable[EventModel]) -> RunState:
    """按 seq 顺序折叠事件流，得到当前状态。纯函数，同一批事件永远得到同一结果。"""
    state = RunState()
    for event in events:
        state = _apply(state, event.kind, event.payload_json)
    return state


def _state_cache(session: Session) -> Dict[str, RunState]:
    """session 级投影缓存。同一请求内多次读取不重复折叠；append_event 会失效它。"""
    return session.info.setdefault("_run_state_cache", {})


def project(session: Session, run: RunModel) -> RunState:
    """查询并折叠某个 Run 的事件账本。所有状态读取的唯一权威入口。"""
    cache = _state_cache(session)
    if run.id in cache:
        return cache[run.id]
    events = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id)
        .order_by(EventModel.seq)
        .all()
    )
    state = fold(events)
    cache[run.id] = state
    return state


def project_many(session: Session, runs: Iterable[RunModel]) -> Dict[str, RunState]:
    """批量折叠：一次 IN 查询取出所有事件，避免列表接口的 N+1。"""
    ids = [run.id for run in runs]
    if not ids:
        return {}
    cache = _state_cache(session)
    if all(run_id in cache for run_id in ids):
        return {run_id: cache[run_id] for run_id in ids}
    grouped: Dict[str, List[EventModel]] = defaultdict(list)
    rows = (
        session.query(EventModel)
        .filter(EventModel.run_id.in_(ids))
        .order_by(EventModel.run_id, EventModel.seq)
        .all()
    )
    for event in rows:
        grouped[event.run_id].append(event)
    states = {run_id: fold(grouped.get(run_id, [])) for run_id in ids}
    cache.update(states)
    return states


def append_event(
    session: Session,
    run: RunModel,
    kind: str,
    node: str,
    payload: Dict[str, Any],
) -> EventModel:
    """追加一条不可变事件。这是 Run 状态变化的唯一入口。"""
    previous = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id)
        .order_by(EventModel.seq.desc())
        .first()
    )
    prev_hash = previous.event_hash if previous else GENESIS_HASH
    material = canonical_json(
        {
            "run_id": run.id,
            "kind": kind,
            "node": node,
            "payload": payload,
            "prev_hash": prev_hash,
        }
    )
    event = EventModel(
        run_id=run.id,
        kind=kind,
        node=node,
        payload_json=payload,
        prev_hash=prev_hash,
        event_hash=hashlib.sha256(material.encode("utf-8")).hexdigest(),
    )
    session.add(event)
    run.updated_at = datetime.utcnow()
    session.flush()
    _state_cache(session).pop(run.id, None)
    return event


def verify_chain(session: Session, run: RunModel) -> bool:
    """校验事件账本的哈希链完整性（防篡改 / 检测手工删改）。"""
    events = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id)
        .order_by(EventModel.seq)
        .all()
    )
    prev_hash = GENESIS_HASH
    for event in events:
        if event.prev_hash != prev_hash:
            return False
        material = canonical_json(
            {
                "run_id": run.id,
                "kind": event.kind,
                "node": event.node,
                "payload": event.payload_json,
                "prev_hash": prev_hash,
            }
        )
        prev_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
        if event.event_hash != prev_hash:
            return False
    return True
