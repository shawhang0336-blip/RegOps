"""RegOps 事前确定性门禁策略（策略层）。

将五大监管业务门禁裁决器（Adjudicator）挂载到 ProdAgent 的 Bus 微内核上：
1. field_whitelist_adjudicator：字段白名单门禁（覆盖 schema/rules/generators/audit_cases 全阶段）
2. cross_table_adjudicator：跨表引用白名单门禁
3. operator_whitelist_adjudicator：受控算子白名单门禁（仅允许 LLM_OPERATORS，拦截 LENGTH 等高误报算子）
4. code_set_values_adjudicator：已发布码表值域白名单门禁
5. structure_block_adjudicator：阶段一文档结构切块 ID 白名单门禁
"""

from typing import Any, Dict, List, Optional

from app.infra.domain import LLM_OPERATORS
from app.kernel.bus import Bus, global_bus

# 字段名参数：模型在这些槽位上填错/编造会导致规则、生成器或表结构绑定到不存在的列
_FIELD_ARG_KEYS = (
    "field",
    "if_field",
    "then_field",
    "right_field",
    "target_field",
    "references_field",
    "lhs_field",
    "rhs_field",
)


def field_whitelist_adjudicator(
    tool_name: str, args: Dict[str, Any], payload: Dict[str, Any]
) -> Optional[str]:
    """字段名白名单门禁：所有字段名槽位必须来自 payload.field_names（或 payload.table.field_names）。"""
    safe_payload = payload if isinstance(payload, dict) else {}
    table_obj = safe_payload.get("table")
    allowed = safe_payload.get("field_names") or (
        table_obj.get("field_names") if isinstance(table_obj, dict) else None
    )
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
        scopes = [candidate]
        params = candidate.get("params")
        if isinstance(params, dict):
            scopes.append(params)
        for scope in scopes:
            for key in _FIELD_ARG_KEYS:
                value = scope.get(key)
                if value is not None and (not isinstance(value, str) or value not in allowed_set):
                    return (
                        f"字段幻觉拦截：参数 `{key}='{value}'` 不在字段白名单 "
                        f"{sorted(allowed_set)} 中"
                    )
            # 复杂监管算子字段列表槽位检查：ARITHMETIC_BALANCE.terms[].field / UNIQUE_KEY.key_fields[]
            terms = scope.get("terms")
            if isinstance(terms, list):
                for term in terms:
                    if (
                        isinstance(term, dict)
                        and term.get("field") is not None
                        and (not isinstance(term.get("field"), str) or term.get("field") not in allowed_set)
                    ):
                        return (
                            f"字段幻觉拦截：算术项 `terms.field='{term.get('field')}'` "
                            f"不在字段白名单 {sorted(allowed_set)} 中"
                        )
            key_fields = scope.get("key_fields")
            if isinstance(key_fields, list):
                for kf in key_fields:
                    if kf is not None and (not isinstance(kf, str) or kf not in allowed_set):
                        return (
                            f"字段幻觉拦截：唯一键字段 `key_fields` 包含不在白名单中的 `{kf}` "
                            f"{sorted(allowed_set)}"
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
    """跨表引用门禁：references_table / references_field / table_code / golden_overrides 必须在已确认表与字段白名单内。"""
    safe_payload = payload if isinstance(payload, dict) else {}
    allowed = safe_payload.get("table_codes") or safe_payload.get("selected_table_codes")
    if not allowed:
        return None
    allowed_set = set(allowed)
    table_fields = safe_payload.get("table_fields") or {}
    for candidate in args.get("rules", []) or []:
        if not isinstance(candidate, dict):
            continue
        params = candidate.get("params") if isinstance(candidate.get("params"), dict) else {}
        for scope in (candidate, params):
            for key in ("references_table", "table_code"):
                value = scope.get(key)
                if value is not None and value not in allowed_set:
                    return f"跨表引用拦截：{key}='{value}' 不在已确认表集合 {sorted(allowed_set)} 中"
        if params and table_fields:
            ref_table = params.get("references_table")
            ref_field = params.get("references_field")
            if ref_table in table_fields and ref_field is not None and ref_field not in set(table_fields[ref_table]):
                return (
                    f"跨表字段幻觉拦截：references_field='{ref_field}' "
                    f"不在目标表 {ref_table} 字段白名单 {sorted(table_fields[ref_table])} 中"
                )
    overrides = args.get("golden_overrides")
    if isinstance(overrides, dict):
        for table_code, field_overrides in overrides.items():
            if table_code not in allowed_set:
                return f"跨表引用拦截：golden_overrides 表 `{table_code}` 不在已确认表集合 {sorted(allowed_set)} 中"
            if isinstance(field_overrides, dict) and table_code in table_fields:
                allowed_cols = set(table_fields[table_code])
                for col in field_overrides:
                    if col not in allowed_cols:
                        return (
                            f"跨表字段幻觉拦截：golden_overrides[{table_code}] 包含不在目标表白名单中的字段 "
                            f"`{col}`（允许：{sorted(allowed_cols)}）"
                        )
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
    """已发布码表值域门禁：覆盖 assign_rules（ENUM 与 COND_VALUE.ENUM）与 assign_generators（单码表与多字段码表映射）。"""
    safe_payload = payload if isinstance(payload, dict) else {}
    published = safe_payload.get("published_code_set")
    published_map = safe_payload.get("published_code_sets")

    def _resolve_target_code_set(field_name: Any) -> Optional[Dict[str, Any]]:
        target_cs = None
        # 存在多字段映射 published_code_sets 时严禁把单字段 published_code_set 误套到未关联码表的其他字段
        if isinstance(published_map, dict):
            target_cs = published_map.get(field_name) if field_name is not None else None
        elif isinstance(published, dict):
            target_cs = published
        if isinstance(target_cs, dict) and target_cs.get("values"):
            return target_cs
        return None

    if tool_name == "assign_rules":
        for candidate in args.get("rules", []) or []:
            if not isinstance(candidate, dict):
                continue
            op = candidate.get("operator")
            params = candidate.get("params") if isinstance(candidate.get("params"), dict) else {}
            if op == "ENUM":
                values = params.get("values") or []
                label = "ENUM 参数"
            elif op == "COND_VALUE" and params.get("then_operator") == "ENUM":
                values = params.get("then_values") or params.get("values") or []
                label = "COND_VALUE(ENUM) 参数"
            else:
                continue
            target_cs = _resolve_target_code_set(candidate.get("field"))
            if not target_cs or not values:
                continue
            allowed_values = {str(v) for v in target_cs["values"]}
            code_name = target_cs.get("code") or "PUBLISHED_CODE_SET"
            invalid = sorted({str(v) for v in values} - allowed_values)
            if invalid:
                return f"码值白名单拦截：{label}包含不在已发布码表 {code_name} 中的取值 {invalid}"

    if tool_name == "assign_generators":
        for candidate in args.get("generators", []) or []:
            if not isinstance(candidate, dict) or candidate.get("generator") != "ENUM":
                continue
            target_cs = _resolve_target_code_set(candidate.get("field"))
            if not target_cs:
                continue
            allowed_values = {str(v) for v in target_cs["values"]}
            code_name = target_cs.get("code") or "PUBLISHED_CODE_SET"
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


REGOPS_ADJUDICATORS = (
    field_whitelist_adjudicator,
    cross_table_adjudicator,
    operator_whitelist_adjudicator,
    code_set_values_adjudicator,
    structure_block_adjudicator,
)


def _attach_regops_adjudicators(bus: Bus) -> Bus:
    for fn in REGOPS_ADJUDICATORS:
        bus.add_adjudicator(fn)
    return bus


_attach_regops_adjudicators(global_bus())


def default_bus(parent: Optional[Bus] = None) -> Bus:
    """单次 Run 的默认总线：搭载五大确定性门禁，并将生命周期广播冒泡至 global_bus。"""
    effective_parent = global_bus() if parent is None else parent
    return _attach_regops_adjudicators(Bus(parent=effective_parent))
