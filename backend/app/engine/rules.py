import re
from typing import Any, Dict, Iterable, List

from app.infra.domain import Evidence, RuleDefinition, SpecDocument, calculate_rule_hash, field_map


class RuleValidationError(ValueError):
    pass


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
        if rule.params.get("if_field") not in fields:
            raise RuleValidationError("Unknown condition field")
        if rule.params.get("if_operator") not in {"EQ", "NE"}:
            raise RuleValidationError("Unsupported condition operator")
        if not rule.params.get("if_values"):
            raise RuleValidationError("Condition values cannot be empty")
    if rule.operator in {"ENUM", "NOT_IN"} and not rule.params.get("values"):
        raise RuleValidationError("Enum values cannot be empty")
    if rule.operator in {"LENGTH", "LENGTH_MAX"} and int(rule.params.get("length", 0)) <= 0:
        raise RuleValidationError("Length must be positive")
    if rule.operator == "NUM_RANGE":
        min_value = rule.params.get("min")
        max_value = rule.params.get("max")
        if min_value is None and max_value is None:
            raise RuleValidationError("NUM_RANGE requires at least one of min/max")
        try:
            min_float = float(min_value) if min_value is not None else None
            max_float = float(max_value) if max_value is not None else None
        except (TypeError, ValueError) as exc:
            raise RuleValidationError("NUM_RANGE bounds must be numeric") from exc
        if min_float is not None and max_float is not None and min_float > max_float:
            raise RuleValidationError("NUM_RANGE min cannot exceed max")
    if rule.operator == "FIELD_COMPARE":
        if rule.params.get("right_field") not in fields:
            raise RuleValidationError("Unknown comparison field")
        if rule.params.get("right_field") == rule.field:
            raise RuleValidationError("FIELD_COMPARE cannot compare a field with itself")
        if rule.params.get("operator") not in {"LT", "LE", "GT", "GE", "EQ", "NE"}:
            raise RuleValidationError("Unsupported comparison operator")
    if rule.operator == "REGEXP":
        try:
            re.compile(rule.params["pattern"])
        except (KeyError, re.error) as exc:
            raise RuleValidationError("Invalid regular expression") from exc
    if rule.evidence.field not in fields:
        raise RuleValidationError("Evidence field is outside the whitelist")
    if rule.source == "DOC_EXPLICIT" and rule.evidence.quote != fields[rule.evidence.field].check_text:
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
