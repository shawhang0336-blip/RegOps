"""Bus：AI 工具调用的统一门禁层。

模型只负责填工具参数，参数在执行前先过 Bus 的前置裁决（adjudicator）。
裁决器返回拒绝原因即物理拦截，原因回喂给模型重试，并记录到 trace 与 EventLog（BUS_REJECTED）。
这把 ProdAgent「机制在内、策略在外」的 Bus 裁决落到了实际执行链路上：
白名单约束不再只写在 prompt 里，而是代码里的一道硬门。

设计要点：
- Bus 不认识具体算子实现，裁决器按 tool_name 与 payload 契约运行，新增规则只加裁决器；
- 裁决是纯函数（args + payload -> 拒绝原因或 None），无 DB 副作用；
- 拦截记录线程安全地暂存，既回写 trace，又在 Scheduler 屏障处统一落 BUS_REJECTED 事件。
"""

import threading
from typing import Any, Callable, Dict, List, Optional

from .domain import LLM_OPERATORS

Adjudicator = Callable[[str, Dict[str, Any], Dict[str, Any]], Optional[str]]


class Bus:
    """工具调用总线：前置裁决 + 旁观记录 + 线程安全拦截缓冲。"""

    def __init__(self) -> None:
        self._adjudicators: List[Adjudicator] = []
        self._observers: List[Callable[[Dict[str, Any]], None]] = []
        self._rejections: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

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
                table_code = payload.get("table_code") or (payload.get("table") or {}).get("table_code")
                fields = payload.get("fields") or []
                field_name = fields[0].get("name") if len(fields) == 1 and isinstance(fields[0], dict) else None
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
                for observer in observers:
                    observer(record)
                return reason
        return None

    def drain_rejections(self) -> List[Dict[str, Any]]:
        """取出并清空已累积的 Bus 拦截记录，供 Scheduler 屏障处统一落 BUS_REJECTED 事件。"""
        with self._lock:
            items = list(self._rejections)
            self._rejections.clear()
            return items


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


def default_bus() -> Bus:
    """默认门禁组合：字段白名单 + 跨表引用 + 算子白名单 + 码表值域 + 结构块白名单。"""
    return (
        Bus()
        .add_adjudicator(field_whitelist_adjudicator)
        .add_adjudicator(cross_table_adjudicator)
        .add_adjudicator(operator_whitelist_adjudicator)
        .add_adjudicator(code_set_values_adjudicator)
        .add_adjudicator(structure_block_adjudicator)
    )

