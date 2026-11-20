"""Bus：ProdAgent 全生命周期事件总线（事前门禁裁决 check/adjudicate + 事后广播 fire/subscribe）。

一体两面设计（对标李号双 limenagent/prodagent）：
1. 事前门禁裁决（check / adjudicate）：
   模型或节点准备执行动作前先过 Bus 征询；任一裁决器返回拒绝原因即物理拦截，
   原因回喂给模型重试，同时触发 `tool.rejected` 广播并在屏障处落 `BUS_REJECTED` 事件。
2. 全生命周期事后广播（fire / subscribe）：
   覆盖 Run 启停与中断（`run.*`）、节点进出与跳转（`node.*`）、BSP 并发波次（`wave.*`）、
   工具调用前后（`tool.*`）、事件账本追加（`event.appended`）以及文档解析进度（`document.progress`）。
   旁观订阅者（Observer）异常被严格隔离，绝不影响主流程状态机推进。
"""

import threading
import time
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

from .domain import LLM_OPERATORS

Adjudicator = Callable[[str, Dict[str, Any], Dict[str, Any]], Optional[str]]
Subscriber = Callable[[Dict[str, Any]], None]

LIFECYCLE_TOPICS = (
    "run.created",
    "run.resumed",
    "run.interrupted",
    "run.completed",
    "run.failed",
    "node.enter",
    "node.exit",
    "node.redirect",
    "node.error",
    "wave.fanout",
    "wave.barrier",
    "tool.before",
    "tool.after",
    "tool.rejected",
    "event.appended",
    "document.progress",
)

MAX_BUS_HISTORY = 2000


class Bus:
    """全生命周期总线：事前门禁裁决（adjudicate/check）+ 事后主题广播（fire/subscribe）。"""

    def __init__(self, parent: Optional["Bus"] = None) -> None:
        self._adjudicators: List[Adjudicator] = []
        self._observers: List[Subscriber] = []
        self._subscribers: Dict[str, List[Subscriber]] = defaultdict(list)
        self._rejections: List[Dict[str, Any]] = []
        self._history: List[Dict[str, Any]] = []
        self._seq: int = 0
        self._parent: Optional["Bus"] = parent
        self._lock = threading.Lock()

    def add_adjudicator(self, fn: Adjudicator) -> "Bus":
        with self._lock:
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
        """子 Bus（如单次 Run 的 ctx.bus）向全局 Bus 冒泡广播。"""
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


# 字段名参数：模型在这些槽位上填错/编造会导致规则、生成器或表结构绑定到不存在的列
_FIELD_ARG_KEYS = (
    "field",
    "if_field",
    "then_field",
    "right_field",
    "references_field",
    "lhs_field",
    "rhs_field",
)


def field_whitelist_adjudicator(
    tool_name: str, args: Dict[str, Any], payload: Dict[str, Any]
) -> Optional[str]:
    """字段名白名单门禁：所有字段名槽位必须来自 payload.field_names（或 payload.table.field_names）。

    覆盖 assign_schema / assign_rules / assign_generators / assign_audit_cases 全阶段。
    模型常把简称、中文或臆造名（如 ORG_CODE_ERR）塞进来；这里在工具执行前物理拦下。
    """
    allowed = payload.get("field_names") or (payload.get("table") or {}).get("field_names")
    if not allowed:
        return None
    allowed_set = set(allowed)

    candidates: List[Any] = []
    for list_key in ("rules", "fields", "generators"):
        items = args.get(list_key)
        if isinstance(items, list):
            candidates.extend(items)
    if not candidates and not isinstance(args.get("cases"), list):
        candidates = [args]

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        # 字段名既可能在顶层（field/if_field），也可能藏在 params 里
        # （FIELD_COMPARE.right_field、COND_REQUIRED.if_field）。
        scopes = [candidate]
        params = candidate.get("params")
        if isinstance(params, dict):
            scopes.append(params)
        for scope in scopes:
            for key in _FIELD_ARG_KEYS:
                value = scope.get(key)
                if value is not None and value not in allowed_set:
                    return (
                        f"字段幻觉拦截：参数 `{key}='{value}'` 不在字段白名单 "
                        f"{sorted(allowed_set)} 中"
                    )

    for case in args.get("cases") or []:
        if not isinstance(case, dict):
            continue
        overrides = case.get("overrides")
        if isinstance(overrides, dict):
            for field_key in overrides:
                if field_key not in allowed_set:
                    return (
                        f"字段幻觉拦截：审计用例 `{case.get('case_id', '')}` 的变异字段 "
                        f"`{field_key}` 不在字段白名单 {sorted(allowed_set)} 中"
                    )
    return None


def cross_table_adjudicator(
    tool_name: str, args: Dict[str, Any], payload: Dict[str, Any]
) -> Optional[str]:
    """跨表引用门禁：references_table / table_code / golden_overrides 必须在已确认表集合内。"""
    allowed = payload.get("table_codes") or payload.get("selected_table_codes")
    if not allowed:
        return None
    allowed_set = set(allowed)
    for candidate in args.get("rules", []) or []:
        if not isinstance(candidate, dict):
            continue
        params = candidate.get("params") or {}
        for key in ("references_table", "table_code"):
            value = candidate.get(key) or (params.get(key) if isinstance(params, dict) else None)
            if value is not None and value not in allowed_set:
                return f"跨表引用拦截：{key}='{value}' 不在已确认表集合 {sorted(allowed_set)} 中"
    overrides = args.get("golden_overrides")
    if isinstance(overrides, dict):
        for table_code in overrides:
            if table_code not in allowed_set:
                return f"跨表引用拦截：golden_overrides 表 `{table_code}` 不在已确认表集合 {sorted(allowed_set)} 中"
    return None


def operator_whitelist_adjudicator(
    tool_name: str, args: Dict[str, Any], payload: Dict[str, Any]
) -> Optional[str]:
    """受控算子白名单门禁：AI 只能选用 LLM_OPERATORS 中的算子（禁止 LENGTH 等高误报算子）。"""
    if tool_name != "assign_rules":
        return None
    allowed_ops = set(LLM_OPERATORS)
    for candidate in args.get("rules", []) or []:
        if not isinstance(candidate, dict):
            continue
        op = candidate.get("operator")
        if op is not None and op not in allowed_ops:
            return (
                f"算子白名单拦截：算子 `{op}` 不在允许集合 {list(LLM_OPERATORS)} 中。"
                "列宽上限请用 LENGTH_MAX，精确位数格式请用 REGEXP。"
            )
    return None


def code_set_values_adjudicator(
    tool_name: str, args: Dict[str, Any], payload: Dict[str, Any]
) -> Optional[str]:
    """已发布码表值域门禁：当 payload 携带 published_code_set 时，ENUM 规则与生成器取值不得越界。"""
    published = payload.get("published_code_set")
    if not isinstance(published, dict) or not published.get("values"):
        return None
    allowed_values = {str(v) for v in published["values"]}
    code_name = published.get("code") or "PUBLISHED_CODE_SET"

    if tool_name == "assign_rules":
        for candidate in args.get("rules", []) or []:
            if not isinstance(candidate, dict) or candidate.get("operator") != "ENUM":
                continue
            params = candidate.get("params")
            values = (params.get("values") if isinstance(params, dict) else None) or []
            if values:
                invalid = sorted({str(v) for v in values} - allowed_values)
                if invalid:
                    return f"码值白名单拦截：ENUM 参数包含不在已发布码表 {code_name} 中的取值 {invalid}"

    if tool_name == "assign_generators":
        for candidate in args.get("generators", []) or []:
            if not isinstance(candidate, dict) or candidate.get("generator") != "ENUM":
                continue
            params = candidate.get("params")
            values = (params.get("values") if isinstance(params, dict) else None) or []
            if values:
                invalid = sorted({str(v) for v in values} - allowed_values)
                if invalid:
                    return f"码值白名单拦截：ENUM 生成器包含不在已发布码表 {code_name} 中的取值 {invalid}"
    return None


def structure_block_adjudicator(
    tool_name: str, args: Dict[str, Any], payload: Dict[str, Any]
) -> Optional[str]:
    """文档结构切块门禁：source_block_ids 必须来自输入 candidate_blocks。"""
    if tool_name != "assign_document_structure":
        return None
    candidates = payload.get("candidate_blocks")
    if not isinstance(candidates, list) or not candidates:
        return None
    allowed_ids = {item["id"] for item in candidates if isinstance(item, dict) and "id" in item}
    for block in args.get("blocks", []) or []:
        if not isinstance(block, dict):
            continue
        for block_id in block.get("source_block_ids") or []:
            if block_id not in allowed_ids:
                return f"块 ID 幻觉拦截：source_block_ids 包含不在候选列表中的块 ID `{block_id}`"
    return None


_GLOBAL_BUS = (
    Bus()
    .add_adjudicator(field_whitelist_adjudicator)
    .add_adjudicator(cross_table_adjudicator)
    .add_adjudicator(operator_whitelist_adjudicator)
    .add_adjudicator(code_set_values_adjudicator)
    .add_adjudicator(structure_block_adjudicator)
)


def global_bus() -> Bus:
    """进程级全局 Bus：汇聚所有 Run 子总线的生命周期广播与文档解析进度广播。"""
    return _GLOBAL_BUS


def default_bus(parent: Optional[Bus] = _GLOBAL_BUS) -> Bus:
    """单次 Run 的默认总线：搭载五大确定性门禁，并将生命周期广播冒泡至 global_bus。"""
    return (
        Bus(parent=parent)
        .add_adjudicator(field_whitelist_adjudicator)
        .add_adjudicator(cross_table_adjudicator)
        .add_adjudicator(operator_whitelist_adjudicator)
        .add_adjudicator(code_set_values_adjudicator)
        .add_adjudicator(structure_block_adjudicator)
    )

