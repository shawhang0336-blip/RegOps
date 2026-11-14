import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .domain import Evidence, RuleDefinition, SpecDocument, calculate_rule_hash
from .rules import RuleValidationError, validate_rule
from .spec_parser import field_map


class SchemaBinding(BaseModel):
    field: str
    storage_type: Literal["TEXT", "NUMBER"]
    length: int = Field(gt=0)
    decimal_places: int = Field(default=0, ge=0)


class GeneratorBinding(BaseModel):
    field: str
    generator: Literal["FIXED", "ENUM", "SEQUENCE"]
    params: Dict[str, Any]


class AuditCaseBinding(BaseModel):
    case_id: str
    overrides: Dict[str, Any] = Field(default_factory=dict)
    expected_rule_keys: List[str] = Field(default_factory=list)


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


def bind_generators(spec: SpecDocument, items: List[Dict[str, Any]]) -> List[GeneratorBinding]:
    bindings = [GeneratorBinding.model_validate(item) for item in items]
    expected = field_map(spec)
    if {item.field for item in bindings} != set(expected) or len(bindings) != len(expected):
        raise RuleValidationError("Generator assignment must cover every whitelisted field exactly once")
    for item in bindings:
        if item.generator == "FIXED" and "value" not in item.params:
            raise RuleValidationError(f"{item.field} FIXED generator requires a value")
        if item.generator == "ENUM" and not item.params.get("values"):
            raise RuleValidationError(f"{item.field} ENUM generator requires values")
        if item.generator == "SEQUENCE" and "start" not in item.params:
            raise RuleValidationError(f"{item.field} SEQUENCE generator requires start")
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
            padding = int(item.params.get("padding", 0))
            record[item.field] = str(value).zfill(padding) if padding else str(value)
    return record


def bind_audit_cases(
    spec: SpecDocument,
    items: List[Dict[str, Any]],
    active_rule_keys: set,
) -> List[AuditCaseBinding]:
    cases = [AuditCaseBinding.model_validate(item) for item in items]
    if not cases:
        raise RuleValidationError("At least one audit case is required")
    case_ids = set()
    for case in cases:
        if case.case_id in case_ids:
            raise RuleValidationError("Duplicate audit case ID")
        case_ids.add(case.case_id)
        if any(field not in field_map(spec) for field in case.overrides):
            raise RuleValidationError("Audit override is outside the field whitelist")
        if any(rule_key not in active_rule_keys for rule_key in case.expected_rule_keys):
            raise RuleValidationError("Audit expectation references an inactive rule")
    return cases
