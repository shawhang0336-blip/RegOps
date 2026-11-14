import pytest

from app.generic_domain import GeneratorBinding, materialize_golden_record
from app.generic_workflow import _validate_plan
from app.rules import RuleValidationError


def test_ai_generation_plan_respects_budget():
    budget = {"max_rules_per_table": 10, "max_cases_per_table": 20}
    plan = _validate_plan(
        {
            "mandatory_rule_target": 3,
            "expanded_rule_target": 2,
            "golden_count": 4,
            "mutations_per_rule": 2,
            "rationale": "test",
        },
        budget,
    )
    assert plan["golden_count"] == 4
    with pytest.raises(RuleValidationError):
        _validate_plan(
            {
                "mandatory_rule_target": 9,
                "expanded_rule_target": 2,
                "golden_count": 1,
                "mutations_per_rule": 1,
                "rationale": "over budget",
            },
            budget,
        )


def test_sequence_generator_creates_distinct_records(organization_spec):
    generators = [GeneratorBinding(field="CUSTCODE", generator="SEQUENCE", params={"start": 7, "step": 2, "padding": 3})]
    assert materialize_golden_record(organization_spec, generators, 0)["CUSTCODE"] == "007"
    assert materialize_golden_record(organization_spec, generators, 1)["CUSTCODE"] == "009"
