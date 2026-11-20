"""RunContext：一次执行的运行时上下文（ProdAgent 的 ctx 与 Channel）。

Node body 的签名是 `body(input, ctx) -> Outcome`：
  - input 是本节点的输入（上一步的产出或 Plan 里的初始输入）；
  - ctx 携带这一轮执行的一切运行时设施——状态投影、带 Reducer 的命名数据通道（Channel）、
    操作人身份、Bus 门禁、以及 settings/llm/budget/session 这些依赖。

设计边界：
  - state 是 EventLog 的折叠投影，节点**只读**，不直接改；
  - shared 是命名 Channel，节点把中间产物写进 Outcome.shared，由 Scheduler 在屏障处按 Channel Reducer 合并；
  - build_context 启动或恢复时，自动从 EventLog + BlobStore 折叠恢复全部 Channel 快照；
  - identity 记录 operator/approver/editor，随事件 payload 落账，可追溯；
  - 节点之间不互相调用，只通过 ctx.shared 与 Scheduler 协作。
"""

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from sqlalchemy.orm import Session

from .blobs import LocalBlobStore
from .bus import Bus, default_bus
from .config import Settings
from .domain import RuleDefinition
from .events import RunState, project
from .generic_domain import AuditCaseBinding
from .models import EventModel, RuleModel, RunModel

Reducer = Callable[[Any, Any], Any]


def last_value(_old: Any, new: Any) -> Any:
    """覆盖型 Reducer：取最新写入值。"""
    return new


def merge_dict(old: Any, new: Any) -> Any:
    """字典合并型 Reducer：按 key 合并多波次或多表产出；传入 None 时保留空或新值。"""
    if new is None:
        return old
    if not isinstance(old, dict) or not isinstance(new, dict):
        return new
    return {**old, **new}


def append_list(old: Any, new: Any) -> list:
    """列表追加型 Reducer：累积多波次列表产物。"""
    base = list(old) if isinstance(old, list) else []
    if isinstance(new, list):
        return base + new
    return base + [new]


@dataclass(frozen=True)
class ChannelSpec:
    name: str
    reducer: Reducer = last_value


DEFAULT_CHANNELS: Dict[str, ChannelSpec] = {
    "plans": ChannelSpec("plans", merge_dict),
    "schema_payload": ChannelSpec("schema_payload", merge_dict),
    "golden_overrides": ChannelSpec("golden_overrides", merge_dict),
    "generator_payload": ChannelSpec("generator_payload", merge_dict),
    "golden_records": ChannelSpec("golden_records", merge_dict),
    "audit_cases": ChannelSpec("audit_cases", merge_dict),
    "use_plan": ChannelSpec("use_plan", last_value),
    "pending_count": ChannelSpec("pending_count", last_value),
    "rules": ChannelSpec("rules", last_value),
    "prepared_assets": ChannelSpec("prepared_assets", last_value),
    "report": ChannelSpec("report", last_value),
    "result": ChannelSpec("result", last_value),
    "attempt": ChannelSpec("attempt", last_value),
    "first_attempt": ChannelSpec("first_attempt", last_value),
    "self_heal_report": ChannelSpec("self_heal_report", last_value),
    "approval_origin": ChannelSpec("approval_origin", last_value),
}


def resolve_event_tables(payload: Optional[Dict[str, Any]], blob_store: Optional[LocalBlobStore] = None) -> Dict[str, Any]:
    """从事件 payload 取出 tables 字典；若已卸载到 BlobStore（tables_blob），优先读取或透明回退。"""
    if not isinstance(payload, dict):
        return {}
    tables = payload.get("tables")
    if isinstance(tables, dict) and tables:
        return tables
    blob_uri = payload.get("tables_blob")
    if blob_uri and blob_store is not None:
        try:
            loaded = json.loads(blob_store.get(blob_uri).decode("utf-8"))
            if isinstance(loaded, dict):
                return loaded
        except Exception:
            pass
    return tables if isinstance(tables, dict) else {}


@dataclass
class Identity:
    """操作人身份：所有写操作的审计归属。"""

    operator: str = "UNSPECIFIED_OPERATOR"
    approver: Optional[str] = None
    editor: Optional[str] = None

    def as_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"operator": self.operator}
        if self.approver:
            payload["approver"] = self.approver
        if self.editor:
            payload["editor"] = self.editor
        return payload


@dataclass
class RunContext:
    session: Session
    run: RunModel
    settings: Settings
    state: RunState
    identity: Identity = field(default_factory=Identity)
    shared: Dict[str, Any] = field(default_factory=dict)
    channels: Dict[str, ChannelSpec] = field(default_factory=lambda: dict(DEFAULT_CHANNELS))
    bus: Bus = field(default_factory=default_bus)
    llm: Any = None
    budget: Optional[Dict[str, Any]] = None
    concurrency: int = 4
    attempt_base: Optional[int] = None
    current_node: str = "created"

    # 每个节点执行后累计的写集，屏障处统一落账
    pending_events: list = field(default_factory=list)

    def put(self, key: str, value: Any) -> None:
        """通过 Channel Reducer 往共享通道写一个命名产物。"""
        spec = self.channels.get(key)
        if spec is not None and key in self.shared:
            self.shared[key] = spec.reducer(self.shared.get(key), value)
        else:
            self.shared[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.shared.get(key, default)

    def refresh_state(self) -> RunState:
        """从事件账本重新折叠状态（append_event 之后调用）。"""
        self.state = project(self.session, self.run)
        return self.state

    def with_actor(self, **kwargs) -> Identity:
        """入口处设置操作人身份。"""
        for key, value in kwargs.items():
            if hasattr(self.identity, key) and value:
                setattr(self.identity, key, value)
        return self.identity


def recover_channels(session: Session, run: RunModel, settings: Any = None) -> Dict[str, Any]:
    """从 EventLog + BlobStore 折叠恢复跨节点 Channel 状态。

    任何时刻中断（人工门等待、进程重启、网络超时），新建 RunContext 都能
    无损还原当时已产出的 plans / schema_payload / prepared_assets / golden_records / audit_cases / self_heal_report。
    """
    blob_dir = getattr(settings, "blob_dir", None)
    blob_store = LocalBlobStore(blob_dir) if isinstance(blob_dir, str) and blob_dir else None

    shared: Dict[str, Any] = {
        "plans": {},
        "schema_payload": {},
        "golden_overrides": {},
    }
    events = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id)
        .order_by(EventModel.seq)
        .all()
    )
    for event in events:
        kind = event.kind
        payload = event.payload_json or {}
        if kind == "GENERATION_PLANNED":
            shared["plans"] = merge_dict(shared.get("plans"), resolve_event_tables(payload, blob_store))
            shared["use_plan"] = True
        elif kind == "SCHEMA_ASSIGNED":
            shared["schema_payload"] = merge_dict(shared.get("schema_payload"), resolve_event_tables(payload, blob_store))
        elif kind == "CROSS_RULES_ASSIGNED":
            shared["golden_overrides"] = merge_dict(
                shared.get("golden_overrides"), payload.get("golden_overrides") or {}
            )
        elif kind == "RULES_BOUND":
            shared["pending_count"] = int(payload.get("pending_count") or 0)
            shared["approval_origin"] = payload.get("origin") or "rule_binder"
            shared.pop("self_heal_report", None)
        elif kind == "ORACLE_ASSETS_PREPARED":
            if payload.get("assets") is not None:
                shared["prepared_assets"] = payload["assets"]
        elif kind == "GENERATORS_ASSIGNED":
            tables = resolve_event_tables(payload, blob_store)
            shared["generator_payload"] = merge_dict(shared.get("generator_payload"), tables)
            golden = {
                code: item.get("golden_record", {})
                for code, item in tables.items()
                if isinstance(item, dict) and "golden_record" in item
            }
            shared["golden_records"] = merge_dict(shared.get("golden_records"), golden)
        elif kind == "AUDIT_CASES_ASSIGNED":
            tables = resolve_event_tables(payload, blob_store)
            typed_cases = {}
            for code, items in tables.items():
                if isinstance(items, list):
                    try:
                        typed_cases[code] = [AuditCaseBinding.model_validate(item) for item in items]
                    except Exception:
                        pass
            if typed_cases:
                shared["audit_cases"] = merge_dict(shared.get("audit_cases"), typed_cases)
        elif kind == "GO_REDIRECT":
            shared["approval_origin"] = payload.get("origin") or "self_heal"
            report_uri = (payload.get("failed_result") or {}).get("audit_report_blob") or payload.get("audit_report_blob")
            if report_uri and blob_store is not None:
                try:
                    shared["self_heal_report"] = json.loads(blob_store.get(report_uri).decode("utf-8"))
                except Exception:
                    pass

    approved_rows = (
        session.query(RuleModel)
        .filter(RuleModel.run_id == run.id, RuleModel.status == "APPROVED")
        .all()
    )
    if approved_rows:
        try:
            shared["rules"] = [RuleDefinition.model_validate(row.definition_json) for row in approved_rows]
        except Exception:
            pass
    return shared


def build_context(
    session: Session,
    run: RunModel,
    settings: Settings,
    llm=None,
    budget=None,
    concurrency: int = 4,
    operator: str = "UNSPECIFIED_OPERATOR",
    approver: Optional[str] = None,
    editor: Optional[str] = None,
    attempt_base: Optional[int] = None,
) -> RunContext:
    """从入口参数组装 RunContext，并从事件账本自动折叠恢复 Channel 快照。"""
    state = project(session, run)
    shared = recover_channels(session, run, settings)
    return RunContext(
        session=session,
        run=run,
        settings=settings,
        state=state,
        identity=Identity(operator=operator, approver=approver, editor=editor),
        shared=shared,
        bus=default_bus(),
        llm=llm,
        budget=budget,
        concurrency=concurrency,
        attempt_base=attempt_base,
        current_node=state.current_node,
    )
