"""Bus：AI 工具调用的统一门禁层。

模型只负责填工具参数，参数在执行前先过 Bus 的前置裁决（adjudicator）。
裁决器返回拒绝原因即物理拦截，原因回喂给模型重试，并记录到 trace。
这把 ProdAgent「机制在内、策略在外」的 Bus 裁决落到了实际执行链路上：
白名单约束不再只写在 prompt 里，而是代码里的一道硬门。

设计要点：
- Bus 不认识具体算子，裁决器按 tool_name 注册，新增规则只加裁决器；
- 裁决是纯函数（args + payload -> 拒绝原因或 None），无 DB 副作用；
- 拒绝记录进 trace，供 AI_TOOL_ASSIGNED 事件与前端审计展示。
"""

from typing import Any, Callable, Dict, List, Optional

Adjudicator = Callable[[str, Dict[str, Any], Dict[str, Any]], Optional[str]]


class Bus:
    """工具调用总线：前置裁决 + 旁观记录。"""

    def __init__(self) -> None:
        self._adjudicators: List[Adjudicator] = []
        self._observers: List[Callable[[Dict[str, Any]], None]] = []

    def add_adjudicator(self, fn: Adjudicator) -> "Bus":
        self._adjudicators.append(fn)
        return self

    def add_observer(self, fn: Callable[[Dict[str, Any]], None]) -> "Bus":
        self._observers.append(fn)
        return self

    def adjudicate(self, tool_name: str, args: Dict[str, Any], payload: Dict[str, Any]) -> Optional[str]:
        """逐个跑裁决器，第一个拒绝原因立即返回；全部通过返回 None。"""
        for fn in self._adjudicators:
            reason = fn(tool_name, args, payload)
            if reason:
                record = {
                    "tool": tool_name,
                    "adjudicator": getattr(fn, "__name__", "anonymous"),
                    "reason": reason,
                    "args": args,
                }
                for observer in self._observers:
                    observer(record)
                return reason
        return None


# 字段名参数：模型在这些槽位上填错/编造会导致规则绑定到不存在的列
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
    """字段名白名单门禁：所有字段名槽位必须来自 payload.field_names。

    assign_rules 单字段批次里 field_names 是整表字段清单，模型常把简称、
    中文或臆造名（如 ORG_CODE_ERR）塞进来；这里在工具执行前物理拦下。
    """
    allowed = payload.get("field_names")
    if not allowed:
        return None
    allowed_set = set(allowed)
    candidates = list(args.get("rules", [])) or [args]
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
    return None


def cross_table_adjudicator(
    tool_name: str, args: Dict[str, Any], payload: Dict[str, Any]
) -> Optional[str]:
    """跨表引用门禁：references_table / table_code 必须在已确认表集合内。"""
    allowed = payload.get("table_codes") or payload.get("selected_table_codes")
    if not allowed:
        return None
    allowed_set = set(allowed)
    for candidate in args.get("rules", []) or []:
        if not isinstance(candidate, dict):
            continue
        params = candidate.get("params") or {}
        for key in ("references_table", "table_code"):
            value = candidate.get(key) or params.get(key)
            if value is not None and value not in allowed_set:
                return f"跨表引用拦截：{key}='{value}' 不在已确认表集合 {sorted(allowed_set)} 中"
    return None


def default_bus() -> Bus:
    """默认门禁组合：字段白名单 + 跨表引用。"""
    return Bus().add_adjudicator(field_whitelist_adjudicator).add_adjudicator(cross_table_adjudicator)
