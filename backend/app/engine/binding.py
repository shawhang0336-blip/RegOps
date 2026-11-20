import re
from typing import Any, Dict, List, Optional

from app.infra.domain import (
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
from app.engine.rules import RuleValidationError, validate_rule


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
        params = item.get("params", {})
        if operator == "FOREIGN_KEY":
            if selected_table_codes is None or params.get("references_table") not in selected_table_codes:
                raise RuleValidationError("Cross-rule reference is outside the confirmed table set")
            if params.get("references_field") != field:
                raise RuleValidationError("This slice requires matching cross-table field names")
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
    """把 JSON 里写成字符串的数字收敛成数值。

    实测出现过 SEQUENCE 的 start/step 都写成 "1000.00" / "100.00"，
    于是 index=1 时 `"1000.00" + 1 * "100.00"` 走成了字符串拼接，
    生成出 "1000.00100.00"，插 NUMBER 列直接 ORA-01722 整次审计失败。
    千分位逗号（"1,000.00"）视为合法数字，入库前由 fit_sandbox_values 清洗。
    """
    if isinstance(value, bool):
        raise ValueError("布尔值不是数字")
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip().replace(",", "")
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    if re.fullmatch(r"[+-]?\d+\.\d+", text):
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
            value = item.params["start"] + index * item.params.get("step", 1)
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
        if value is None or params.get("length") is None:
            return None
        size = len(str(value))
        if operator == "LENGTH":
            return size != int(params["length"])
        return size > int(params["length"])
    if operator == "REGEXP":
        pattern = params.get("pattern")
        if not pattern or value is None:
            return None
        try:
            # Oracle 的 REGEXP_LIKE 是「包含匹配」，用 search 对齐
            return re.search(pattern, str(value)) is None
        except re.error:
            return None
    if operator == "NUM_RANGE":
        if value is None or str(value).strip() == "":
            return None
        try:
            number = float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return None
        min_value = params.get("min")
        max_value = params.get("max")
        if min_value is not None:
            bound = float(min_value)
            if number < bound or (not params.get("min_inclusive", True) and number == bound):
                return True
        if max_value is not None:
            bound = float(max_value)
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

    能转数值就按数值比（与生产 NUMBER 列一致），转不了按字符串序
    （与沙箱/生产字符列的字典序比较一致）。任一侧缺失或关系未知返回 None。
    """
    relation = _COMPARE_RELATIONS.get(operator)
    if relation is None or left is None or right is None:
        return None
    try:
        left_value = float(str(left).replace(",", ""))
        right_value = float(str(right).replace(",", ""))
    except (TypeError, ValueError):
        left_value, right_value = str(left), str(right)
    try:
        return not relation(left_value, right_value)
    except TypeError:
        return None


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

        if case.case_id in case_ids:
            raise RuleValidationError("Duplicate audit case ID")
        case_ids.add(case.case_id)
        if any(field not in field_map(spec) for field in case.overrides):
            raise RuleValidationError("Audit override is outside the field whitelist")
        # 沙箱数值列是原生 NUMBER：塞非数字会在入库时 ORA-01722 挂掉整批，绑定阶段就拦下
        for override_field, override_value in case.overrides.items():
            if override_field in numeric_fields and override_value is not None:
                try:
                    float(str(override_value).replace(",", ""))
                except (TypeError, ValueError) as exc:
                    raise RuleValidationError(
                        f"用例 {case.case_id} 把数值字段 {override_field} 变异成非数字 {override_value!r}，"
                        "沙箱数值列无法入库。数值字段的违规请用越界数值（如 0、负数、超大数）。"
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
                verdict = violates_single_field_rule(operator, params, case.overrides[field])
                if verdict is False:
                    raise RuleValidationError(
                        f"用例 {case.case_id} 声称会触发 {rule_key}"
                        f"（{operator} {params}），但取值 {case.overrides[field]!r} 并不违规，"
                        "这条用例跑到 Oracle 也不会命中，白白消耗一轮审计。请给出真正越界或缺失的值。"
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
    return cases
