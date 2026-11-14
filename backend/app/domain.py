import hashlib
import json
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class FieldSpec(BaseModel):
    name: str
    content: str = ""
    data_type: str = "VARCHAR"
    length: int = 255
    decimal_places: Optional[int] = None
    check_text: str = ""
    evidence_page: str


class SpecDocument(BaseModel):
    version: str
    table_code: str
    table_name: str
    source_name: str
    source_sha256: str
    section: str
    fields: List[FieldSpec]
    terms_text: str
    dictionary_text: str


class Evidence(BaseModel):
    section: str
    page: str
    field: str
    quote: str


class RuleDefinition(BaseModel):
    rule_key: str
    table_code: str
    field: str
    operator: Literal[
        "REQUIRED",
        "ENUM",
        "NOT_IN",
        "COND_REQUIRED",
        "LENGTH",
        "LENGTH_MAX",
        "REGEXP",
        "FOREIGN_KEY",
    ]
    params: Dict[str, Any] = Field(default_factory=dict)
    source: Literal["DOC_EXPLICIT", "AI_EXPANDED"]
    evidence: Evidence
    rationale: str
    rule_hash: str = ""


class AuditCase(BaseModel):
    case_id: str
    record: Dict[str, Any]
    expected_rules: List[str]
    expanded_expectations: Dict[str, List[str]] = Field(default_factory=dict)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def calculate_rule_hash(rule: RuleDefinition, source_sha256: str) -> str:
    payload = rule.model_dump(exclude={"rule_hash"})
    payload["spec_sha256"] = source_sha256
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
