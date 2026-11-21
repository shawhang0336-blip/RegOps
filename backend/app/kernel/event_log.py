"""EventLog：ProdAgent 事件溯源账本微内核（唯一事实源）。

职责边界：
- RunModel 只保存身份（id/spec_id/时间戳）；
- 状态（RunState）由 append-only 的事件账本通过可插拔 StateReducer 纯函数折叠产生；
- 每条事件串联 SHA-256 哈希链（prev_hash -> event_hash），支持防篡改校验（verify_chain）；
- 提供通用 Interrupt 协议三要素构造与幂等重入查询原语；
- 机制层不硬编码任何业务事件名（业务折叠规则位于 app/policy/regops_plan.py）。
"""

import hashlib
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from app.infra.models import EventModel, RunModel
from .bus import global_bus

GENESIS_HASH = "0" * 64


_MYSQL_JSON_INT_MIN = -(1 << 63)
_MYSQL_JSON_INT_MAX = (1 << 64) - 1


def _normalize_event_value(value: Any) -> Any:
    """对齐 MySQL 8 JSON 列数值表示：超出 64 位整数范围的超大 int 在落库后会被 MySQL 转为 DOUBLE。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value < _MYSQL_JSON_INT_MIN or value > _MYSQL_JSON_INT_MAX:
            return float(value)
        return value
    if isinstance(value, dict):
        return {k: _normalize_event_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_event_value(v) for v in value]
    if isinstance(value, tuple):
        return [_normalize_event_value(v) for v in value]
    return value


def canonical_event_json(value: Any) -> str:
    """确定性 JSON 序列化（按 key 排序、紧凑分隔符），用于计算事件 SHA-256 哈希链。"""
    return json.dumps(_normalize_event_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class RunState:
    """Run 在某一事件序号处的状态。纯值对象，不落库。"""

    status: str = "NEW"
    current_node: str = "created"
    attempt: int = 0
    result_json: Optional[Dict[str, Any]] = None
    when: str = "always"
    pending_target: Optional[str] = None
    resume_from_node: str = "created"
    resume_when: str = "always"


StateReducer = Callable[[RunState, str, Dict[str, Any]], RunState]

_DEFAULT_REDUCER: StateReducer = lambda state, _kind, _payload: state


def register_default_reducer(reducer: StateReducer) -> None:
    """由策略层注册默认事件折叠器（如 RegOps 的 _apply）。"""
    global _DEFAULT_REDUCER
    _DEFAULT_REDUCER = reducer


def fold_with(events: Iterable[EventModel], reducer: Optional[StateReducer] = None) -> RunState:
    """按 seq 顺序折叠事件流，得到当前状态。纯函数，同一批事件永远得到同一结果。"""
    active_reducer = reducer or _DEFAULT_REDUCER
    state = RunState()
    for event in events:
        state = active_reducer(state, event.kind, event.payload_json)
    return state


def _state_cache(session: Session) -> Dict[str, RunState]:
    """session 级投影缓存。同一请求内多次读取不重复折叠；append_event 会失效它。"""
    return session.info.setdefault("_run_state_cache", {})


def project_with(
    session: Session,
    run: RunModel,
    reducer: Optional[StateReducer] = None,
) -> RunState:
    """查询并折叠某个 Run 的事件账本。所有状态读取的唯一权威入口。"""
    cache = _state_cache(session)
    if reducer is None and run.id in cache:
        return cache[run.id]
    events = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id)
        .order_by(EventModel.seq)
        .all()
    )
    state = fold_with(events, reducer=reducer)
    if reducer is None:
        cache[run.id] = state
    return state


def project_many_with(
    session: Session,
    runs: Iterable[RunModel],
    reducer: Optional[StateReducer] = None,
) -> Dict[str, RunState]:
    """批量折叠：一次 IN 查询取出所有事件，避免列表接口的 N+1。"""
    ids = [run.id for run in runs]
    if not ids:
        return {}
    cache = _state_cache(session)
    if reducer is None and all(run_id in cache for run_id in ids):
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
    states = {run_id: fold_with(grouped.get(run_id, []), reducer=reducer) for run_id in ids}
    if reducer is None:
        cache.update(states)
    return states


def compute_event_hash(
    run_id: str,
    kind: str,
    node: str,
    payload: Dict[str, Any],
    prev_hash: str,
) -> str:
    material = canonical_event_json(
        {
            "run_id": run_id,
            "kind": kind,
            "node": node,
            "payload": payload,
            "prev_hash": prev_hash,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def append_event(
    session: Session,
    run: RunModel,
    kind: str,
    node: str,
    payload: Dict[str, Any],
    bus: Any = None,
) -> EventModel:
    """追加一条不可变事件，并通过 Bus 广播 event.appended 生命周期信号。"""
    previous = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id)
        .order_by(EventModel.seq.desc())
        .first()
    )
    prev_hash = previous.event_hash if previous else GENESIS_HASH
    event_hash = compute_event_hash(run.id, kind, node, payload, prev_hash)
    event = EventModel(
        run_id=run.id,
        kind=kind,
        node=node,
        payload_json=payload,
        prev_hash=prev_hash,
        event_hash=event_hash,
    )
    session.add(event)
    run.updated_at = datetime.utcnow()
    session.flush()
    _state_cache(session).pop(run.id, None)

    active_bus = bus if bus is not None else global_bus()
    if hasattr(active_bus, "fire"):
        active_bus.fire(
            "event.appended",
            {
                "run_id": run.id,
                "seq": event.seq,
                "kind": kind,
                "node": node,
                "event_hash": event.event_hash,
            },
        )
        if kind == "RUN_CREATED":
            active_bus.fire("run.created", {"run_id": run.id, "spec_id": run.spec_id, "node": node})
        elif kind in {"RUN_RESUMED", "REAUDIT_REQUESTED"}:
            active_bus.fire("run.resumed", {"run_id": run.id, "kind": kind, "node": node})
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
        prev_hash = compute_event_hash(run.id, event.kind, event.node, event.payload_json, prev_hash)
        if event.event_hash != prev_hash:
            return False
    return True


# 兼容 re-export：中断协议已独立归位至 app.kernel.interrupt
from .interrupt import (  # noqa: E402, F401
    latest_interrupt,
    make_interrupt_payload,
    matching_answer,
    pending_interrupt_with,
)
