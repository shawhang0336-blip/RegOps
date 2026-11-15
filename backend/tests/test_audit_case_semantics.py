"""审计用例的期望集语义。

黄金用例是「零命中」基线，变异用例是「定向打死某条规则」。
这两个语义以前没区分：AI 手写的 expected_rule_keys 被当成全集断言，
写少了就判 false positive（实测 4 条用例把「置空同时违反必填和枚举」判成误报），
而写空了又能把漏覆盖伪装成通过（AI 就干过，REMARK 长度用例 expected=[]）。
"""

import pytest

from app.generic_domain import bind_audit_cases
from app.generic_domain import RuleValidationError


def _case(case_id, overrides, expected):
    return {"case_id": case_id, "overrides": overrides, "expected_rule_keys": expected}


def test_golden_case_expectation_is_forced_empty(organization_spec):
    cases = bind_audit_cases(
        organization_spec,
        [_case("SAFE_6_1_ORGANIZATION_GOLDEN_01", {}, ["SAFE_6_1_ANYTHING"])],
        {"SAFE_6_1_ANYTHING"},
    )
    assert cases[0].expected_rule_keys == []


def test_mutation_case_must_declare_what_it_kills(organization_spec):
    with pytest.raises(RuleValidationError):
        bind_audit_cases(
            organization_spec,
            [_case("SAFE_6_1_ORGANIZATION_MUT_01", {}, [])],
            {"SAFE_6_1_ANYTHING"},
        )


def test_mutation_case_with_single_expectation_is_kept_as_is(organization_spec):
    cases = bind_audit_cases(
        organization_spec,
        [_case("SAFE_6_1_ORGANIZATION_MUT_02", {}, ["SAFE_6_1_ANYTHING"])],
        {"SAFE_6_1_ANYTHING"},
    )
    assert cases[0].expected_rule_keys == ["SAFE_6_1_ANYTHING"]


def test_case_that_does_not_mutate_the_target_field_is_rejected(organization_spec):
    """实测出现过 FIELD_ABSENT 这种空转用例。

    名字说「字段缺失」，overrides 里却没有那个键——JSON 表达不了「缺键」，
    于是它继承了黄金值，什么都没变异，只是把该规则的漏测伪装成一条有效用例。
    """
    with pytest.raises(RuleValidationError):
        bind_audit_cases(
            organization_spec,
            [_case("SAFE_6_1_ORGANIZATION_MUT_03", {}, ["SAFE_6_1_ANYTHING"])],
            {"SAFE_6_1_ANYTHING"},
            {"SAFE_6_1_ANYTHING": ("CUSTOMERNAME", "REQUIRED")},
        )


def test_case_mutating_the_target_field_is_accepted(organization_spec):
    cases = bind_audit_cases(
        organization_spec,
        [_case("SAFE_6_1_ORGANIZATION_MUT_04", {"CREDITCODE": None}, ["SAFE_6_1_ANYTHING"])],
        {"SAFE_6_1_ANYTHING"},
        {"SAFE_6_1_ANYTHING": ("CREDITCODE", "REQUIRED")},
    )
    assert cases[0].overrides == {"CREDITCODE": None}


def test_cond_required_may_mutate_the_condition_field(organization_spec):
    """条件必填的变异可以落在条件字段上，不能要求它也改被约束字段。"""
    cases = bind_audit_cases(
        organization_spec,
        [_case("SAFE_6_1_ORGANIZATION_MUT_05", {"COUNTRYCODE": "USA"}, ["SAFE_6_1_ANYTHING"])],
        {"SAFE_6_1_ANYTHING"},
        {"SAFE_6_1_ANYTHING": ("CUSTOMERENGNAME", "COND_REQUIRED", {})},
    )
    assert cases[0].expected_rule_keys == ["SAFE_6_1_ANYTHING"]


def test_case_whose_value_does_not_actually_violate_is_rejected(organization_spec):
    """REMARK_LENGTH_MAX_C5 那种空转用例：声称超长，实际没超。

    这种用例跑到 Oracle 也不会命中，回流再去改规则又改不动（长度就是 256），
    白白消耗一整轮审计。所以在绑定阶段直接用 Python 判掉。
    """
    with pytest.raises(RuleValidationError):
        bind_audit_cases(
            organization_spec,
            [_case("SAFE_6_1_ORGANIZATION_MUT_06", {"REMARKS": "短备注"}, ["SAFE_6_1_ANYTHING"])],
            {"SAFE_6_1_ANYTHING"},
            {"SAFE_6_1_ANYTHING": ("REMARKS", "LENGTH_MAX", {"length": 256})},
        )


def test_case_with_genuinely_overlong_value_is_accepted(organization_spec):
    cases = bind_audit_cases(
        organization_spec,
        [_case("SAFE_6_1_ORGANIZATION_MUT_07", {"REMARKS": "x" * 300}, ["SAFE_6_1_ANYTHING"])],
        {"SAFE_6_1_ANYTHING"},
        {"SAFE_6_1_ANYTHING": ("REMARKS", "LENGTH_MAX", {"length": 256})},
    )
    assert len(cases[0].overrides["REMARKS"]) == 300


def test_enum_value_inside_whitelist_is_rejected(organization_spec):
    with pytest.raises(RuleValidationError):
        bind_audit_cases(
            organization_spec,
            [_case("SAFE_6_1_ORGANIZATION_MUT_08", {"COUNTRYCODE": "CHN"}, ["SAFE_6_1_ANYTHING"])],
            {"SAFE_6_1_ANYTHING"},
            {"SAFE_6_1_ANYTHING": ("COUNTRYCODE", "ENUM", {"values": ["CHN", "USA"]})},
        )


def test_regexp_matching_value_is_rejected(organization_spec):
    with pytest.raises(RuleValidationError):
        bind_audit_cases(
            organization_spec,
            [_case("SAFE_6_1_ORGANIZATION_MUT_09", {"REGDATE": "20240101"}, ["SAFE_6_1_ANYTHING"])],
            {"SAFE_6_1_ANYTHING"},
            {"SAFE_6_1_ANYTHING": ("REGDATE", "REGEXP", {"pattern": r"^\d{8}$"})},
        )
