import pytest

from app.domain import Evidence, RuleDefinition, calculate_rule_hash
from app.llm import ScriptedLlm, bind_rule_candidate
from app.rules import RuleValidationError, build_explicit_rules, validate_rule


def test_real_pdf_yields_field_whitelist_and_evidence(organization_spec):
    assert len(organization_spec.fields) == 33
    assert organization_spec.fields[0].name == "CUSTCODE"
    assert organization_spec.fields[-1].name == "FAX"
    assert len(organization_spec.source_sha256) == 64
    country = next(field for field in organization_spec.fields if field.name == "COUNTRYCODE")
    assert country.length == 3
    assert "CHN" in country.check_text
    english = next(field for field in organization_spec.fields if field.name == "CUSTOMERENGNAME")
    assert english.content == "组织机构英文名称"
    assert "非中国时必填" in english.check_text
    industry = next(field for field in organization_spec.fields if field.name == "INDUSTRYCODE")
    assert "国家外汇管理局" not in industry.check_text


def test_explicit_rules_have_source_backed_hashes(organization_spec):
    rules = build_explicit_rules(organization_spec)
    assert len(rules) == 12
    assert all(rule.source == "DOC_EXPLICIT" for rule in rules)
    assert len({rule.rule_hash for rule in rules}) == 12
    for rule in rules:
        assert rule.rule_hash == calculate_rule_hash(rule, organization_spec.source_sha256)


def test_candidate_factory_cannot_set_explicit_source(organization_spec):
    candidates = ScriptedLlm().propose(organization_spec)
    assert len(candidates) == 1
    assert candidates[0].source == "AI_EXPANDED"
    assert candidates[0].field == "POSTCODE"


def test_unknown_field_is_rejected(organization_spec):
    with pytest.raises(KeyError):
        bind_rule_candidate(
            organization_spec,
            {"field": "HALLUCINATED_FIELD", "operator": "REQUIRED", "params": {}},
        )


def test_tampered_explicit_evidence_is_rejected(organization_spec):
    rule = build_explicit_rules(organization_spec)[0]
    rule.evidence = Evidence(section="6.1.3", page="89", field=rule.field, quote="伪造原文")
    with pytest.raises(RuleValidationError):
        validate_rule(rule, organization_spec)
