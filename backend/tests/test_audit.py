from app.audit import run_independent_audit
from app.llm import ScriptedLlm
from app.rules import build_explicit_rules


def test_independent_cases_pass_for_explicit_rules(organization_spec):
    report = run_independent_audit(organization_spec, build_explicit_rules(organization_spec))
    assert report["all_passed"] is True
    assert report["case_count"] == 11
    assert report["false_positive_count"] == 0
    assert report["false_negative_count"] == 0


def test_approved_expanded_rule_changes_expected_set(organization_spec):
    rules = build_explicit_rules(organization_spec) + ScriptedLlm().propose(organization_spec)
    report = run_independent_audit(organization_spec, rules)
    postcode = next(case for case in report["cases"] if case["case_id"] == "POSTCODE_ALPHA")
    assert report["all_passed"] is True
    assert postcode["actual_rules"] == ["SAFE_6_1_POSTCODE_REGEXP_AI"]


def test_independent_oracle_catches_wrong_condition(organization_spec):
    rules = build_explicit_rules(organization_spec)
    conditional = next(rule for rule in rules if rule.operator == "COND_REQUIRED")
    conditional.params["if_operator"] = "EQ"
    report = run_independent_audit(organization_spec, rules)
    assert report["all_passed"] is False
    assert report["false_positive_count"] > 0
    assert report["false_negative_count"] > 0
