"""Bus：ProdAgent 全生命周期事件总线微内核（事前门禁裁决 check/adjudicate + 事后广播 fire/subscribe）。

机制层严守零业务硬编码：
1. 事前门禁裁决（check / adjudicate）：
   模型或节点准备执行动作前先过 Bus 征询已注册的 Adjudicator；任一裁决器返回拒绝原因即物理拦截，
   同时触发 `tool.rejected` 广播并在屏障处由 Scheduler 统一排空落 `BUS_REJECTED` 事件。
2. 全生命周期事后广播（fire / subscribe）：
   支持精确主题与 `*` 通配订阅，支持子总线（Run 级 `ctx.bus`）向父总线（`global_bus`）自动冒泡，
   且对旁观订阅者（Observer/Subscriber）做严格的异常隔离，绝不影响主流程状态机推进。
"""

import threading
import time
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

Adjudicator = Callable[[str, Dict[str, Any], Dict[str, Any]], Optional[str]]
Subscriber = Callable[[Dict[str, Any]], None]

LIFECYCLE_TOPICS = (
    "run.created",
    "run.resumed",
    "run.interrupted",
    "run.completed",
    "run.failed",
    "run.cancelled",
    "node.enter",
    "node.exit",
    "node.redirect",
    "node.error",
    "wave.fanout",
    "wave.barrier",
    "tool.before",
    "tool.after",
    "tool.rejected",
    "budget.exceeded",
    "event.appended",
    "document.progress",
)

MAX_BUS_HISTORY = 2000


class Bus:
    """全生命周期总线内核：事前门禁裁决（adjudicate/check）+ 事后主题广播（fire/subscribe）。"""

    def __init__(self, parent: Optional["Bus"] = None) -> None:
        self._adjudicators: List[Adjudicator] = []
        self._observers: List[Subscriber] = []
        self._subscribers: Dict[str, List[Subscriber]] = defaultdict(list)
        self._rejections: List[Dict[str, Any]] = []
        self._history: List[Dict[str, Any]] = []
        self._seq: int = 0
        self._parent: Optional["Bus"] = parent
        self._lock = threading.Lock()
        self._budget_guard: Any = None

    def attach_budget(self, guard: Any) -> "Bus":
        """挂载横切预算守卫（BudgetGuard），供 LLM 守卫与调度器在关键节点做熔断检查。"""
        with self._lock:
            self._budget_guard = guard
        return self

    @property
    def budget_guard(self) -> Any:
        with self._lock:
            return self._budget_guard

    def check_budget(self) -> None:
        """触发当前总线挂载的预算检查；未挂载时为空操作。"""
        guard = self.budget_guard
        if guard is not None and hasattr(guard, "check"):
            guard.check(self)

    def add_adjudicator(self, fn: Adjudicator) -> "Bus":
        with self._lock:
            if fn not in self._adjudicators:
                self._adjudicators.append(fn)
        return self

    def add_observer(self, fn: Subscriber) -> "Bus":
        """注册门禁拦截旁观者（兼容旧接口；等价于订阅 tool.rejected）。"""
        with self._lock:
            self._observers.append(fn)
        return self

    def subscribe(self, topic: str, fn: Subscriber) -> "Bus":
        """订阅特定生命周期主题（如 'node.enter'、'tool.rejected'、'event.appended'）或 '*' 通配全部主题。"""
        with self._lock:
            self._subscribers[topic].append(fn)
        return self

    def unsubscribe(self, topic: str, fn: Subscriber) -> "Bus":
        with self._lock:
            handlers = self._subscribers.get(topic, [])
            if fn in handlers:
                handlers.remove(fn)
        return self

    def fire(self, topic: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """向外广播一条生命周期事件。旁观者抛出的任何异常均被吞掉隔离，绝不阻断主流程。"""
        with self._lock:
            self._seq += 1
            envelope: Dict[str, Any] = {
                "seq": self._seq,
                "topic": topic,
                "timestamp": time.time(),
                **(payload or {}),
            }
            self._history.append(envelope)
            if len(self._history) > MAX_BUS_HISTORY:
                del self._history[:-MAX_BUS_HISTORY]
            topic_handlers = list(self._subscribers.get(topic, []))
            wildcard_handlers = list(self._subscribers.get("*", []))
            parent = self._parent

        for handler in topic_handlers + wildcard_handlers:
            try:
                handler(envelope)
            except Exception:
                # 旁观者只看不拦：任何订阅者异常不得影响主流程
                pass

        if parent is not None:
            parent._forward(envelope)
        return envelope

    def _forward(self, envelope: Dict[str, Any]) -> None:
        """子 Bus（如单次 Run 的 ctx.bus）向父 Bus 冒泡广播。"""
        topic = envelope.get("topic", "")
        with self._lock:
            self._seq += 1
            forwarded = {**envelope, "global_seq": self._seq}
            self._history.append(forwarded)
            if len(self._history) > MAX_BUS_HISTORY:
                del self._history[:-MAX_BUS_HISTORY]
            topic_handlers = list(self._subscribers.get(topic, []))
            wildcard_handlers = list(self._subscribers.get("*", []))
        for handler in topic_handlers + wildcard_handlers:
            try:
                handler(forwarded)
            except Exception:
                pass

    def adjudicate(self, tool_name: str, args: Dict[str, Any], payload: Dict[str, Any]) -> Optional[str]:
        """事前门禁裁决：先发 tool.before 广播，再逐个跑裁决器；拒绝时发 tool.rejected 广播并返回原因。"""
        table_code = payload.get("table_code") or (payload.get("table") or {}).get("table_code")
        fields = payload.get("fields") or []
        field_name = fields[0].get("name") if len(fields) == 1 and isinstance(fields[0], dict) else None

        before_meta: Dict[str, Any] = {"tool": tool_name, "args": args}
        if table_code:
            before_meta["table_code"] = table_code
        if field_name:
            before_meta["field"] = field_name
        self.fire("tool.before", before_meta)

        with self._lock:
            adjudicators = list(self._adjudicators)

        for fn in adjudicators:
            reason = fn(tool_name, args, payload)
            if reason:
                record: Dict[str, Any] = {
                    "tool": tool_name,
                    "adjudicator": getattr(fn, "__name__", "anonymous"),
                    "reason": reason,
                    "args": args,
                }
                if table_code:
                    record["table_code"] = table_code
                if field_name:
                    record["field"] = field_name
                with self._lock:
                    self._rejections.append(record)
                    observers = list(self._observers)
                self.fire("tool.rejected", record)
                for observer in observers:
                    try:
                        observer(record)
                    except Exception:
                        pass
                return reason
        return None

    # ProdAgent 标准别名：check 等同于 adjudicate
    check = adjudicate

    def drain_rejections(self) -> List[Dict[str, Any]]:
        """取出并清空已累积的 Bus 拦截记录，供 Scheduler 屏障处统一落 BUS_REJECTED 事件。"""
        with self._lock:
            items = list(self._rejections)
            self._rejections.clear()
            return items

    def history(self, topic: Optional[str] = None, run_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """查询已广播的生命周期事件流（可按 topic 或 run_id 过滤）。"""
        with self._lock:
            items = list(self._history)
        if topic is not None:
            items = [item for item in items if item.get("topic") == topic]
        if run_id is not None:
            items = [item for item in items if item.get("run_id") == run_id]
        return items

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()


_GLOBAL_BUS = Bus()


def global_bus() -> Bus:
    """进程级全局根 Bus：汇聚所有 Run 子总线冒泡的生命周期广播。"""
    return _GLOBAL_BUS
