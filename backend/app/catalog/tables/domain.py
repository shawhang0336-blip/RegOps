import re
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field, model_validator


class MetadataFieldDraft(BaseModel):
    field_cn: str = Field(min_length=1)
    field_en: str = Field(min_length=1)
    field_definition: str = ""
    check_text_raw: str = ""
    data_type: Literal["VARCHAR", "NUMBER"] = "VARCHAR"
    length: int = Field(default=128, gt=0)
    decimal_places: int = Field(default=0, ge=0)
    required: bool = False
    code_reference_text: str = ""
    confidence: float = Field(default=0, ge=0, le=1)
    evidence_json: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def default_length_from_type(cls, value):
        if isinstance(value, dict) and not value.get("length"):
            return {**value, "length": 22 if str(value.get("data_type", "VARCHAR")).upper() == "NUMBER" else 128}
        return value


class MetadataTableDraft(BaseModel):
    table_code: str = Field(min_length=1)
    table_name: str = Field(min_length=1)
    table_definition: str = ""
    fields: List[MetadataFieldDraft]


class MetadataValidationError(ValueError):
    pass


def validate_metadata_draft(draft: MetadataTableDraft) -> List[Dict[str, Any]]:
    issues = []
    seen = set()
    low_confidence = []
    for field in draft.fields:
        key_ci = field.field_en.strip().upper()
        if key_ci in seen:
            issues.append({"severity": "ERROR", "code": "DUPLICATE_FIELD", "field_en": field.field_en})
        seen.add(key_ci)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", field.field_en):
            issues.append({"severity": "ERROR", "code": "INVALID_FIELD_NAME", "field_en": field.field_en})
        if field.data_type == "NUMBER" and field.decimal_places > field.length:
            issues.append({"severity": "ERROR", "code": "INVALID_PRECISION", "field_en": field.field_en})
        if not field.evidence_json:
            issues.append({"severity": "WARNING", "code": "MISSING_EVIDENCE", "field_en": field.field_en})
        if field.confidence < 0.7:
            low_confidence.append(field.field_en)
    if low_confidence:
        issues.append({
            "severity": "WARNING",
            "code": "LOW_CONFIDENCE",
            "field_en": low_confidence[0] if len(low_confidence) == 1 else "",
            "message": f"{len(low_confidence)} 个字段的识别置信度较低，请对照原文核对字段名、类型和长度。",
        })
    if not draft.fields:
        issues.append({"severity": "ERROR", "code": "EMPTY_TABLE"})
    return issues


def require_valid_metadata(draft: MetadataTableDraft) -> None:
    errors = [issue for issue in validate_metadata_draft(draft) if issue["severity"] == "ERROR"]
    if errors:
        raise MetadataValidationError(str(errors))
