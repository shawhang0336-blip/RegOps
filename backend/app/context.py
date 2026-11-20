"""RunContext：一次执行的运行时上下文（ProdAgent 的 ctx）。

Node body 的签名是 `body(input, ctx) -> Outcome`：
  - input 是本节点的输入（上一步的产出或 Plan 里的初始输入）；
  - ctx 携带这一轮执行的一切运行时设施——状态投影、共享数据通道、
    操作人身份、Bus 门禁、以及 settings/llm/budget/session 这些依赖。

设计边界：
  - state 是 EventLog 的折叠投影，节点**只读**，不直接改；
  - shared 是命名 Channel，节点把中间产物写进来，供后续节点读；
    大对象（造数/审计报告）只存 BlobStore 指针，不放这里；
  - identity 记录 operator/approver/editor，随事件 payload 落账，可追溯；
  - 节点之间不互相调用，只通过 ctx.shared 与 Scheduler 协作。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from .bus import Bus, default_bus
from .config import Settings
from .events import RunState, project
from .models import RunModel


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
    bus: Bus = field(default_factory=default_bus)
    llm: Any = None
    budget: Optional[Dict[str, Any]] = None
    concurrency: int = 4
    attempt_base: Optional[int] = None

    # 每个节点执行后累计的写集，屏障处统一落账
    pending_events: list = field(default_factory=list)

    def put(self, key: str, value: Any) -> None:
        """往共享 Channel 写一个命名产物。"""
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
    """从入口参数组装 RunContext。身份缺省时用占位符，后续接入登录只改这里。"""
    state = project(session, run)
    return RunContext(
        session=session,
        run=run,
        settings=settings,
        state=state,
        identity=Identity(operator=operator, approver=approver, editor=editor),
        bus=default_bus(),
        llm=llm,
        budget=budget,
        concurrency=concurrency,
        attempt_base=attempt_base,
    )
