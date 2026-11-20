"""EventLog 是 Run 状态的唯一事实源。

RunModel 只保存身份（id/spec_id/时间戳）。状态（status/current_node/attempt/
result_json）由 append-only 的事件账本折叠产生，任何时刻都能通过 project() 重算。
不存在第二份可写状态，也不再需要"事件投影 vs 存储投影"的一致性自检。
"""

import hashlib
from datetime import datetime
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from .domain import canonical_json
from .models import EventModel, RunModel

GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class RunState:
    """Run 在某一事件序号处的状态。纯值对象，不落库。"""

    status: str = "NEW"
    current_node: str = "created"
    attempt: int = 0
    result_json: Optional[Dict[str, Any]] = None


def _apply(state: RunState, kind: str, payload: Dict[str, Any]) -> RunState:
    if kind == "RUN_CREATED":
        return replace(state, status="RUNNING", current_node="bind_rules")
    if kind == "RULES_BOUND":
        pending = bool(payload.get("pending_count"))
        return replace(
            state,
            status="WAITING_APPROVAL" if pending else "READY_TO_AUDIT",
            current_node="human_gate" if pending else "build_audit",
        )
    if kind == "APPROVAL_REQUIRED":
        return replace(state, status="WAITING_APPROVAL", current_node="human_gate")
    if kind == "HUMAN_DECISIONS_RECORDED":
        decided = bool(payload.get("all_decided"))
        return replace(
            state,
            status="READY_TO_AUDIT" if decided else "WAITING_APPROVAL",
            current_node="build_audit" if decided else "human_gate",
        )
    if kind == "REAUDIT_REQUESTED":
        # 人工改参后只重跑造数与 Oracle 验证，规则集合保持不动
        return replace(state, status="READY_TO_AUDIT", current_node="build_audit")
    if kind == "ORACLE_ASSETS_PREPARED":
        return replace(state, result_json=payload.get("result"))
    if kind == "TEST_CONFIRMATION_REQUIRED":
        return replace(state, status="WAITING_TEST_APPROVAL", current_node="test_confirmation")
    if kind == "TEST_RESULTS_CONFIRMED":
        return replace(state, status="COMPLETED", current_node="export")
    if kind == "AUDIT_STARTED":
        return replace(
            state,
            status="AUDITING",
            current_node="sandbox_audit",
            attempt=payload.get("attempt", state.attempt),
        )
    if kind == "GO_REDIRECT":
        return replace(state, status="RUNNING", current_node=payload["target"])
    if kind == "AUDIT_COMPLETED":
        return replace(state, status="COMPLETED", current_node="export", result_json=payload.get("result"))
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
