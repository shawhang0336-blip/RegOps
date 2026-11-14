import copy
import re
import sqlite3
from typing import Any, Dict, Iterable, List

from .compiler import compile_rule_query, quote_identifier
from .domain import AuditCase, RuleDefinition, SpecDocument

POSTCODE_RULE = "SAFE_6_1_POSTCODE_REGEXP_AI"

BASE_RECORD: Dict[str, Any] = {
    "record_id": "GOLDEN_CN",
    "CUSTNAME": "示例测试机构",
    "INDUSTRYCODE": "J662",
    "ATTRCODE": "110",
    "AREACODE": "110000",
    "COUNTRYCODE": "CHN",
    "CUSTOMERENGNAME": "",
    "ISTAXFREE": "N",
    "RPTMETHOD": "2",
    "CREDITCODE": "91110000100001043K",
    "REGDATE": "20200101",
    "POSTCODE": "100000",
}


def _case(case_id: str, expected: List[str], **changes: Any) -> AuditCase:
    record = copy.deepcopy(BASE_RECORD)
    record.update(changes)
    record["record_id"] = case_id
    return AuditCase(case_id=case_id, record=record, expected_rules=expected)


def independent_audit_cases() -> List[AuditCase]:
    cases = [
        AuditCase(case_id="GOLDEN_CN", record=copy.deepcopy(BASE_RECORD), expected_rules=[]),
        _case(
            "GOLDEN_FOREIGN",
            [],
            COUNTRYCODE="USA",
            CUSTOMERENGNAME="EXAMPLE ORGANIZATION",
        ),
        _case("MISSING_NAME", ["SAFE_6_1_CUSTNAME_REQUIRED"], CUSTNAME=""),
        _case("MISSING_INDUSTRY", ["SAFE_6_1_INDUSTRYCODE_REQUIRED"], INDUSTRYCODE=None),
        _case(
            "MISSING_ENGLISH_NAME",
            ["SAFE_6_1_CUSTOMERENGNAME_COND_REQUIRED"],
            COUNTRYCODE="USA",
            CUSTOMERENGNAME="",
        ),
        _case("AREA_NOT_ALLOWED", ["SAFE_6_1_AREACODE_NOT_IN"], AREACODE="100000"),
        _case("INVALID_TAX_FREE", ["SAFE_6_1_ISTAXFREE_ENUM"], ISTAXFREE="X"),
        _case("INVALID_RPT_METHOD", ["SAFE_6_1_RPTMETHOD_ENUM"], RPTMETHOD="9"),
        _case("SHORT_CREDIT_CODE", ["SAFE_6_1_CREDITCODE_LENGTH"], CREDITCODE="12345678901234567"),
        _case("SHORT_REG_DATE", ["SAFE_6_1_REGDATE_FORMAT"], REGDATE="2020010"),
    ]
    postcode_case = _case("POSTCODE_ALPHA", [], POSTCODE="10A000")
    postcode_case.expanded_expectations[POSTCODE_RULE] = [POSTCODE_RULE]
    cases.append(postcode_case)
    return cases


def run_independent_audit(spec: SpecDocument, rules: Iterable[RuleDefinition]) -> Dict[str, Any]:
    active_rules = list(rules)
    active_keys = {rule.rule_key for rule in active_rules}
    connection = sqlite3.connect(":memory:")
    connection.create_function("REGEXP", 2, lambda pattern, value: 1 if re.search(pattern, value or "") else 0)
    try:
        columns = ", ".join(f"{quote_identifier(field.name)} TEXT" for field in spec.fields)
        connection.execute(
            f"CREATE TABLE {quote_identifier(spec.table_code)} "
            f"(`record_id` VARCHAR(64) PRIMARY KEY, {columns})"
        )
        cases = independent_audit_cases()
        insert_columns = ["record_id"] + [field.name for field in spec.fields]
        insert_sql = (
            f"INSERT INTO {quote_identifier(spec.table_code)} "
            f"({', '.join(quote_identifier(name) for name in insert_columns)}) "
            f"VALUES ({', '.join('?' for _ in insert_columns)})"
        )
        for case in cases:
            connection.execute(insert_sql, [case.record.get(name) for name in insert_columns])

        hits: Dict[str, List[str]] = {case.case_id: [] for case in cases}
        for rule in active_rules:
            sql, params = compile_rule_query(rule)
            for row in connection.execute(sql, params).fetchall():
                hits[row[1]].append(row[0])

        results = []
        false_positives = 0
        false_negatives = 0
        for case in cases:
            expected = set(case.expected_rules)
            for rule_key, expected_when_active in case.expanded_expectations.items():
                if rule_key in active_keys:
                    expected.update(expected_when_active)
            actual = set(hits[case.case_id])
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            false_negatives += len(missing)
            false_positives += len(unexpected)
            results.append(
                {
                    "case_id": case.case_id,
                    "expected_rules": sorted(expected),
                    "actual_rules": sorted(actual),
                    "missing_rules": missing,
                    "unexpected_rules": unexpected,
                    "passed": not missing and not unexpected,
                }
            )
        return {
            "all_passed": false_positives == 0 and false_negatives == 0,
            "case_count": len(cases),
            "active_rule_count": len(active_rules),
            "false_positive_count": false_positives,
            "false_negative_count": false_negatives,
            "cases": results,
            "coverage_statement": "仅证明独立编制的当前用例集通过，不代表监管语义完备或全部边界覆盖。",
        }
    finally:
        connection.close()


INCOME_RPTNO = "110000123401010101N001"
CROSS_RPTNO_RULE = "SAFE_CROSS_INCOME_RPTNO_FK"

INCOME_BASE_RECORD: Dict[str, Any] = {
    "record_id": "BASE_GOLDEN_CORP",
    "ACTIONTYPE": "A",
    "ACTIONDESC": "",
    "RPTNO": INCOME_RPTNO,
    "CUSTYPE": "C",
    "IDCODE": "",
    "CUSTCOD": "91110000100001043K",
    "CUSTNM": "示例收款企业",
    "OPPUSER": "（JW）EXAMPLE[acc:000000]",
    "TXCCY": "USD",
    "TXAMT": 100,
    "EXRATE": 0,
    "LCYAMT": 0,
    "LCYACC": "",
    "FCYAMT": 100,
    "FCYACC": "SAFE001",
    "OTHAMT": 0,
    "OTHACC": "",
    "METHOD": "T",
    "BUSCODE": "BUS2026001",
    "INCHARGECCY": "",
    "INCHARGEAMT": None,
    "OUTCHARGECCY": "",
    "OUTCHARGEAMT": None,
}

INCOME_DECL_RECORD: Dict[str, Any] = {
    "record_id": "DECL_GOLDEN",
    "ACTIONTYPE": "A",
    "ACTIONDESC": "",
    "RPTNO": INCOME_RPTNO,
    "COUNTRY": "USA",
    "PAYTYPE": "A",
    "TXCODE": "101010",
    "TC1AMT": 100,
    "TXREM": "示例交易附言",
    "TXCODE2": "",
    "TC2AMT": None,
    "TX2REM": "",
    "ISREF": "N",
    "BILLNO": "N/A",
    "PAYATTR": "F",
    "CRTUSER": "测试员",
    "INPTELC": "13800000000",
    "RPTDATE": "20260101",
}


def _income_case(case_id: str, expected: List[str], **changes: Any) -> AuditCase:
    record = copy.deepcopy(INCOME_BASE_RECORD)
    record.update(changes)
    record["record_id"] = case_id
    return AuditCase(case_id=case_id, record=record, expected_rules=expected)


def _declaration_case(case_id: str, expected: List[str], **changes: Any) -> AuditCase:
    record = copy.deepcopy(INCOME_DECL_RECORD)
    record.update(changes)
    record["record_id"] = case_id
    return AuditCase(case_id=case_id, record=record, expected_rules=expected)


def bundle_audit_cases() -> Dict[str, List[AuditCase]]:
    organization = independent_audit_cases()
    base = [
        AuditCase(case_id="BASE_GOLDEN_CORP", record=copy.deepcopy(INCOME_BASE_RECORD), expected_rules=[]),
        _income_case("BASE_GOLDEN_PERSON", [], CUSTYPE="D", IDCODE="ID123456", CUSTCOD=""),
        _income_case("BASE_INVALID_CUSTYPE", ["SAFE_6_2_1_CUSTYPE_ENUM"], CUSTYPE="X", IDCODE="ID123456"),
        _income_case("BASE_MISSING_IDCODE", ["SAFE_6_2_1_IDCODE_COND_REQUIRED"], CUSTYPE="D", IDCODE="", CUSTCOD=""),
        _income_case("BASE_MISSING_CUSTCOD", ["SAFE_6_2_1_CUSTCOD_COND_REQUIRED"], CUSTCOD=""),
        _income_case("BASE_MISSING_CUSTNM", ["SAFE_6_2_1_CUSTNM_REQUIRED"], CUSTNM=""),
        _income_case("BASE_MISSING_OPPUSER", ["SAFE_6_2_1_OPPUSER_REQUIRED"], OPPUSER=None),
        _income_case("BASE_MISSING_TXCCY", ["SAFE_6_2_1_TXCCY_REQUIRED"], TXCCY=""),
        _income_case("BASE_MISSING_TXAMT", ["SAFE_6_2_1_TXAMT_REQUIRED"], TXAMT=None),
        _income_case("BASE_INVALID_METHOD", ["SAFE_6_2_1_METHOD_ENUM"], METHOD="X"),
        _income_case("BASE_MISSING_BUSCODE", ["SAFE_6_2_1_BUSCODE_REQUIRED"], BUSCODE=""),
    ]
    declaration = [
        AuditCase(case_id="DECL_GOLDEN", record=copy.deepcopy(INCOME_DECL_RECORD), expected_rules=[]),
        _declaration_case("DECL_MISSING_COUNTRY", ["SAFE_6_2_2_COUNTRY_REQUIRED"], COUNTRY=""),
        _declaration_case("DECL_INVALID_PAYTYPE", ["SAFE_6_2_2_PAYTYPE_ENUM"], PAYTYPE="X"),
        _declaration_case("DECL_MISSING_TXCODE", ["SAFE_6_2_2_TXCODE_REQUIRED"], TXCODE=""),
        _declaration_case("DECL_MISSING_TC1AMT", ["SAFE_6_2_2_TC1AMT_REQUIRED"], TC1AMT=None),
        _declaration_case("DECL_MISSING_TXREM", ["SAFE_6_2_2_TXREM_REQUIRED"], TXREM=""),
        _declaration_case("DECL_INVALID_ISREF", ["SAFE_6_2_2_ISREF_ENUM"], ISREF="X"),
        _declaration_case("DECL_MISSING_CRTUSER", ["SAFE_6_2_2_CRTUSER_REQUIRED"], CRTUSER=""),
        _declaration_case("DECL_MISSING_INPTELC", ["SAFE_6_2_2_INPTELC_REQUIRED"], INPTELC=""),
        _declaration_case("DECL_MISSING_RPTDATE", ["SAFE_6_2_2_RPTDATE_REQUIRED"], RPTDATE=""),
        _declaration_case("DECL_ORPHAN_RPTNO", [CROSS_RPTNO_RULE], RPTNO="110000123401010101N999"),
    ]
    return {
        "SAFE_6_1_ORGANIZATION": organization,
        "SAFE_6_2_1_INCOME_BASE": base,
        "SAFE_6_2_2_INCOME_DECL": declaration,
    }


def expected_case_rules(case: AuditCase, active_keys: set) -> set:
    expected = set(case.expected_rules)
    for rule_key, expanded in case.expanded_expectations.items():
        if rule_key in active_keys:
            expected.update(expanded)
    return expected


def run_sqlite_bundle_audit(specs: List[SpecDocument], rules: Iterable[RuleDefinition]) -> Dict[str, Any]:
    active_rules = list(rules)
    active_keys = {rule.rule_key for rule in active_rules}
    cases_by_table = bundle_audit_cases()
    connection = sqlite3.connect(":memory:")
    connection.create_function("REGEXP", 2, lambda pattern, value: 1 if re.search(pattern, value or "") else 0)
    try:
        for spec in specs:
            columns = ", ".join(f"{quote_identifier(field.name)} TEXT" for field in spec.fields)
            connection.execute(
                f"CREATE TABLE {quote_identifier(spec.table_code)} "
                f"(`record_id` VARCHAR(64) PRIMARY KEY, {columns})"
            )
            names = ["record_id"] + [field.name for field in spec.fields]
            statement = (
                f"INSERT INTO {quote_identifier(spec.table_code)} "
                f"({', '.join(quote_identifier(name) for name in names)}) "
                f"VALUES ({', '.join('?' for _ in names)})"
            )
            for case in cases_by_table[spec.table_code]:
                connection.execute(statement, [case.record.get(name) for name in names])
        hits: Dict[str, Dict[str, List[str]]] = {
            code: {case.case_id: [] for case in cases} for code, cases in cases_by_table.items()
        }
        for rule in active_rules:
            sql, params = compile_rule_query(rule)
            for row in connection.execute(sql, params).fetchall():
                hits[rule.table_code][row[1]].append(row[0])
        return build_bundle_report(specs, active_rules, cases_by_table, hits)
    finally:
        connection.close()


def build_bundle_report(
    specs: List[SpecDocument],
    active_rules: List[RuleDefinition],
    cases_by_table: Dict[str, List[AuditCase]],
    hits: Dict[str, Dict[str, List[str]]],
) -> Dict[str, Any]:
    active_keys = {rule.rule_key for rule in active_rules}
    results = []
    false_positives = 0
    false_negatives = 0
    for spec in specs:
        for case in cases_by_table[spec.table_code]:
            expected = expected_case_rules(case, active_keys)
            actual = set(hits[spec.table_code][case.case_id])
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            false_negatives += len(missing)
            false_positives += len(unexpected)
            results.append(
                {
                    "table_code": spec.table_code,
                    "case_id": case.case_id,
                    "expected_rules": sorted(expected),
                    "actual_rules": sorted(actual),
                    "missing_rules": missing,
                    "unexpected_rules": unexpected,
                    "passed": not missing and not unexpected,
                }
            )
    return {
        "all_passed": false_positives == 0 and false_negatives == 0,
        "case_count": len(results),
        "table_count": len(specs),
        "active_rule_count": len(active_rules),
        "false_positive_count": false_positives,
        "false_negative_count": false_negatives,
        "cases": results,
        "coverage_statement": "仅证明独立编制的当前多表用例集通过，不代表监管语义完备或全部边界覆盖。",
    }
