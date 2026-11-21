"""Channel & RunContext：ProdAgent 跨节点带合并语义（Reducer）的命名数据通道与运行时上下文微内核。

职责边界：
- ChannelSpec 定义每个共享数据通道的名称与合并函数（Reducer：last_value / merge_dict / append_list）；
- RunContext 是单次调度的运行时容器：承载只读 state 投影、shared 数据通道、Identity 审计身份与 Bus 总线；
- 节点之间不互相调用，只通过 Outcome.shared 与 Scheduler 屏障处的 ctx.put(key, value) 协作；
- 机制层不硬编码任何业务通道名（具体通道与 EventLog 恢复逻辑位于 app/policy/regops_channels.py）。
"""

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .blob_store import LocalBlobStore
from .bus import Bus
from .event_log import RunState, project_with

Reducer = Callable[[Any, Any], Any]


def last_value(_old: Any, new: Any) -> Any:
    """覆盖型 Reducer：取最新写入值。"""
    return new


def merge_dict(old: Any, new: Any) -> Any:
    """字典合并型 Reducer：按 key 合并多波次或多表产出；传入 None 时保留旧值。"""
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


def resolve_event_tables(
    payload: Optional[Dict[str, Any]],
    blob_store: Optional[LocalBlobStore] = None,
) -> Dict[str, Any]:
    """从事件 payload 取出 tables 字典；若已卸载到 BlobStore（tables_blob），支持按指针透明读取。"""
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
    session: Any
    run: Any
    settings: Any
    state: RunState
    identity: Identity = field(default_factory=Identity)
    shared: Dict[str, Any] = field(default_factory=dict)
    channels: Dict[str, ChannelSpec] = field(default_factory=dict)
    bus: Bus = field(default_factory=Bus)
    llm: Any = None
    budget: Optional[Dict[str, Any]] = None
    concurrency: int = 4
    attempt_base: Optional[int] = None
    current_node: str = "created"
    pending_events: list = field(default_factory=list)
    projector: Optional[Callable[[Any, Any], RunState]] = None
    cancellation: Any = None
    budget_guard: Any = None

    def check_cancelled(self) -> None:
        """在节点/波次边界检查取消令牌；若已取消则抛出 RunCancelledError。"""
        if self.cancellation is not None and hasattr(self.cancellation, "raise_if_cancelled"):
            self.cancellation.raise_if_cancelled()

    def check_budget(self) -> None:
        """在节点/波次边界检查成本预算守卫；若超限则抛出 BudgetExceededError。"""
        if self.budget_guard is not None and hasattr(self.budget_guard, "check"):
            self.budget_guard.check(self.bus)
        elif hasattr(self.bus, "check_budget"):
            self.bus.check_budget()

    @property
    def is_cancelled(self) -> bool:
        return bool(self.cancellation is not None and getattr(self.cancellation, "is_cancelled", False))

    def put(self, key: str, value: Any) -> None:
        """通过 Channel Reducer 往共享通道写一个命名产物。"""
        spec = self.channels.get(key)
        if spec is not None:
            self.shared[key] = spec.reducer(self.shared.get(key), value)
        else:
            self.shared[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.shared.get(key, default)

    def refresh_state(self) -> RunState:
        """从事件账本重新折叠状态（append_event 之后调用）。"""
        if self.projector is not None:
            self.state = self.projector(self.session, self.run)
        else:
            self.state = project_with(self.session, self.run)
        return self.state

    def with_actor(self, **kwargs) -> Identity:
        """入口处设置操作人身份。"""
        for key, value in kwargs.items():
            if hasattr(self.identity, key) and value:
                setattr(self.identity, key, value)
        return self.identity
