import json
import re
from typing import Any, Dict, List, Optional

import httpx

from .config import Settings
from .domain import Evidence, RuleDefinition, SpecDocument, calculate_rule_hash
from .rules import validate_rule
from .spec_parser import field_map


class LlmConfigurationError(RuntimeError):
    pass


def bind_rule_candidate(spec: SpecDocument, arguments: Dict[str, Any]) -> RuleDefinition:
    field = arguments["field"]
    metadata = field_map(spec)[field]
    terms_compact = re.sub(r"\s+", "", spec.terms_text)
    term_index = terms_compact.find(metadata.content)
    term_tail = terms_compact[term_index:] if term_index >= 0 else ""
    term_end = term_tail.find("。")
    term_quote = term_tail[: term_end + 1] if term_end >= 0 else term_tail[:160]
    rule = RuleDefinition(
        rule_key=f"{'SAFE_6_1' if spec.table_code == 'SAFE_6_1_ORGANIZATION' else spec.table_code}_{field}_{arguments['operator']}_AI",
        table_code=spec.table_code,
        field=field,
        operator=arguments["operator"],
        params=arguments.get("params", {}),
        source="AI_EXPANDED",
        evidence=Evidence(
            section=f"{spec.section}.1/{spec.section}.3",
            page=metadata.evidence_page,
            field=field,
            quote=f"{term_quote}｜数据字典：{metadata.check_text}",
        ),
        rationale=arguments.get("rationale", ""),
    )
    rule.rule_hash = calculate_rule_hash(rule, spec.source_sha256)
    validate_rule(rule, spec)
    return rule


class ScriptedLlm:
    def propose(self, spec: SpecDocument, feedback: Optional[str] = None) -> List[RuleDefinition]:
        if feedback or spec.table_code != "SAFE_6_1_ORGANIZATION":
            return []
        return [
            bind_rule_candidate(
                spec,
                {
                    "field": "POSTCODE",
                    "operator": "REGEXP",
                    "params": {"pattern": "^[0-9]{6}$"},
                    "rationale": "由邮政编码术语及六位字符长度拓展为六位数字；原文未明文规定仅允许数字，必须人工确权。",
                },
            )
        ]


class OpenAICompatibleLlm:
    def __init__(self, settings: Settings) -> None:
        if not settings.llm_api_key:
            raise LlmConfigurationError("LLM_API_KEY is required for live model calls")
        self.base_url = settings.llm_base_url.rstrip("/")
        self.model = settings.llm_model
        self.api_key = settings.llm_api_key

    def propose(self, spec: SpecDocument, feedback: Optional[str] = None) -> List[RuleDefinition]:
        fields = [
            {
                "name": field.name,
                "content": field.content,
                "type": f"{field.data_type}({field.length})",
                "check": field.check_text,
            }
            for field in spec.fields
        ]
        tool = {
            "type": "function",
            "function": {
                "name": "bind_rule_candidate",
                "description": "绑定一条监管拓展规则候选；禁止生成 SQL，所有结果都需人工审批。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "enum": [field.name for field in spec.fields]},
                        "operator": {
                            "type": "string",
                            "enum": ["REQUIRED", "ENUM", "NOT_IN", "COND_REQUIRED", "LENGTH", "REGEXP"],
                        },
                        "params": {"type": "object"},
                        "rationale": {"type": "string"},
                    },
                    "required": ["field", "operator", "params", "rationale"],
                },
            },
        }
        prompt = (
            "你是监管规则候选装配器。只可调用工具，不得输出 SQL。"
            "只能从字段白名单选择字段。不要把数据字典已经明文规定的规则作为拓展规则重复提交。"
            "拓展必须谨慎，并说明原文没有直接规定的部分。\n"
            f"字段白名单：{json.dumps(fields, ensure_ascii=False)}\n"
            f"术语解释：{spec.terms_text[:16000]}"
        )
        if feedback:
            prompt += f"\n上一轮独立沙箱诊断：{feedback}"
        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "你只生成结构化工具调用，不生成自由文本 SQL。"},
                    {"role": "user", "content": prompt},
                ],
                "tools": [tool],
                "tool_choice": "auto",
                "temperature": 0,
            },
            timeout=90,
        )
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        candidates = []
        for call in message.get("tool_calls") or []:
            if call["function"]["name"] != "bind_rule_candidate":
                continue
            arguments = json.loads(call["function"]["arguments"])
            candidates.append(bind_rule_candidate(spec, arguments))
        return candidates


def build_llm(settings: Settings, requested_provider: str):
    if requested_provider == "scripted":
        return ScriptedLlm()
    if requested_provider == "live":
        return OpenAICompatibleLlm(settings)
    raise LlmConfigurationError(f"Unknown LLM provider: {requested_provider}")
