import math
import re
from typing import Any, Dict, Iterable, List

from app.infra.contracts import Evidence, RuleDefinition, SpecDocument, calculate_rule_hash, field_map


class RuleValidationError(ValueError):
    pass


_BRACKET_LITERAL_ESCAPES = set(r".+/()_*?,;:@#$!~=%")


def normalize_oracle_regexp(pattern: str) -> str:
    r"""将正则字符类 [...] 规范化为 Oracle POSIX REGEXP_LIKE 兼容写法（同时保持 Python re 语义完全等价）。

    Oracle POSIX 字符类内反斜杠不作转义：
    - `\-` 会被解析为从 `\` 到后续字符的非法范围（ORA-12728），需将 `-` 移至字符类末尾；
    - `\d`/`\s`/`\w` 在 [...] 内会被当成字面反斜杠和字母，需展开为 `0-9`/` `/`A-Za-z0-9_`；
    - `\.` / `\+` 等多余转义在 [...] 内会多匹配反斜杠本身，需去反斜杠。
    """
    if not pattern or "[" not in pattern:
        return pattern
    out = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "\\" and i + 1 < n:
            out.append(pattern[i : i + 2])
            i += 2
            continue
        if ch != "[":
            out.append(ch)
            i += 1
            continue
        j = i + 1
        negated = False
        if j < n and pattern[j] == "^":
            negated = True
            j += 1
        leading_rbracket = False
        if j < n and pattern[j] == "]":
            leading_rbracket = True
            j += 1
        body = []
        trailing_hyphen = False
        closed = False
        while j < n:
            c = pattern[j]
            if c == "[" and j + 1 < n and pattern[j + 1] in (":", ".", "="):
                delim = pattern[j + 1]
                end = pattern.find(f"{delim}]", j + 2)
                if end != -1:
                    body.append(pattern[j : end + 2])
                    j = end + 2
                    continue
            if c == "]":
                closed = True
                j += 1
                break
            if c == "\\" and j + 1 < n:
                nxt = pattern[j + 1]
                if nxt == "-":
                    trailing_hyphen = True
                    j += 2
                    continue
                if nxt == "d":
                    body.append("0-9")
                    j += 2
                    continue
                if nxt == "w":
                    body.append("A-Za-z0-9_")
                    j += 2
                    continue
                if nxt == "s":
                    body.append(" ")
                    j += 2
                    continue
                if nxt in _BRACKET_LITERAL_ESCAPES:
                    body.append(nxt)
                    j += 2
                    continue
                body.append(pattern[j : j + 2])
                j += 2
                continue
            body.append(c)
            j += 1
        if not closed:
            out.append(pattern[i:])
            break
        rebuilt = "["
        if negated:
            rebuilt += "^"
        if leading_rbracket:
            rebuilt += "]"
        rebuilt += "".join(body)
        if trailing_hyphen:
            rebuilt += "-"
        rebuilt += "]"
        out.append(rebuilt)
        i = j
    return "".join(out)


_POSIX_CLASS_TO_PYTHON = {
    "digit": "0-9",
    "alpha": "A-Za-z",
    "alnum": "A-Za-z0-9",
    "upper": "A-Z",
    "lower": "a-z",
    "space": r" \t\r\n\f\v",
    "blank": r" \t",
    "xdigit": "0-9A-Fa-f",
    "punct": r"!-/:-@\[-`{-~",
}


def posix_classes_to_python(pattern: str) -> str:
    """把 Oracle POSIX 字符类（`[[:digit:]]`）翻译成 Python `re` 等价写法，供 Python 侧预判与 Oracle 语义对齐。

    Python `re` 不认识 `[:digit:]`，会把 `[[:digit:]]` 解析成「`[:digt` 中任一字符 + `]`」，
    导致对 `^[[:digit:]]{8}$` 这类正则的违规预判完全相反。
    """
    if not isinstance(pattern, str) or "[:" not in pattern:
        return pattern
    return re.sub(
        r"\[:([a-z]+):\]",
        lambda m: _POSIX_CLASS_TO_PYTHON.get(m.group(1), m.group(0)),
        pattern,
    )


def _explicit_rule(
    spec: SpecDocument,
    rule_key: str,
    field: str,
    operator: str,
    params: Dict[str, Any],
    rationale: str,
) -> RuleDefinition:
    metadata = field_map(spec)[field]
    rule = RuleDefinition(
        rule_key=rule_key,
        table_code=spec.table_code,
        field=field,
        operator=operator,
        params=params,
        source="DOC_EXPLICIT",
        evidence=Evidence(
            section=f"{spec.section}.3",
            page=metadata.evidence_page,
            field=field,
            quote=metadata.check_text,
        ),
        rationale=rationale,
    )
    rule.rule_hash = calculate_rule_hash(rule, spec.source_sha256)
    return rule


def build_organization_rules(spec: SpecDocument) -> List[RuleDefinition]:
    required_fields = {
        "CUSTNAME": "机构名称为必填项",
        "INDUSTRYCODE": "行业属性代码为必填项",
        "ATTRCODE": "经济类型代码为必填项",
        "AREACODE": "住所/营业场所代码为必填项",
        "COUNTRYCODE": "常驻国家代码为必填项",
        "ISTAXFREE": "是否特殊经济区内企业为必填项",
    }
    rules = [
        _explicit_rule(
            spec,
            f"SAFE_6_1_{field}_REQUIRED",
            field,
            "REQUIRED",
            {},
            rationale,
        )
        for field, rationale in required_fields.items()
    ]
    rules.extend(
        [
            _explicit_rule(
                spec,
                "SAFE_6_1_ISTAXFREE_ENUM",
                "ISTAXFREE",
                "ENUM",
                {"values": ["Y", "N"]},
                "是否特殊经济区内企业仅允许 Y 或 N",
            ),
            _explicit_rule(
                spec,
                "SAFE_6_1_AREACODE_NOT_IN",
                "AREACODE",
                "NOT_IN",
                {"values": ["100000"]},
                "住所/营业场所代码不能选择 100000",
            ),
            _explicit_rule(
                spec,
                "SAFE_6_1_CUSTOMERENGNAME_COND_REQUIRED",
                "CUSTOMERENGNAME",
                "COND_REQUIRED",
                {"if_field": "COUNTRYCODE", "if_operator": "NE", "if_values": ["CHN"]},
                "常驻国家（地区）为非中国时组织机构英文名称必填",
            ),
            _explicit_rule(
                spec,
                "SAFE_6_1_CREDITCODE_LENGTH",
                "CREDITCODE",
                "LENGTH",
                {"length": 18},
                "统一社会信用代码填写时应为 18 位",
            ),
            _explicit_rule(
                spec,
                "SAFE_6_1_REGDATE_FORMAT",
                "REGDATE",
                "REGEXP",
                {"pattern": "^[0-9]{8}$"},
                "注册/开业成立日期采用 YYYYMMDD 八位格式",
            ),
            _explicit_rule(
                spec,
                "SAFE_6_1_RPTMETHOD_ENUM",
                "RPTMETHOD",
                "ENUM",
                {"values": ["1", "2"]},
                "申报方式仅允许 1 或 2",
            ),
        ]
    )
    for rule in rules:
        validate_rule(rule, spec)
    return rules


def validate_rule(rule: RuleDefinition, spec: SpecDocument) -> None:
    fields = field_map(spec)
    if rule.table_code != spec.table_code:
        raise RuleValidationError("Rule table is outside the active specification")
    if rule.field not in fields:
        raise RuleValidationError(f"Unknown field: {rule.field}")
    if rule.operator == "COND_REQUIRED":
        if_field = rule.params.get("if_field")
        if if_field not in fields:
            raise RuleValidationError("Unknown condition field")
        if if_field == rule.field:
            raise RuleValidationError("COND_REQUIRED cannot condition a field on itself")
        if rule.params.get("if_operator") not in {"EQ", "NE"}:
            raise RuleValidationError("Unsupported condition operator")
        if not rule.params.get("if_values"):
            raise RuleValidationError("Condition values cannot be empty")
    if rule.operator == "COND_VALUE":
        if_field = rule.params.get("if_field")
        if if_field not in fields:
            raise RuleValidationError("Unknown condition field")
        if if_field == rule.field:
            raise RuleValidationError("COND_VALUE cannot condition a field on itself")
        if rule.params.get("if_operator") not in {"EQ", "NE"}:
            raise RuleValidationError("Unsupported condition operator")
        if not rule.params.get("if_values"):
            raise RuleValidationError("Condition values cannot be empty")
        then_op = rule.params.get("then_operator")
        if then_op not in {"MUST_BE_NULL", "ENUM", "NOT_IN", "NUM_RANGE"}:
            raise RuleValidationError("COND_VALUE then_operator must be MUST_BE_NULL, ENUM, NOT_IN, or NUM_RANGE")
        if then_op in {"ENUM", "NOT_IN"}:
            then_vals = rule.params.get("then_values")
            if then_vals is None:
                then_vals = rule.params.get("values")
            if not then_vals:
                raise RuleValidationError(f"COND_VALUE({then_op}) requires non-empty then_values")
        if then_op == "NUM_RANGE":
            if (fields[rule.field].data_type or "").upper() != "NUMBER":
                raise RuleValidationError(f"COND_VALUE(NUM_RANGE) requires numeric target field: {rule.field}")
            min_v = rule.params.get("min")
            max_v = rule.params.get("max")
            if min_v is None and max_v is None:
                raise RuleValidationError("COND_VALUE(NUM_RANGE) requires at least one of min/max")
            try:
                min_f = float(min_v) if min_v is not None else None
                max_f = float(max_v) if max_v is not None else None
            except (TypeError, ValueError) as exc:
                raise RuleValidationError("COND_VALUE(NUM_RANGE) bounds must be numeric") from exc
            if (min_f is not None and not math.isfinite(min_f)) or (max_f is not None and not math.isfinite(max_f)):
                raise RuleValidationError("COND_VALUE(NUM_RANGE) bounds must be finite numbers")
            if min_f is not None and max_f is not None:
                min_inc = bool(rule.params.get("min_inclusive", True))
                max_inc = bool(rule.params.get("max_inclusive", True))
                if min_f > max_f or (min_f == max_f and (not min_inc or not max_inc)):
                    raise RuleValidationError("COND_VALUE(NUM_RANGE) interval is empty or min exceeds max")
    if rule.operator == "ARITHMETIC_BALANCE":
        terms = rule.params.get("terms")
        if not isinstance(terms, list) or not terms:
            raise RuleValidationError("ARITHMETIC_BALANCE requires a non-empty terms list")
        balance_op = rule.params.get("operator", "EQ")
        if balance_op not in {"EQ", "GE", "LE", "GT", "LT"}:
            raise RuleValidationError("ARITHMETIC_BALANCE operator must be EQ, GE, LE, GT, or LT")
        term_fields = set()
        for term in terms:
            if not isinstance(term, dict):
                raise RuleValidationError("Each ARITHMETIC_BALANCE term must be an object with field and coef")
            tf = term.get("field")
            if tf not in fields:
                raise RuleValidationError(f"Unknown arithmetic term field: {tf}")
            if (fields[tf].data_type or "").upper() != "NUMBER":
                raise RuleValidationError(f"ARITHMETIC_BALANCE term field must be NUMBER: {tf}")
            try:
                coef = float(term.get("coef", 1))
            except (TypeError, ValueError) as exc:
                raise RuleValidationError(f"Invalid coef for term {tf}") from exc
            if not math.isfinite(coef) or coef == 0:
                raise RuleValidationError(f"ARITHMETIC_BALANCE term coef must be finite and non-zero: {tf}")
            term_fields.add(tf)
        target_field = rule.params.get("target_field")
        target_value = rule.params.get("target_value")
        if target_field is None and target_value is None:
            raise RuleValidationError("ARITHMETIC_BALANCE requires target_field or target_value")
        if target_field is not None:
            if target_field not in fields:
                raise RuleValidationError(f"Unknown arithmetic target_field: {target_field}")
            if (fields[target_field].data_type or "").upper() != "NUMBER":
                raise RuleValidationError(f"ARITHMETIC_BALANCE target_field must be NUMBER: {target_field}")
            if len(terms) == 1 and target_field in term_fields and float(terms[0].get("coef", 1)) == 1:
                raise RuleValidationError("ARITHMETIC_BALANCE single-term 1*X vs X is trivial")
        if target_value is not None:
            try:
                tv_f = float(target_value)
            except (TypeError, ValueError) as exc:
                raise RuleValidationError("ARITHMETIC_BALANCE target_value must be numeric") from exc
            if not math.isfinite(tv_f):
                raise RuleValidationError("ARITHMETIC_BALANCE target_value must be finite")
        if rule.params.get("tolerance") is not None:
            try:
                tol = float(rule.params["tolerance"])
            except (TypeError, ValueError) as exc:
                raise RuleValidationError("ARITHMETIC_BALANCE tolerance must be numeric") from exc
            if not math.isfinite(tol) or tol < 0:
                raise RuleValidationError("ARITHMETIC_BALANCE tolerance must be finite and non-negative")
        participants = set(term_fields)
        if target_field:
            participants.add(target_field)
        if rule.field not in participants:
            raise RuleValidationError("ARITHMETIC_BALANCE rule.field must participate in terms or target_field")
    if rule.operator == "UNIQUE_KEY":
        key_fields = rule.params.get("key_fields") or [rule.field]
        if not isinstance(key_fields, list) or not key_fields:
            raise RuleValidationError("UNIQUE_KEY requires non-empty key_fields")
        for kf in key_fields:
            if kf not in fields:
                raise RuleValidationError(f"Unknown UNIQUE_KEY field: {kf}")
        if rule.field not in key_fields:
            raise RuleValidationError("UNIQUE_KEY rule.field must be included in key_fields")
    if rule.operator in {"ENUM", "NOT_IN"} and not rule.params.get("values"):
        raise RuleValidationError("Enum values cannot be empty")
    if rule.operator in {"LENGTH", "LENGTH_MAX"}:
        try:
            length_val = int(rule.params.get("length", 0))
        except (TypeError, ValueError) as exc:
            raise RuleValidationError("Length must be a positive integer") from exc
        if length_val <= 0:
            raise RuleValidationError("Length must be positive")
    if rule.operator == "NUM_RANGE":
        if (fields[rule.field].data_type or "").upper() != "NUMBER":
            raise RuleValidationError(f"NUM_RANGE requires numeric field: {rule.field}")
        min_value = rule.params.get("min")
        max_value = rule.params.get("max")
        if min_value is None and max_value is None:
            raise RuleValidationError("NUM_RANGE requires at least one of min/max")
        try:
            min_float = float(min_value) if min_value is not None else None
            max_float = float(max_value) if max_value is not None else None
        except (TypeError, ValueError) as exc:
            raise RuleValidationError("NUM_RANGE bounds must be numeric") from exc
        if (min_float is not None and not math.isfinite(min_float)) or (
            max_float is not None and not math.isfinite(max_float)
        ):
            raise RuleValidationError("NUM_RANGE bounds must be finite numbers")
        if min_float is not None and max_float is not None:
            min_inc = bool(rule.params.get("min_inclusive", True))
            max_inc = bool(rule.params.get("max_inclusive", True))
            if min_float > max_float or (min_float == max_float and (not min_inc or not max_inc)):
                raise RuleValidationError("NUM_RANGE interval is empty or min exceeds max")
    if rule.operator == "FIELD_COMPARE":
        right_name = rule.params.get("right_field")
        if right_name not in fields:
            raise RuleValidationError("Unknown comparison field")
        if right_name == rule.field:
            raise RuleValidationError("FIELD_COMPARE cannot compare a field with itself")
        cmp_op = rule.params.get("operator")
        if cmp_op not in {"LT", "LE", "GT", "GE", "EQ", "NE"}:
            raise RuleValidationError("Unsupported comparison operator")
        left_meta = fields[rule.field]
        right_meta = fields[right_name]
        left_type = (left_meta.data_type or "").upper()
        right_type = (right_meta.data_type or "").upper()
        if left_type != right_type:
            raise RuleValidationError(
                f"FIELD_COMPARE 不能跨数据类型比较：{rule.field}({left_type}) vs {right_name}({right_type})"
            )
        if left_type == "VARCHAR" and cmp_op == "EQ":
            l_len = int(left_meta.length or 0)
            r_len = int(right_meta.length or 0)
            if l_len > 0 and r_len > 0 and min(l_len, r_len) == 1 and max(l_len, r_len) >= 8:
                raise RuleValidationError(
                    f"FIELD_COMPARE(EQ) 不合理：{rule.field}(长度{l_len}) 与 {right_name}(长度{r_len}) 长度量级悬殊，"
                    "不能将单字符代码与长编码/编号整列判等；若原文要求与另一字段某一位/子串含义一致，请勿生成 FIELD_COMPARE(EQ)。"
                )
    if rule.operator == "REGEXP":
        pattern = rule.params.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise RuleValidationError("Invalid regular expression")
        if any(tok in pattern for tok in ("(?=", "(?!", "(?<=", "(?<!", "(?:")):
            raise RuleValidationError(
                "Oracle REGEXP_LIKE 不支持前后瞻或非捕获分组 (?= / (?! / (?:，请改用标准 POSIX 正则或配合 NOT_IN/ENUM 规则。"
            )
        try:
            re.compile(pattern)
        except (KeyError, re.error) as exc:
            raise RuleValidationError("Invalid regular expression") from exc
    if rule.evidence.field not in fields:
        raise RuleValidationError("Evidence field is outside the whitelist")
    if rule.source == "DOC_EXPLICIT":
        evidence_field = fields[rule.evidence.field]
        allowed_quotes = {evidence_field.check_text}
        if rule.operator in {"LENGTH", "LENGTH_MAX"}:
            allowed_quotes.add(f"{evidence_field.name} 类型（长度）列：{evidence_field.data_type}({evidence_field.length})")
            want = (evidence_field.name or "").lower()
            for line in (spec.dictionary_text or "").splitlines():
                if want and want in line.lower() and str(evidence_field.length) in line:
                    allowed_quotes.add(" ".join(line.split()))
                    break
        if rule.evidence.quote not in allowed_quotes:
            raise RuleValidationError("Explicit-rule evidence does not match the parsed source")


def validate_rules(rules: Iterable[RuleDefinition], spec: SpecDocument) -> None:
    seen = set()
    for rule in rules:
        validate_rule(rule, spec)
        if rule.rule_hash in seen:
            raise RuleValidationError(f"Duplicate rule hash: {rule.rule_hash}")
        seen.add(rule.rule_hash)


def build_income_base_rules(spec: SpecDocument) -> List[RuleDefinition]:
    definitions = [
        ("SAFE_6_2_1_CUSTYPE_ENUM", "CUSTYPE", "ENUM", {"values": ["C", "D", "F"]}, "收款人类型仅允许 C/D/F"),
        ("SAFE_6_2_1_IDCODE_COND_REQUIRED", "IDCODE", "COND_REQUIRED", {"if_field": "CUSTYPE", "if_operator": "NE", "if_values": ["C"]}, "非对公用户时个人身份证件号码必填"),
        ("SAFE_6_2_1_CUSTCOD_COND_REQUIRED", "CUSTCOD", "COND_REQUIRED", {"if_field": "CUSTYPE", "if_operator": "EQ", "if_values": ["C"]}, "对公用户时主体标识码必填"),
        ("SAFE_6_2_1_CUSTNM_REQUIRED", "CUSTNM", "REQUIRED", {}, "收款人名称必填"),
        ("SAFE_6_2_1_OPPUSER_REQUIRED", "OPPUSER", "REQUIRED", {}, "付款人名称必填"),
        ("SAFE_6_2_1_TXCCY_REQUIRED", "TXCCY", "REQUIRED", {}, "收入款币种必填"),
        ("SAFE_6_2_1_TXAMT_REQUIRED", "TXAMT", "REQUIRED", {}, "收入款金额必填"),
        ("SAFE_6_2_1_METHOD_ENUM", "METHOD", "ENUM", {"values": ["L", "G", "C", "T", "D", "M", "O"]}, "结算方式仅允许规范列出的七类码值"),
        ("SAFE_6_2_1_BUSCODE_REQUIRED", "BUSCODE", "REQUIRED", {}, "银行业务编号必填"),
    ]
    rules = [_explicit_rule(spec, *definition) for definition in definitions]
    for rule in rules:
        validate_rule(rule, spec)
    return rules


def build_income_declaration_rules(spec: SpecDocument) -> List[RuleDefinition]:
    definitions = [
        ("SAFE_6_2_2_COUNTRY_REQUIRED", "COUNTRY", "REQUIRED", {}, "付款人常驻国家/地区代码必填"),
        ("SAFE_6_2_2_PAYTYPE_ENUM", "PAYTYPE", "ENUM", {"values": ["A", "R", "O"]}, "收款性质仅允许 A/R/O"),
        ("SAFE_6_2_2_TXCODE_REQUIRED", "TXCODE", "REQUIRED", {}, "交易编码 1 必填"),
        ("SAFE_6_2_2_TC1AMT_REQUIRED", "TC1AMT", "REQUIRED", {}, "相应金额 1 必填"),
        ("SAFE_6_2_2_TXREM_REQUIRED", "TXREM", "REQUIRED", {}, "交易附言 1 必填"),
        ("SAFE_6_2_2_ISREF_ENUM", "ISREF", "ENUM", {"values": ["Y", "N"]}, "是否保税货物项下收汇仅允许 Y/N"),
        ("SAFE_6_2_2_CRTUSER_REQUIRED", "CRTUSER", "REQUIRED", {}, "填报人必填"),
        ("SAFE_6_2_2_INPTELC_REQUIRED", "INPTELC", "REQUIRED", {}, "填报人电话必填"),
        ("SAFE_6_2_2_RPTDATE_REQUIRED", "RPTDATE", "REQUIRED", {}, "申报日期必填"),
    ]
    rules = [_explicit_rule(spec, *definition) for definition in definitions]
    for rule in rules:
        validate_rule(rule, spec)
    return rules


def build_explicit_rules(spec: SpecDocument) -> List[RuleDefinition]:
    builders = {
        "SAFE_6_1_ORGANIZATION": build_organization_rules,
        "SAFE_6_2_1_INCOME_BASE": build_income_base_rules,
        "SAFE_6_2_2_INCOME_DECL": build_income_declaration_rules,
    }
    if spec.table_code not in builders:
        raise RuleValidationError(f"No explicit-rule catalog for {spec.table_code}")
    return builders[spec.table_code](spec)


def build_cross_table_rules(specs: List[SpecDocument]) -> List[RuleDefinition]:
    by_code = {spec.table_code: spec for spec in specs}
    declaration = by_code["SAFE_6_2_2_INCOME_DECL"]
    base = by_code["SAFE_6_2_1_INCOME_BASE"]
    evidence = field_map(declaration)["RPTNO"]
    rule = RuleDefinition(
        rule_key="SAFE_CROSS_INCOME_RPTNO_FK",
        table_code=declaration.table_code,
        field="RPTNO",
        operator="FOREIGN_KEY",
        params={"references_table": base.table_code, "references_field": "RPTNO"},
        source="DOC_EXPLICIT",
        evidence=Evidence(
            section="6.2.2.3",
            page=evidence.evidence_page,
            field="RPTNO",
            quote=evidence.check_text,
        ),
        rationale="涉外收入申报信息的申报号码应与基础数据一致",
    )
    rule.rule_hash = calculate_rule_hash(rule, declaration.source_sha256)
    validate_rule(rule, declaration)
    if rule.params["references_field"] not in field_map(base):
        raise RuleValidationError("Cross-table reference field is outside the whitelist")
    return [rule]
