"""确定性审计变异引擎（Deterministic Mutation Engine）。

职责边界（遵循「AI 不下场干活」原则）：
- 黄金基线用例（`build_golden_audit_case`）：纯零覆盖结构，零 LLM 调用；
- 确定性算子反例（`build_deterministic_mutations`）：对 REQUIRED / LENGTH_MAX / LENGTH /
  ENUM / NOT_IN / NUM_RANGE / FIELD_COMPARE / COND_REQUIRED / FOREIGN_KEY 直接按算子语义与
  Oracle 列类型数学构造保证违规的变异样本（0 Token、零漏覆盖）；
- 仅将需要业务语义想象力的 REGEXP 交给 AI 构造贴近真实的格式反例，并在 AI 失败时提供兜底探测。
"""

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from app.engine.binding import (
    _as_number,
    violates_field_compare,
    violates_single_field_rule,
)
from app.engine.oracle import SANDBOX_TEXT_LIMIT
from app.infra.domain import AuditCaseBinding, FieldSpec, RuleDefinition, SpecDocument, field_map


def build_golden_audit_case() -> AuditCaseBinding:
    """零覆盖黄金合规基线用例：不需要调用大模型返回空字典。"""
    return AuditCaseBinding(case_id="GOLDEN_BASELINE", overrides={}, expected_rule_keys=[])


def _is_numeric_field(field: Optional[FieldSpec]) -> bool:
    return bool(field and (field.data_type or "").upper() == "NUMBER")


def _pick_invalid_enum_value(
    field: Optional[FieldSpec],
    forbidden_values: set,
) -> Any:
    if _is_numeric_field(field):
        for candidate in ("999999", "999998", "-999", "888888", "777777"):
            if candidate not in forbidden_values:
                return candidate
        probe = 900000
        while str(probe) in forbidden_values:
            probe += 1
        return str(probe)
    for candidate in ("__INVALID_ENUM__", "ZZ_BAD_CODE", "INVALID_99", "9Z", "!"):
        if candidate not in forbidden_values:
            return candidate
    idx = 1
    while f"BAD_{idx}" in forbidden_values:
        idx += 1
    return f"BAD_{idx}"


def _format_number_like(value: Decimal, keep_int: bool) -> Any:
    if keep_int and value == value.to_integral_value():
        return int(value)
    # 避免科学计数法字符串进入 Oracle 比较
    normalized = format(value.normalize(), "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return int(normalized) if re.fullmatch(r"[+-]?\d+", normalized) else float(normalized)


def _num_range_breach_values(params: Dict[str, Any], field: Optional[FieldSpec]) -> List[Tuple[str, Any]]:
    decimals = int(getattr(field, "decimal_places", 0) or 0) if field else 0
    step = Decimal("1") if decimals == 0 else Decimal("1").scaleb(-min(decimals, 2))
    keep_int = decimals == 0
    results: List[Tuple[str, Any]] = []

    min_val = params.get("min")
    max_val = params.get("max")
    min_inc = params.get("min_inclusive", True)
    max_inc = params.get("max_inclusive", True)

    if min_val is not None:
        try:
            d_min = Decimal(str(min_val))
            candidate = d_min if not min_inc else (d_min - step)
            val = _format_number_like(candidate, keep_int)
            if violates_single_field_rule("NUM_RANGE", params, val) is True:
                results.append(("LOW", val))
        except (InvalidOperation, ValueError):
            pass

    if max_val is not None:
        try:
            d_max = Decimal(str(max_val))
            candidate = d_max if not max_inc else (d_max + step)
            val = _format_number_like(candidate, keep_int)
            if violates_single_field_rule("NUM_RANGE", params, val) is True:
                results.append(("HIGH", val))
        except (InvalidOperation, ValueError):
            pass

    return results


def _field_compare_breach_value(
    op: str,
    left_field: FieldSpec,
    right_value: Any,
) -> Optional[Any]:
    if right_value is None:
        return None
    right_str = str(right_value).strip()
    is_num = _is_numeric_field(left_field)

    # 8 位紧凑日期（YYYYMMDD）单独按合法日期前后偏移，保持可读性且打破字符串/数值序
    if not is_num and re.fullmatch(r"\d{8}", right_str):
        year = int(right_str[:4])
        if op in {"GE", "GT", "EQ"}:
            candidate = right_str if op == "GT" else f"{max(1900, year - 1)}{right_str[4:]}"
        elif op in {"LE", "LT"}:
            candidate = right_str if op == "LT" else f"{min(9999, year + 1)}{right_str[4:]}"
        elif op == "NE":
            candidate = right_str
        else:
            return None
        if violates_field_compare(op, candidate, right_value) is True:
            return candidate

    # 数值比较
    try:
        num = Decimal(str(_as_number(right_value)))
        decimals = int(getattr(left_field, "decimal_places", 0) or 0)
        keep_int = decimals == 0 and "." not in right_str
        delta = Decimal("1") if keep_int else Decimal("0.01")
        if op == "GE":
            cand_dec = num - delta
        elif op == "GT":
            cand_dec = num
        elif op == "LE":
            cand_dec = num + delta
        elif op == "LT":
            cand_dec = num
        elif op == "EQ":
            cand_dec = num + delta
        elif op == "NE":
            cand_dec = num
        else:
            return None
        candidate = _format_number_like(cand_dec, keep_int)
        if not is_num:
            candidate = str(candidate)
        if violates_field_compare(op, candidate, right_value) is True:
            return candidate
    except (InvalidOperation, ValueError):
        if is_num:
            return None

    # 纯字符串字典序比较
    if op == "GE":
        candidate = "0" if right_str > "0" else "!"
    elif op == "GT":
        candidate = right_str
    elif op == "LE":
        candidate = f"{right_str}Z"
    elif op == "LT":
        candidate = right_str
    elif op == "EQ":
        candidate = f"{right_str}_DIFF"
    elif op == "NE":
        candidate = right_str
    else:
        return None
    if violates_field_compare(op, candidate, right_value) is True:
        return candidate
    return None


def _cond_required_trigger_value(
    if_field: Optional[FieldSpec],
    if_operator: str,
    if_values: List[Any],
    golden_if_value: Any,
) -> Any:
    str_values = {str(v) for v in if_values}
    if if_operator == "EQ":
        return if_values[0]
    # NE: 需要一个非空且不在 if_values 里的取值
    if golden_if_value is not None and str(golden_if_value).strip() and str(golden_if_value) not in str_values:
        return golden_if_value
    return _pick_invalid_enum_value(if_field, str_values)


def build_deterministic_mutations(
    spec: SpecDocument,
    rule: RuleDefinition,
    golden_record: Dict[str, Any],
    field_code_sets: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
    mutations_per_rule: int = 1,
) -> Optional[List[AuditCaseBinding]]:
    """对确定性算子直接数学构造违规变异用例；返回 None 表示应交由 AI 语义构造（如 REGEXP）。"""
    fields = field_map(spec)
    field = fields.get(rule.field)
    if not field:
        return None
    target_count = max(1, int(mutations_per_rule or 1))
    op = rule.operator
    params = rule.params or {}
    key = rule.rule_key
    cases: List[AuditCaseBinding] = []

    if op == "REQUIRED":
        cases.append(AuditCaseBinding(case_id=f"MUT_{key}_NULL", overrides={rule.field: None}, expected_rule_keys=[key]))
        if target_count >= 2 and not _is_numeric_field(field):
            cases.append(
                AuditCaseBinding(case_id=f"MUT_{key}_BLANK", overrides={rule.field: "  \t "}, expected_rule_keys=[key])
            )
        return cases[:target_count]

    if op in {"LENGTH_MAX", "LENGTH"}:
        if _is_numeric_field(field):
            return None
        declared = int(params.get("length") or 0)
        if declared <= 0:
            return None
        if op == "LENGTH_MAX":
            if declared >= SANDBOX_TEXT_LIMIT:
                return None
            bad_val = "X" * min(declared + 1, SANDBOX_TEXT_LIMIT)
        else:
            bad_val = "X" * (declared + 1) if declared < SANDBOX_TEXT_LIMIT else "X" * max(1, declared - 1)
        cases.append(AuditCaseBinding(case_id=f"MUT_{key}_LEN", overrides={rule.field: bad_val}, expected_rule_keys=[key]))
        return cases

    if op == "ENUM":
        allowed = {str(v) for v in (params.get("values") or [])}
        cs = (field_code_sets or {}).get((spec.table_code, rule.field))
        if cs:
            allowed.update(str(item["value"]) for item in (cs.get("values") or []))
        bad_val = _pick_invalid_enum_value(field, allowed)
        if violates_single_field_rule("ENUM", params, bad_val) is True:
            cases.append(AuditCaseBinding(case_id=f"MUT_{key}_ENUM", overrides={rule.field: bad_val}, expected_rule_keys=[key]))
            return cases
        return None

    if op == "NOT_IN":
        forbidden = list(params.get("values") or [])
        if not forbidden:
            return None
        for idx, val in enumerate(forbidden[:target_count], start=1):
            if _is_numeric_field(field):
                try:
                    _as_number(val)
                except ValueError:
                    continue
            if violates_single_field_rule("NOT_IN", params, val) is True:
                cases.append(
                    AuditCaseBinding(case_id=f"MUT_{key}_FORBIDDEN_{idx}", overrides={rule.field: val}, expected_rule_keys=[key])
                )
        return cases or None

    if op == "NUM_RANGE":
        breaches = _num_range_breach_values(params, field)
        for tag, val in breaches[:target_count]:
            cases.append(AuditCaseBinding(case_id=f"MUT_{key}_{tag}", overrides={rule.field: val}, expected_rule_keys=[key]))
        return cases or None

    if op == "FIELD_COMPARE":
        right_name = params.get("right_field")
        cmp_op = params.get("operator")
        right_val = golden_record.get(right_name) if right_name else None
        bad_left = _field_compare_breach_value(cmp_op, field, right_val)
        if bad_left is not None:
            cases.append(
                AuditCaseBinding(case_id=f"MUT_{key}_CMP", overrides={rule.field: bad_left}, expected_rule_keys=[key])
            )
            return cases
        return None

    if op == "COND_REQUIRED":
        if_field_name = params.get("if_field")
        if_op = params.get("if_operator")
        if_values = list(params.get("if_values") or [])
        if not if_field_name or not if_values or if_op not in {"EQ", "NE"}:
            return None
        trigger_val = _cond_required_trigger_value(
            fields.get(if_field_name),
            if_op,
            if_values,
            golden_record.get(if_field_name),
        )
        cases.append(
            AuditCaseBinding(
                case_id=f"MUT_{key}_COND",
                overrides={if_field_name: trigger_val, rule.field: None},
                expected_rule_keys=[key],
            )
        )
        return cases

    if op == "FOREIGN_KEY":
        golden_val = str(golden_record.get(rule.field) or "")
        bad_ref = _pick_invalid_enum_value(field, {golden_val}) if _is_numeric_field(field) else "__FK_ORPHAN_REF__"
        if bad_ref == golden_val:
            bad_ref = f"{bad_ref}_X" if not _is_numeric_field(field) else "99999999"
        cases.append(AuditCaseBinding(case_id=f"MUT_{key}_FK", overrides={rule.field: bad_ref}, expected_rule_keys=[key]))
        return cases

    # REGEXP 等复杂格式规则交由 AI 构造贴近业务语义的格式反例
    return None


def build_regexp_fallback_mutation(
    spec: SpecDocument,
    rule: RuleDefinition,
) -> Optional[AuditCaseBinding]:
    """当 AI 未能为 REGEXP 规则构造有效反例时的兜底探测，避免规则沦为未覆盖卡死导出。"""
    if rule.operator != "REGEXP":
        return None
    field = field_map(spec).get(rule.field)
    if _is_numeric_field(field):
        candidates = ("-999999999", "0", "999999999999")
    else:
        candidates = ("INVALID_FMT_@@@", "###BAD###", "Z", "00000000000000000000000000")
    for candidate in candidates:
        if violates_single_field_rule("REGEXP", rule.params or {}, candidate) is True:
            return AuditCaseBinding(
                case_id=f"MUT_{rule.rule_key}_REGEX_FB",
                overrides={rule.field: candidate},
                expected_rule_keys=[rule.rule_key],
            )
    return None
