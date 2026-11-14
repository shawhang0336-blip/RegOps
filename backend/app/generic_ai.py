import json
import threading
import time
from typing import Any, Dict, Optional, Tuple

import httpx
from json_repair import repair_json

from .config import Settings
from .domain import SpecDocument

LLM_CALL_SEMAPHORE = threading.BoundedSemaphore(4)

FIELD_PROPERTIES = {
    "field": {"type": "string"},
    "storage_type": {"type": "string", "enum": ["TEXT", "NUMBER"]},
    "length": {"type": "integer"},
    "decimal_places": {"type": "integer"},
}

RULE_PROPERTIES = {
    "rule_key": {"type": "string"},
    "table_code": {"type": "string"},
    "field": {"type": "string"},
    "operator": {
        "type": "string",
        "enum": ["REQUIRED", "ENUM", "NOT_IN", "COND_REQUIRED", "LENGTH", "LENGTH_MAX", "REGEXP", "FOREIGN_KEY"],
    },
    "params": {"type": "object"},
    "evidence_kind": {"type": "string", "enum": ["DICTIONARY", "TERM_INFERENCE"]},
    "rationale": {"type": "string"},
}

METADATA_FIELD_PROPERTIES = {
    "field_cn": {"type": "string"},
    "field_en": {"type": "string"},
    "field_definition": {"type": "string"},
    "check_text_raw": {"type": "string"},
    "data_type": {"type": "string", "enum": ["VARCHAR", "NUMBER"]},
    "length": {"type": "integer"},
    "decimal_places": {"type": "integer"},
    "required": {"type": "boolean"},
    "code_reference_text": {"type": "string"},
    "confidence": {"type": "number"},
    "evidence_json": {"type": "object"},
}

METADATA_TABLE_PROPERTIES = {
    "table_code": {"type": "string"},
    "table_name": {"type": "string"},
    "table_definition": {"type": "string"},
    "fields": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": METADATA_FIELD_PROPERTIES,
            "required": ["field_cn", "field_en", "data_type", "check_text_raw"],
        },
    },
}

CODE_VALUE_PROPERTIES = {
    "value": {"type": "string"},
    "label_cn": {"type": "string"},
    "label_en": {"type": "string"},
    "definition": {"type": "string"},
}

CODE_SET_PROPERTIES = {
    "code": {"type": "string"},
    "name": {"type": "string"},
    "definition": {"type": "string"},
    "values": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": CODE_VALUE_PROPERTIES,
            "required": ["value", "label_cn"],
        },
    },
}

TOOL_SCHEMAS = {
    "inspect_document_structure": {
        "type": "object",
        "properties": {
            "document_kind": {
                "type": "string",
                "enum": ["COLLECTION_SPEC", "STATISTICAL_SYSTEM", "RULE_DOCUMENT", "REVISION_DOCUMENT", "CODE_VALUES", "MIXED", "UNKNOWN"],
            },
            "summary": {"type": "string"},
            "chunking_strategy": {"type": "string", "enum": ["DOCUMENT_CATALOG", "WORD_TABLE_AND_PARAGRAPH", "SHEET_PER_TABLE", "MANUAL_REVIEW"]},
            "expected_contents": {"type": "array", "items": {"type": "string", "enum": ["TABLE_METADATA", "CODE_SET", "RULE_EVIDENCE", "REVISION_DIFF", "NARRATIVE"]}},
            "requires_human": {"type": "boolean"},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["document_kind", "summary", "chunking_strategy", "expected_contents", "requires_human", "warnings"],
    },
    "assign_document_structure": {
        "type": "object",
        "properties": {
            "blocks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source_block_ids": {"type": "array", "items": {"type": "string"}},
                        "role": {"type": "string", "enum": ["TABLE_METADATA", "CODE_SET", "NARRATIVE", "IGNORE"]},
                        "title": {"type": "string"},
                        "column_mapping": {"type": "object"},
                        "human_required": {"type": "boolean"},
                        "warnings": {"type": "array", "items": {"type": "string"}},
                        "split_markers": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["source_block_ids", "role", "title", "column_mapping", "human_required", "warnings", "split_markers"],
                },
            },
            "global_warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["blocks", "global_warnings"],
    },
    "assign_block_segments": {
        "type": "object",
        "properties": {
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "segment_type": {"type": "string", "enum": ["TERMINOLOGY", "XML_FORMAT", "DATA_DICTIONARY", "CODE_VALUES", "PRIMARY_KEY", "NARRATIVE", "IGNORE"]},
                        "title": {"type": "string"},
                        "start_marker": {"type": "string"},
                        "end_marker": {"type": "string"},
                        "requires_human": {"type": "boolean"},
                        "warnings": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["segment_type", "title", "start_marker", "end_marker", "requires_human", "warnings"],
                },
            }
        },
        "required": ["segments"],
    },
    "assign_generation_plan": {
        "type": "object",
        "properties": {
            "mandatory_rule_target": {"type": "integer"},
            "expanded_rule_target": {"type": "integer"},
            "golden_count": {"type": "integer"},
            "mutations_per_rule": {"type": "integer"},
            "rationale": {"type": "string"},
        },
        "required": ["mandatory_rule_target", "expanded_rule_target", "golden_count", "mutations_per_rule", "rationale"],
    },
    "assign_metadata": {
        "type": "object",
        "properties": METADATA_TABLE_PROPERTIES,
        "required": ["table_code", "table_name", "fields"],
    },
    "review_metadata": {
        "type": "object",
        "properties": {
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string", "enum": ["ERROR", "WARNING", "INFO"]},
                        "code": {"type": "string"},
                        "field_en": {"type": "string"},
                        "message": {"type": "string"},
                    },
                    "required": ["severity", "code", "field_en", "message"],
                },
            }
        },
        "required": ["issues"],
    },
    "assign_code_set": {
        "type": "object",
        "properties": CODE_SET_PROPERTIES,
        "required": list(CODE_SET_PROPERTIES),
    },
    "review_code_set": {
        "type": "object",
        "properties": {
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string", "enum": ["ERROR", "WARNING", "INFO"]},
                        "code": {"type": "string"},
                        "value": {"type": "string"},
                        "message": {"type": "string"},
                    },
                    "required": ["severity", "code", "value", "message"],
                },
            }
        },
        "required": ["issues"],
    },
    "assign_schema": {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {"type": "object", "properties": FIELD_PROPERTIES, "required": list(FIELD_PROPERTIES)},
            }
        },
        "required": ["fields"],
    },
    "assign_rules": {
        "type": "object",
        "properties": {
            "rules": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": RULE_PROPERTIES,
                    "required": ["rule_key", "field", "operator", "params", "evidence_kind", "rationale"],
                },
            }
        },
        "required": ["rules"],
    },
    "assign_generators": {
        "type": "object",
        "properties": {
            "generators": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "generator": {"type": "string", "enum": ["FIXED", "ENUM", "SEQUENCE"]},
                        "params": {"type": "object"},
                    },
                    "required": ["field", "generator", "params"],
                },
            }
        },
        "required": ["generators"],
    },
    "assign_audit_cases": {
        "type": "object",
        "properties": {
            "cases": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "case_id": {"type": "string"},
                        "overrides": {"type": "object"},
                        "expected_rule_keys": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["case_id", "overrides", "expected_rule_keys"],
                },
            }
        },
        "required": ["cases"],
    },
}


def spec_payload(spec: SpecDocument) -> Dict[str, Any]:
    return {
        "table_code": spec.table_code,
        "table_name": spec.table_name,
        "section": spec.section,
        "fields": [field.model_dump() for field in spec.fields],
        "terms_text": spec.terms_text[:6000],
    }


class RealDesignLlm:
    def __init__(self, settings: Settings) -> None:
        if not settings.llm_api_key:
            raise RuntimeError("LLM_API_KEY is required")
        self.settings = settings

    def assign(
        self,
        tool_name: str,
        instruction: str,
        payload: Dict[str, Any],
        feedback: Optional[str] = None,
        timeout: int = 90,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        schema = TOOL_SCHEMAS[tool_name]
        prompt = (
            "你是监管报送装配器，只能调用指定工具进行结构化参数赋值，禁止输出 SQL。"
            "所有字段必须来自输入白名单。DICTIONARY 仅用于数据字典明确表达的规则；"
            "术语推理、额外格式限制或不确定拓展必须使用 TERM_INFERENCE。"
            "算子参数契约：REQUIRED={}；ENUM/NOT_IN={values:[...]}；"
            "COND_REQUIRED={if_field,if_operator:EQ|NE,if_values:[...]}；LENGTH={length:整数}；"
            "LENGTH={length:精确长度}；LENGTH_MAX={length:最大长度}；REGEXP={pattern:正则}；FOREIGN_KEY={references_table,references_field}。"
            f"任务：{instruction}\n输入：{json.dumps(payload, ensure_ascii=False)}"
        )
        if feedback:
            prompt += f"\n上次参数被确定性门禁拒绝，请修正：{feedback}"
        started_at = time.monotonic()
        try:
            with LLM_CALL_SEMAPHORE:
                response = httpx.post(
                    f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.llm_api_key}"},
                    json={
                        "model": self.settings.llm_model,
                        "messages": [
                            {"role": "system", "content": "只进行工具参数赋值，不输出自由文本。"},
                            {"role": "user", "content": prompt},
                        ],
                        "tools": [{"type": "function", "function": {"name": tool_name, "description": instruction, "parameters": schema}}],
                        "tool_choice": {"type": "function", "function": {"name": tool_name}},
                        "temperature": 0,
                        "thinking": {"type": "low"},
                        "max_tokens": 8192,
                    },
                    timeout=timeout,
                )
        except httpx.HTTPError as exc:
            elapsed = round(time.monotonic() - started_at, 2)
            raise RuntimeError(f"{type(exc).__name__} after {elapsed}s; request_chars={len(prompt)}") from exc
        if response.is_error:
            error = response.json().get("error", {})
            elapsed = round(time.monotonic() - started_at, 2)
            raise RuntimeError(
                f"LLM HTTP {response.status_code} {error.get('code', '')}: {error.get('message', '')}; elapsed={elapsed}s"
            )
        body = response.json()
        choice = body["choices"][0]
        message = choice["message"]
        calls = message.get("tool_calls") or []
        if not calls:
            diagnostic = {
                "request_id": body.get("id"),
                "finish_reason": choice.get("finish_reason"),
                "content_chars": len(message.get("content") or ""),
                "reasoning_chars": len(message.get("reasoning_content") or ""),
                "request_chars": len(prompt),
                "usage": body.get("usage"),
                "elapsed_seconds": round(time.monotonic() - started_at, 2),
            }
            raise RuntimeError(f"Model returned no tool call: {json.dumps(diagnostic, ensure_ascii=False)}")
        arguments: Dict[str, Any] = {}
        json_repaired = False
        for call in calls:
            if call["function"]["name"] != tool_name:
                raise RuntimeError("Model returned an unexpected tool")
            raw_arguments = call["function"]["arguments"]
            try:
                current = json.loads(raw_arguments)
            except json.JSONDecodeError:
                current = repair_json(raw_arguments, return_objects=True)
                json_repaired = True
            for key, value in current.items():
                if isinstance(value, list):
                    arguments.setdefault(key, []).extend(value)
                else:
                    arguments[key] = value
        trace = {
            "provider": "GLM",
            "model": self.settings.llm_model,
            "request_id": body.get("id"),
            "tool_name": tool_name,
            "tool_call_count": len(calls),
            "json_repaired": json_repaired,
            "http_status": response.status_code,
            "request_chars": len(prompt),
            "elapsed_seconds": round(time.monotonic() - started_at, 2),
            "arguments": arguments,
        }
        return arguments, trace
