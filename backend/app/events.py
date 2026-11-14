import hashlib
from datetime import datetime
from typing import Any, Dict, Iterable

from sqlalchemy.orm import Session

from .domain import canonical_json
from .models import EventModel, RunModel

GENESIS_HASH = "0" * 64


def apply(run: RunModel, kind: str, payload: Dict[str, Any]) -> None:
    if kind == "RUN_CREATED":
        run.status = "RUNNING"
        run.current_node = "bind_rules"
    elif kind == "RULES_BOUND":
        run.status = "WAITING_APPROVAL" if payload.get("pending_count") else "READY_TO_AUDIT"
        run.current_node = "human_gate" if payload.get("pending_count") else "build_audit"
    elif kind == "APPROVAL_REQUIRED":
        run.status = "WAITING_APPROVAL"
        run.current_node = "human_gate"
    elif kind == "HUMAN_DECISIONS_RECORDED":
        run.status = "READY_TO_AUDIT" if payload.get("all_decided") else "WAITING_APPROVAL"
        run.current_node = "build_audit" if payload.get("all_decided") else "human_gate"
    elif kind == "AUDIT_STARTED":
        run.status = "AUDITING"
        run.current_node = "sandbox_audit"
        run.attempt = payload.get("attempt", run.attempt)
    elif kind == "GO_REDIRECT":
        run.status = "RUNNING"
        run.current_node = payload["target"]
    elif kind == "AUDIT_COMPLETED":
        run.status = "COMPLETED"
        run.current_node = "export"
        run.result_json = payload.get("result")
    elif kind in {"RUN_FAILED", "AUDIT_REJECTED"}:
        run.status = "FAILED"
        run.current_node = payload.get("node", run.current_node)
        run.result_json = payload.get("result")
    run.updated_at = datetime.utcnow()


def append_event(
    session: Session,
    run: RunModel,
    kind: str,
    node: str,
    payload: Dict[str, Any],
) -> EventModel:
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
    apply(run, kind, payload)
    session.flush()
    return event


def fold_events(run: RunModel, events: Iterable[EventModel]) -> RunModel:
    run.status = "NEW"
    run.current_node = "created"
    run.attempt = 0
    run.result_json = None
    for event in events:
        apply(run, event.kind, event.payload_json)
    return run
