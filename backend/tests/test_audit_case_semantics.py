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
