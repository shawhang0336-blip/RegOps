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

from app.generation.core.binding import (
    SANDBOX_TEXT_LIMIT,
    _as_number,
    violates_arithmetic_balance,
    violates_cond_value,
    violates_field_compare,
    violates_single_field_rule,
)
from app.infra.contracts import AuditCaseBinding, FieldSpec, RuleDefinition, SpecDocument, field_map


def make_distinct_key_value(value: Any, field: Optional[FieldSpec], offset: int) -> Any:
    """为 UNIQUE_KEY 构造与原值不同、但尽量保持原格式与列宽兼容的唯一键值。"""
    if _is_numeric_field(field):
        try:
            base = Decimal(str(_as_number(value))) if value is not None else Decimal("1000")
            decimals = int(getattr(field, "decimal_places", 0) or 0) if field else 0
            return _format_number_like(base + Decimal(offset), keep_int=(decimals == 0))
        except (InvalidOperation, ValueError):
            return 100000 + offset
    raw = str(value or "")
    max_len = int(getattr(field, "length", 0) or 64) if field else 64
    # 若原值以数字结尾（如 22 位申报号 / 流水号 / 编码），原位增量保持长度与前缀格式不变
    m = re.search(r"(\d+)$", raw)
    if m:
        digits = m.group(1)
        prefix = raw[: m.start(1)]
        width = len(digits)
        mod = 10 ** min(width, 9)
        bumped = (int(digits[-min(width, 9):]) + offset) % mod
        if bumped == int(digits[-min(width, 9):]):
            bumped = (bumped + 1) % mod
        tail = digits[: max(0, width - 9)] + str(bumped).zfill(min(width, 9))
        candidate = f"{prefix}{tail}"
        if candidate != raw and len(candidate) <= max_len:
            return candidate
    suffix = f"_{offset}"
    if len(raw) + len(suffix) <= max_len:
        return f"{raw}{suffix}"
    trim = max(1, max_len - len(suffix))
    return f"{raw[:trim]}{suffix}"[:max_len]


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
            eff_step = step if abs(d_min) < Decimal("1e12") else max(step, (abs(d_min) / Decimal("100")).to_integral_value())
            candidate = d_min if not min_inc else (d_min - eff_step)
            val = _format_number_like(candidate, keep_int)
            if violates_single_field_rule("NUM_RANGE", params, val) is True:
                results.append(("LOW", val))
        except (InvalidOperation, ValueError):
            pass

    if max_val is not None:
        try:
            d_max = Decimal(str(max_val))
            eff_step = step if abs(d_max) < Decimal("1e12") else max(step, (abs(d_max) / Decimal("100")).to_integral_value())
            candidate = d_max if not max_inc else (d_max + eff_step)
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
    non_blank = [v for v in if_values if v is not None and str(v) != ""]
    str_values = {str(v) for v in non_blank}
    if if_operator == "EQ":
        if non_blank:
            return non_blank[0]
        return None
    # NE: 需要一个非空且不在 non_blank 里的取值（空串/None 哨兵表示「条件字段非空时触发」）
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

    if op == "COND_VALUE":
        if_field_name = params.get("if_field")
        if_op = params.get("if_operator")
        if_values = list(params.get("if_values") or [])
        then_op = params.get("then_operator")
        if not if_field_name or not if_values or if_op not in {"EQ", "NE"}:
            return None
        trigger_val = _cond_required_trigger_value(
            fields.get(if_field_name),
            if_op,
            if_values,
            golden_record.get(if_field_name),
        )
        bad_val: Any = None
        if then_op == "MUST_BE_NULL":
            existing_val = golden_record.get(rule.field)
            if existing_val is not None and str(existing_val).strip():
                bad_val = existing_val
            else:
                bad_val = 1 if _is_numeric_field(field) else "NON_NULL_ERR"[: max(1, int(field.length or 12))]
        elif then_op == "ENUM":
            then_vals = list(params.get("then_values") or params.get("values") or [])
            bad_val = _pick_invalid_enum_value(field, {str(v) for v in then_vals})
        elif then_op == "NOT_IN":
            then_vals = list(params.get("then_values") or params.get("values") or [])
            if not then_vals:
                return None
            bad_val = then_vals[0]
        elif then_op == "NUM_RANGE":
            breaches = _num_range_breach_values(params, field)
            if not breaches:
                return None
            bad_val = breaches[0][1]
        else:
            return None
        probe = {**golden_record, if_field_name: trigger_val, rule.field: bad_val}
        if violates_cond_value(params, probe, rule.field) is True:
            cases.append(
                AuditCaseBinding(
                    case_id=f"MUT_{key}_CVAL",
                    overrides={if_field_name: trigger_val, rule.field: bad_val},
                    expected_rule_keys=[key],
                )
            )
            return cases
        return None

    if op == "ARITHMETIC_BALANCE":
        terms = list(params.get("terms") or [])
        if not terms:
            return None
        balance_op = params.get("operator", "EQ")
        tol_raw = params.get("tolerance")
        tol = Decimal(str(tol_raw)) if tol_raw is not None else (Decimal("0.0001") if balance_op == "EQ" else Decimal("0"))
        delta = tol + Decimal("10")
        target_field = params.get("target_field")
        anchor_name = target_field if target_field and target_field in fields else terms[0]["field"]
        anchor_field = fields.get(anchor_name)
        decimals = int(getattr(anchor_field, "decimal_places", 0) or 0) if anchor_field else 0
        try:
            cur_val = Decimal(str(_as_number(golden_record.get(anchor_name)))) if golden_record.get(anchor_name) is not None else Decimal("0")
        except (InvalidOperation, ValueError):
            cur_val = Decimal("0")
        # 双向尝试偏移量，选取第一个经 Decimal 验证确实打破算术公式的值
        candidates = [cur_val + delta, cur_val - delta, cur_val + delta * Decimal("10"), cur_val - delta * Decimal("10")]
        for cand_dec in candidates:
            cand_val = _format_number_like(cand_dec, keep_int=(decimals == 0))
            probe = {**golden_record, anchor_name: cand_val}
            if violates_arithmetic_balance(params, probe) is True:
                cases.append(
                    AuditCaseBinding(
                        case_id=f"MUT_{key}_BAL",
                        overrides={anchor_name: cand_val},
                        expected_rule_keys=[key],
                    )
                )
                return cases
        return None

    if op == "UNIQUE_KEY":
        # 构造一对共享独立冲突键的变异记录（与黄金基线键值隔离，避免污染 GOLDEN_BASELINE）
        key_fields = list(dict.fromkeys(params.get("key_fields") or [rule.field]))
        primary_kf = key_fields[0]
        dup_overrides = {
            primary_kf: make_distinct_key_value(golden_record.get(primary_kf), fields.get(primary_kf), 9001)
        }
        # 联合唯一键：SQL 判重带 `每个键列 IS NOT NULL` 守卫，黄金值为空的键列必须显式补非空值，否则两条 DUP 永不触发
        for idx, kf in enumerate(key_fields[1:], start=1):
            current = golden_record.get(kf)
            if current is None or not str(current).strip():
                dup_overrides[kf] = make_distinct_key_value(None, fields.get(kf), 9001 + idx)
        cases.append(
            AuditCaseBinding(
                case_id=f"MUT_{key}_DUP_1",
                overrides=dict(dup_overrides),
                expected_rule_keys=[key],
            )
        )
        cases.append(
            AuditCaseBinding(
                case_id=f"MUT_{key}_DUP_2",
                overrides=dict(dup_overrides),
                expected_rule_keys=[key],
            )
        )
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
        candidates = ("1.5", "-1.5", "-999999999", "0", "999999999999")
    else:
        candidates = ("INVALID_FMT_@@@", "###BAD###", "   ", "Z", "00000000000000000000000000")
    for candidate in candidates:
        if violates_single_field_rule("REGEXP", rule.params or {}, candidate) is True:
            return AuditCaseBinding(
                case_id=f"MUT_{rule.rule_key}_REGEX_FB",
                overrides={rule.field: candidate},
                expected_rule_keys=[rule.rule_key],
            )
    return None
