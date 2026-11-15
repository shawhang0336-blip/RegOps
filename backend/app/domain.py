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


# LLM 允许使用的算子子集，不含 LENGTH（精确长度）。
#
# 字典/表结构里的「字符型，N」是列宽上限，`VARCHAR2(N)` 本身也只约束上限，
# 短值插得进去。把它卡成「必须正好 N 位」会把业务里所有短值判成违规——
# 报送场景下误报比漏报更致命，正常数据会被自己的校验拦住发不出去。
# 实测同一份原文两次运行，一次出 LENGTH=28 一次出 LENGTH_MAX=28，答案本应唯一，
# 所以干脆不给模型这个选择；确实需要「必须正好 N 位」时用 REGEXP 写死格式。
LLM_OPERATORS = (
    "REQUIRED",
    "ENUM",
    "NOT_IN",
    "COND_REQUIRED",
    "LENGTH_MAX",
    "REGEXP",
    "FOREIGN_KEY",
)


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
