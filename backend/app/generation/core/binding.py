import hashlib
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from app.infra.contracts import (
    LLM_OPERATORS,
    AuditCaseBinding,
    Evidence,
    GeneratorBinding,
    RuleDefinition,
    SchemaBinding,
    SpecDocument,
    calculate_rule_hash,
    field_map,
)
from app.generation.core.rules import (
    RuleValidationError,
    normalize_oracle_regexp,
    posix_classes_to_python,
    validate_rule,
)


def compile_schema_bindings(spec: SpecDocument) -> List[SchemaBinding]:
    """根据已确认字段元数据确定性编译物理存储 Schema（零 AI 抄写、零漏字段）。"""
    items = []
    for field in spec.fields:
        is_number = (field.data_type or "").upper() == "NUMBER"
        items.append(
            {
                "field": field.name,
                "storage_type": "NUMBER" if is_number else "TEXT",
                "length": int(field.length or (22 if is_number else 128)),
                "decimal_places": int(field.decimal_places or 0) if is_number else 0,
            }
        )
    return bind_schema(spec, items)


def bind_schema(spec: SpecDocument, items: List[Dict[str, Any]]) -> List[SchemaBinding]:
    bindings = [SchemaBinding.model_validate(item) for item in items]
    expected = field_map(spec)
    if {item.field for item in bindings} != set(expected) or len(bindings) != len(expected):
        raise RuleValidationError("Schema assignment must cover every whitelisted field exactly once")
    for item in bindings:
        source = expected[item.field]
        if item.length > source.length:
            raise RuleValidationError(f"{item.field} length exceeds the source dictionary")
        if item.storage_type == "NUMBER" and source.data_type != "NUMBER":
            raise RuleValidationError(f"{item.field} cannot be mapped to NUMBER")
        if source.data_type == "NUMBER" and item.storage_type != "NUMBER":
            raise RuleValidationError(f"{item.field} must remain numeric")
    return bindings


def bind_rules(
    spec: SpecDocument,
    items: List[Dict[str, Any]],
    selected_table_codes: Optional[set] = None,
    specs_by_code: Optional[Dict[str, SpecDocument]] = None,
) -> List[RuleDefinition]:
    rules = []
    seen = set()
    for index, item in enumerate(items, start=1):
        field = item["field"]
        metadata = field_map(spec).get(field)
        if not metadata:
            raise RuleValidationError(f"Unknown rule field: {field}")
        evidence_kind = item.get("evidence_kind", "TERM_INFERENCE")
        if evidence_kind not in {"DICTIONARY", "TERM_INFERENCE"}:
            raise RuleValidationError("Invalid evidence kind")
        source = "DOC_EXPLICIT" if evidence_kind == "DICTIONARY" else "AI_EXPANDED"
        quote = metadata.check_text if source == "DOC_EXPLICIT" else f"{spec.terms_text[:500]}｜数据字典：{metadata.check_text}"
        operator = item["operator"]
        if operator not in LLM_OPERATORS:
            # enum 约束只是提示，模型仍可能硬返回 LENGTH；这里做硬拦
            raise RuleValidationError(
                f"算子 {operator} 不允许由模型生成。长度类请用 LENGTH_MAX（列宽是上限），"
                "需要「必须正好 N 位」请用 REGEXP 写死格式。"
            )
        params = dict(item.get("params", {}))
        if operator == "REGEXP" and isinstance(params.get("pattern"), str):
            params["pattern"] = normalize_oracle_regexp(params["pattern"])
        if operator == "FOREIGN_KEY":
            ref_table = params.get("references_table")
            ref_field = params.get("references_field")
            if selected_table_codes is None or ref_table not in selected_table_codes:
                raise RuleValidationError("Cross-rule reference is outside the confirmed table set")
            if not ref_field:
                raise RuleValidationError("Cross-table FOREIGN_KEY requires references_field")
            if specs_by_code is not None:
                target_spec = specs_by_code.get(ref_table)
                if not target_spec or ref_field not in field_map(target_spec):
                    raise RuleValidationError(
                        f"Cross-table reference field `{ref_table}.{ref_field}` is outside the target table whitelist"
                    )
        raw_key = item.get("rule_key") or f"R_{spec.section.replace('.', '_')}_{index}_{field}_{operator}"
        rule_key = re.sub(r"[^A-Z0-9_]", "_", raw_key.upper())
        if rule_key in seen:
            raise RuleValidationError(f"Duplicate rule key: {rule_key}")
        seen.add(rule_key)
        rule = RuleDefinition(
            rule_key=rule_key,
            table_code=spec.table_code,
            field=field,
            operator=operator,
            params=params,
            source=source,
            evidence=Evidence(
                section=f"{spec.section}.3" if source == "DOC_EXPLICIT" else f"{spec.section}.1/{spec.section}.3",
                page=metadata.evidence_page,
                field=field,
                quote=quote,
            ),
            rationale=item.get("rationale", ""),
        )
        rule.rule_hash = calculate_rule_hash(rule, spec.source_sha256)
        validate_rule(rule, spec)
        rules.append(rule)
    return rules


def _as_number(value: Any) -> Any:
    """把 JSON 里写成字符串的数字收敛成数值（严格半角 ASCII，拦截全角数字如 '１００' 引发 Oracle ORA-01722）。

    实测出现过 SEQUENCE 的 start/step 都写成 "1000.00" / "100.00"，
    于是 index=1 时 `"1000.00" + 1 * "100.00"` 走成了字符串拼接，
    生成出 "1000.00100.00"，插 NUMBER 列直接 ORA-01722 整次审计失败。
    千分位逗号（"1,000.00"）视为合法数字，入库前由 fit_sandbox_values 清洗。
    """
    if isinstance(value, bool):
        raise ValueError("布尔值不是数字")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"非有限数值：{value!r}")
        return value
    text = str(value).strip().replace(",", "")
    if not text.isascii():
        raise ValueError(f"包含非 ASCII 字符（如全角数字）：{value!r}")
    if re.fullmatch(r"[+-]?[0-9]+", text):
        return int(text)
    if re.fullmatch(r"[+-]?[0-9]+\.[0-9]+", text):
        return float(text)
    raise ValueError(f"不是数字：{value!r}")


def _validate_generator_item(field, item: "GeneratorBinding") -> None:
    """单字段生成器校验：bind_generators 整表校验与 _data_stage 单字段回调共用。

    数值字段只验不改：FIXED/ENUM 的值保持原始字符串形态（码表白名单按字符串比对，
    Oracle 插入 NUMBER 列时会隐式转换），改写为 int/float 反而会打破下游比对。
    """
    if item.generator == "FIXED" and "value" not in item.params:
        raise RuleValidationError(f"{item.field} FIXED generator requires a value")
    if item.generator == "ENUM" and not item.params.get("values"):
        raise RuleValidationError(f"{item.field} ENUM generator requires values")
    if item.generator == "SEQUENCE":
        if "start" not in item.params:
            raise RuleValidationError(f"{item.field} SEQUENCE generator requires start")
        for key in ("start", "step"):
            if key not in item.params:
                continue
            try:
                item.params[key] = _as_number(item.params[key])
            except ValueError as exc:
                raise RuleValidationError(
                    f"{item.field} SEQUENCE 的 {key} 必须是数字（不能是带单位的文本）：{exc}"
                ) from exc
        for key in ("padding", "width"):
            if key in item.params and item.params[key] is not None:
                try:
                    item.params[key] = int(item.params[key])
                except (TypeError, ValueError) as exc:
                    raise RuleValidationError(f"{item.field} SEQUENCE 的 {key} 必须是整数") from exc
    if (getattr(field, "data_type", "") or "").upper() != "NUMBER":
        return
    try:
        if item.generator == "FIXED":
            _as_number(item.params["value"])
        elif item.generator == "ENUM":
            for value in item.params["values"]:
                _as_number(value)
        elif item.generator == "SEQUENCE" and str(item.params.get("prefix") or ""):
            raise ValueError("prefix 会拼出非数字文本")
    except ValueError as exc:
        raise RuleValidationError(
            f"{item.field} 是数值字段，生成器产出必须能解析成数字（否则 NUMBER 列入不了库）：{exc}"
        ) from exc


def bind_generators(spec: SpecDocument, items: List[Dict[str, Any]]) -> List[GeneratorBinding]:
    bindings = [GeneratorBinding.model_validate(item) for item in items]
    expected = field_map(spec)
    if {item.field for item in bindings} != set(expected) or len(bindings) != len(expected):
        raise RuleValidationError("Generator assignment must cover every whitelisted field exactly once")
    for item in bindings:
        _validate_generator_item(expected[item.field], item)
    return bindings


def materialize_golden_record(spec: SpecDocument, bindings: List[GeneratorBinding], index: int = 0) -> Dict[str, Any]:
    record = {}
    for item in bindings:
        if item.generator == "FIXED":
            record[item.field] = item.params["value"]
        elif item.generator == "ENUM":
            values = item.params["values"]
            record[item.field] = values[index % len(values)]
        else:
            start = item.params["start"]
            step = item.params.get("step", 1)
            if isinstance(start, int) and isinstance(step, int) and not isinstance(start, bool) and not isinstance(step, bool):
                value: Any = start + index * step
            else:
                # 浮点累加会产出 0.30000000000000004 这类脏值（金额 SEQUENCE 入 NUMBER(22,2) 直接截断失真），统一走 Decimal
                value = Decimal(str(start)) + Decimal(index) * Decimal(str(step))
            # AI 有时写 width 有时写 padding，两个都认
            padding = int(item.params.get("padding") or item.params.get("width") or 0)
            text = str(value).zfill(padding) if padding else str(value)
            record[item.field] = f"{item.params.get('prefix') or ''}{text}"
    return record


# 只在单个字段上判定的算子：要打死它们，变异必须落在那个字段上
SELF_FIELD_OPERATORS = {"REQUIRED", "ENUM", "NOT_IN", "LENGTH", "LENGTH_MAX", "REGEXP", "NUM_RANGE"}


def _is_blank(value: Any) -> bool:
    return value is None or not str(value).strip()


def violates_single_field_rule(operator: str, params: Dict[str, Any], value: Any) -> bool:
    """在 Python 侧预判这个取值是否真的违反规则。

    目的是把「空转用例」挡在绑定阶段：实测出现过 REMARK_LENGTH_MAX_C5 声称
    把备注改到超过 256，实际给的字符串没超，于是审计报「该命中未命中」，
    回流去改规则——可长度就是 256，改不动，白跑一轮。

    判定不了的（COND_REQUIRED / FOREIGN_KEY / 非法正则）返回 None，不做结论。
    """
    if operator == "REQUIRED":
        return _is_blank(value)
    if operator == "ENUM":
        return not _is_blank(value) and str(value) not in {str(v) for v in params.get("values", [])}
    if operator == "NOT_IN":
        return not _is_blank(value) and str(value) in {str(v) for v in params.get("values", [])}
    if operator in {"LENGTH", "LENGTH_MAX"}:
        if params.get("length") is None:
            return None
        # Oracle 中空串 '' 等价于 NULL，而 LENGTH/LENGTH_MAX 规则带 IS NOT NULL 守卫，空值不触发
        if value is None or value == "":
            return False
        size = len(str(value))
        if operator == "LENGTH":
            return size != int(params["length"])
        return size > int(params["length"])
    if operator == "REGEXP":
        pattern = params.get("pattern")
        if not pattern:
            return None
        # Oracle 中空串 '' 等价于 NULL，REGEXP 带 IS NOT NULL 守卫，空串/None 永远不会触发 REGEXP
        if value is None or value == "":
            return False
        try:
            # Oracle 的 REGEXP_LIKE 是「包含匹配」，用 search 对齐；POSIX 字符类先翻译成 Python 等价写法
            return re.search(posix_classes_to_python(pattern), str(value)) is None
        except re.error:
            return None
    if operator == "NUM_RANGE":
        if value is None or str(value).strip() == "":
            return False
        try:
            text_val = str(value).strip().replace(",", "")
            if not text_val.isascii():
                return None
            number = Decimal(text_val)
        except (TypeError, ValueError, InvalidOperation):
            return None
        min_value = params.get("min")
        max_value = params.get("max")
        if min_value is not None:
            try:
                bound = Decimal(str(min_value).strip().replace(",", ""))
            except (TypeError, ValueError, InvalidOperation):
                return None
            if number < bound or (not params.get("min_inclusive", True) and number == bound):
                return True
        if max_value is not None:
            try:
                bound = Decimal(str(max_value).strip().replace(",", ""))
            except (TypeError, ValueError, InvalidOperation):
                return None
            if number > bound or (not params.get("max_inclusive", True) and number == bound):
                return True
        return False
    return None


_COMPARE_RELATIONS = {
    "LT": lambda left, right: left < right,
    "LE": lambda left, right: left <= right,
    "GT": lambda left, right: left > right,
    "GE": lambda left, right: left >= right,
    "EQ": lambda left, right: left == right,
    "NE": lambda left, right: left != right,
}


def violates_field_compare(operator: str, left: Any, right: Any) -> Optional[bool]:
    """预判两列取值是否打破 field OP right_field 的断言。

    能转数值就按高精度 Decimal 比（与生产 Oracle NUMBER(22,2) 一致，避免 float64 精度丢失），
    转不了按字符串序（与沙箱/生产字符列的字典序比较一致）。任一侧缺失或关系未知返回 None。
    """
    relation = _COMPARE_RELATIONS.get(operator)
    if relation is None or left is None or right is None:
        return None
    try:
        l_txt = str(left).strip().replace(",", "")
        r_txt = str(right).strip().replace(",", "")
        if not (l_txt.isascii() and r_txt.isascii()):
            raise ValueError("non-ascii")
        left_value: Any = Decimal(l_txt)
        right_value: Any = Decimal(r_txt)
    except (TypeError, ValueError, InvalidOperation):
        left_value, right_value = str(left), str(right)
    try:
        return not relation(left_value, right_value)
    except TypeError:
        return None


def _matches_condition(params: Dict[str, Any], record: Dict[str, Any]) -> Optional[bool]:
    if_field = params.get("if_field")
    if_op = params.get("if_operator")
    if_values = params.get("if_values") or []
    if not if_field or if_op not in {"EQ", "NE"} or not if_values:
        return None
    cond_val = record.get(if_field)
    has_blank = any(v is None or str(v) == "" for v in if_values)
    non_blank = {str(v) for v in if_values if v is not None and str(v) != ""}
    cond_blank = _is_blank(cond_val)
    if if_op == "EQ":
        return (has_blank and cond_blank) or (not cond_blank and str(cond_val) in non_blank)
    if has_blank and not non_blank:
        return not cond_blank
    return (not cond_blank) and (str(cond_val) not in non_blank)


def violates_cond_required(params: Dict[str, Any], record: Dict[str, Any], target_field: str) -> Optional[bool]:
    """预判记录是否触发 COND_REQUIRED（条件成立且目标字段缺失，与 Oracle 空串/NULL 语义一致）。"""
    matched = _matches_condition(params, record)
    if matched is None:
        return None
    return bool(matched and _is_blank(record.get(target_field)))


def violates_cond_value(params: Dict[str, Any], record: Dict[str, Any], target_field: str) -> Optional[bool]:
    """预判记录是否触发 COND_VALUE（条件成立且目标字段违反 MUST_BE_NULL / ENUM / NOT_IN / NUM_RANGE）。"""
    matched = _matches_condition(params, record)
    if matched is None:
        return None
    if not matched:
        return False
    val = record.get(target_field)
    then_op = params.get("then_operator")
    if then_op == "MUST_BE_NULL":
        return not _is_blank(val)
    if then_op in {"ENUM", "NOT_IN"}:
        then_vals = params.get("then_values")
        if then_vals is None:
            then_vals = params.get("values") or []
        return violates_single_field_rule(then_op, {"values": then_vals}, val)
    if then_op == "NUM_RANGE":
        return violates_single_field_rule("NUM_RANGE", params, val)
    return None


def _to_decimal_opt(value: Any) -> Optional[Decimal]:
    if value is None or str(value).strip() == "":
        return None
    text_val = str(value).strip().replace(",", "")
    if not text_val.isascii():
        return None
    try:
        return Decimal(text_val)
    except InvalidOperation:
        return None


def violates_arithmetic_balance(params: Dict[str, Any], record: Dict[str, Any]) -> Optional[bool]:
    """高精度 Decimal 预判行内算术平衡/勾稽规则是否被违反。"""
    terms = params.get("terms") or []
    if not terms:
        return None
    null_as_zero = bool(params.get("null_as_zero", False))
    balance_op = params.get("operator", "EQ")
    tol_raw = params.get("tolerance")
    tol = Decimal(str(tol_raw)) if tol_raw is not None else (Decimal("0.0001") if balance_op == "EQ" else Decimal("0"))
    lhs = Decimal("0")
    for term in terms:
        field_name = term.get("field")
        coef = _to_decimal_opt(term.get("coef", 1))
        if not field_name or coef is None:
            return None
        val = _to_decimal_opt(record.get(field_name))
        if val is None:
            if null_as_zero:
                val = Decimal("0")
            else:
                return False
        lhs += val * coef
    target_field = params.get("target_field")
    if target_field is not None:
        rhs = _to_decimal_opt(record.get(target_field))
        if rhs is None:
            return False
    elif params.get("target_value") is not None:
        rhs = _to_decimal_opt(params.get("target_value"))
        if rhs is None:
            return None
    else:
        return None
    if balance_op == "EQ":
        return abs(lhs - rhs) > tol
    if balance_op == "GE":
        return lhs < (rhs - tol)
    if balance_op == "LE":
        return lhs > (rhs + tol)
    if balance_op == "GT":
        return lhs <= (rhs - tol)
    if balance_op == "LT":
        return lhs >= (rhs + tol)
    return None


def find_golden_rule_violations(
    spec: SpecDocument,
    rules: List[RuleDefinition],
    golden_record: Dict[str, Any],
    golden_generators: Optional[List[GeneratorBinding]] = None,
    golden_count: int = 1,
    golden_overrides: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Python 侧黄金样本自洽门禁：在入 Oracle 前检查黄金样本是否会误触任何已批准规则。"""
    table_rules = [rule for rule in rules if rule.table_code == spec.table_code]
    if not table_rules:
        return []
    sample_records: List[Dict[str, Any]] = []
    if golden_generators:
        total = min(max(int(golden_count or 1), 1), 5)
        for idx in range(total):
            rec = materialize_golden_record(spec, golden_generators, idx)
            if golden_overrides:
                rec.update(golden_overrides)
            sample_records.append(rec)
    else:
        sample_records.append(dict(golden_record))

    violations: List[Dict[str, Any]] = []
    seen = set()
    for row_idx, rec in enumerate(sample_records):
        for rule in table_rules:
            if rule.rule_key in seen:
                continue
            op = rule.operator
            params = rule.params or {}
            broken = False
            detail = ""
            if op in SELF_FIELD_OPERATORS:
                val = rec.get(rule.field)
                if violates_single_field_rule(op, params, val) is True:
                    broken = True
                    if op == "ENUM" and isinstance(params.get("values"), list) and len(params["values"]) > 12:
                        vals = params["values"]
                        params_view = f"共 {len(vals)} 项，示例 {vals[:8]}"
                    else:
                        params_view = str(params)
                    detail = f"{rule.field}={val!r} 违反 {op}({params_view})"
            elif op == "FIELD_COMPARE":
                right_field = params.get("right_field")
                left_val = rec.get(rule.field)
                right_val = rec.get(right_field) if right_field else None
                if violates_field_compare(params.get("operator"), left_val, right_val) is True:
                    broken = True
                    multi_hint = "（第 %d 条黄金样本失配，请将关联双方改为一致的 FIXED 生成器）" % (row_idx + 1) if row_idx > 0 else ""
                    detail = f"{rule.field}={left_val!r} 与 {right_field}={right_val!r} 不满足 {params.get('operator')}{multi_hint}"
            elif op == "COND_REQUIRED":
                if violates_cond_required(params, rec, rule.field) is True:
                    broken = True
                    multi_hint = f"（第 {row_idx + 1} 条黄金样本失配，请将条件字段改为 FIXED）" if row_idx > 0 else ""
                    detail = (
                        f"当 {params.get('if_field')}={rec.get(params.get('if_field'))!r} 时 "
                        f"{rule.field} 不能为空{multi_hint}"
                    )
            elif op == "COND_VALUE":
                if violates_cond_value(params, rec, rule.field) is True:
                    broken = True
                    multi_hint = f"（第 {row_idx + 1} 条黄金样本失配，请将条件关联字段改为一致的 FIXED）" if row_idx > 0 else ""
                    detail = (
                        f"当 {params.get('if_field')}={rec.get(params.get('if_field'))!r} 时 "
                        f"{rule.field}={rec.get(rule.field)!r} 违反条件约束 {params.get('then_operator')}{multi_hint}"
                    )
            elif op == "ARITHMETIC_BALANCE":
                if violates_arithmetic_balance(params, rec) is True:
                    broken = True
                    multi_hint = f"（第 {row_idx + 1} 条黄金样本失配，请将公式涉及字段统一用 FIXED）" if row_idx > 0 else ""
                    detail = f"算术勾稽公式不满足（{params.get('operator', 'EQ')}）{multi_hint}"
            if broken:
                seen.add(rule.rule_key)
                violations.append(
                    {
                        "rule_key": rule.rule_key,
                        "field": rule.field,
                        "operator": op,
                        "message": f"规则 {rule.rule_key} 误报风险：{detail}",
                    }
                )
    # 多条黄金样本之间的 UNIQUE_KEY 重复检测
    if len(sample_records) > 1:
        for rule in table_rules:
            if rule.operator != "UNIQUE_KEY" or rule.rule_key in seen:
                continue
            key_fields = list(dict.fromkeys((rule.params or {}).get("key_fields") or [rule.field]))
            seen_tuples = set()
            for rec in sample_records:
                vals = tuple(None if _is_blank(rec.get(kf)) else str(rec.get(kf)) for kf in key_fields)
                if any(v is None for v in vals):
                    continue
                if vals in seen_tuples:
                    seen.add(rule.rule_key)
                    violations.append(
                        {
                            "rule_key": rule.rule_key,
                            "field": rule.field,
                            "operator": "UNIQUE_KEY",
                            "message": (
                                f"规则 {rule.rule_key} 误报风险：多条黄金样本的唯一键 {key_fields}={vals} 发生重复，"
                                "请将主键字段改为 SEQUENCE 生成器确保每条黄金样本唯一。"
                            ),
                        }
                    )
                    break
                seen_tuples.add(vals)
    return violations


MAX_CASE_ID_LENGTH = 64
# 沙箱宽表文本列物理上限（Oracle VARCHAR2(4000 CHAR) / 4000 字节保护上限）
SANDBOX_TEXT_LIMIT = 4000


def normalize_case_id(case_id: str, max_len: int = MAX_CASE_ID_LENGTH) -> str:
    """将审计用例 ID 收敛至 Oracle record_id 列宽上限以内，避免长表名+长规则名触发 ORA-12899。"""
    cleaned = str(case_id or "").strip()
    if len(cleaned) <= max_len:
        return cleaned
    digest = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:8].upper()
    prefix = cleaned[: max(1, max_len - 9)].rstrip("_")
    return f"{prefix}_{digest}"


def bind_audit_cases(
    spec: SpecDocument,
    items: List[Dict[str, Any]],
    active_rule_keys: set,
    rule_index: Optional[Dict[str, tuple]] = None,
    golden_record: Optional[Dict[str, Any]] = None,
) -> List[AuditCaseBinding]:
    cases = [AuditCaseBinding.model_validate(item) for item in items]
    if not cases:
        raise RuleValidationError("At least one audit case is required")
    numeric_fields = {
        field.name for field in spec.fields if (field.data_type or "").upper() == "NUMBER"
    }
    case_ids = set()
    for case in cases:
        case.case_id = normalize_case_id(case.case_id)
        if case.case_id in case_ids:
            raise RuleValidationError("Duplicate audit case ID")
        case_ids.add(case.case_id)
        if any(field not in field_map(spec) for field in case.overrides):
            raise RuleValidationError("Audit override is outside the field whitelist")
        # 沙箱数值列是原生 NUMBER：塞非数字或全角数字（如 '１００'）会在入库时 ORA-01722 挂掉整批，绑定阶段就拦下
        for override_field, override_value in case.overrides.items():
            if override_field in numeric_fields and override_value is not None:
                try:
                    _as_number(override_value)
                except (TypeError, ValueError) as exc:
                    raise RuleValidationError(
                        f"用例 {case.case_id} 把数值字段 {override_field} 变异成非法数值 {override_value!r}（{exc}），"
                        "沙箱原生 NUMBER 列无法入库。数值字段的违规请使用半角 ASCII 越界数值（如 0、-1、1.5、超大数）。"
                    ) from exc
        if any(rule_key not in active_rule_keys for rule_key in case.expected_rule_keys):
            raise RuleValidationError("Audit expectation references an inactive rule")
        if "GOLDEN" in case.case_id.upper():
            # 黄金用例是零命中基线，期望必须为空，由审计侧强制
            case.expected_rule_keys = []
        elif not case.expected_rule_keys:
            # 变异用例必须声明「这条用例要打死哪条规则」，否则它测不出任何东西，
            # 只是把 AI 的漏覆盖伪装成通过
            raise RuleValidationError(
                f"变异用例 {case.case_id} 必须声明至少一条 expected_rule_keys，否则无法判断它验证了什么"
            )
        for rule_key in case.expected_rule_keys:
            info = (rule_index or {}).get(rule_key)
            if not info:
                continue
            field, operator = info[0], info[1]
            params = info[2] if len(info) > 2 else {}
            if operator in SELF_FIELD_OPERATORS and field not in case.overrides:
                # 实测出现过「FIELD_ABSENT」这种用例：名字说字段缺失，overrides 里
                # 却根本没有这个键，于是继承了黄金值，什么都没变异，只是把该规则
                # 的漏测伪装成一条有效用例。JSON 表达不了「缺键」，字段缺失必须写 null。
                raise RuleValidationError(
                    f"用例 {case.case_id} 期望命中 {rule_key}，但 overrides 里没有 {field}，"
                    f"该字段并没有被变异，这条用例测不到任何东西。"
                    f"要测字段缺失请显式写成 \"{field}\": null。"
                )
            if field in case.overrides:
                check_val = case.overrides[field]
                if field in numeric_fields and operator == "REGEXP" and check_val is not None and check_val != "":
                    # Oracle NUMBER 列会去除前导零与尾随 .00（如 "00100" / "100.00" 入库后都是 100），
                    # 必须按 Oracle 规范化后的数字文本预判 REGEXP_LIKE，防止假阳性反例导致漏报
                    try:
                        dec = Decimal(str(check_val).strip().replace(",", "")).normalize()
                        norm_str = format(dec, "f")
                        if "." in norm_str:
                            norm_str = norm_str.rstrip("0").rstrip(".")
                        check_val = norm_str or "0"
                    except InvalidOperation:
                        pass
                verdict = violates_single_field_rule(operator, params, check_val)
                if verdict is False:
                    raise RuleValidationError(
                        f"用例 {case.case_id} 声称会触发 {rule_key}"
                        f"（{operator} {params}），但取值 {case.overrides[field]!r}（Oracle 实际评估值 {check_val!r}）并不违规，"
                        "这条用例跑到 Oracle 也不会命中，白白消耗一轮审计。请给出真正越界或非空违规的值。"
                    )
            if operator == "FIELD_COMPARE":
                right_field = params.get("right_field")
                if field not in case.overrides and right_field not in case.overrides:
                    raise RuleValidationError(
                        f"用例 {case.case_id} 期望命中 {rule_key}（{field} 与 {right_field} 的跨字段比较），"
                        f"但 overrides 既没有改 {field} 也没有改 {right_field}，两列都继承黄金值，测不到任何东西。"
                    )
                if golden_record is not None:
                    left = case.overrides.get(field, golden_record.get(field))
                    right = case.overrides.get(right_field, golden_record.get(right_field))
                    verdict = violates_field_compare(params.get("operator"), left, right)
                    if verdict is False:
                        raise RuleValidationError(
                            f"用例 {case.case_id} 声称会触发 {rule_key}（{operator} {params}），"
                            f"但变异后的 {field}={left!r} 与 {right_field}={right!r} 仍满足比较关系，"
                            "跑到 Oracle 也不会命中。请给出真正打破关系的取值。"
                        )
            if operator == "ARITHMETIC_BALANCE" and golden_record is not None:
                merged_rec = {**golden_record, **case.overrides}
                verdict = violates_arithmetic_balance(params, merged_rec)
                if verdict is False:
                    raise RuleValidationError(
                        f"用例 {case.case_id} 声称会触发 {rule_key}（ARITHMETIC_BALANCE），"
                        "但变异后记录仍满足算术平衡公式，跑到 Oracle 也不会命中。"
                    )
            if operator == "COND_VALUE" and golden_record is not None:
                merged_rec = {**golden_record, **case.overrides}
                verdict = violates_cond_value(params, merged_rec, field)
                if verdict is False:
                    raise RuleValidationError(
                        f"用例 {case.case_id} 声称会触发 {rule_key}（COND_VALUE），"
                        "但变异后记录并未触发该条件值域/互斥约束。"
                    )
    return cases
