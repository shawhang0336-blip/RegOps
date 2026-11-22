"""受控 Tool-Call 业务契约与作用域前置说明（基础设施层 Layer 0）。

从 `client.py`（纯 HTTP 协议传输与连接池）剥离出的全部 12 个受控工具 JSON Schema 与最小前置 Prompt，
便于按算子/业务契约独立演进并与 `contracts.py` 的 `LLM_OPERATORS` 严格对齐。
"""

from typing import Any, Dict

from app.infra.contracts import LLM_OPERATORS, SpecDocument

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
        # 只暴露 LLM_OPERATORS：不含 LENGTH（列宽是上限，不是定长）
        "enum": list(LLM_OPERATORS),
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
            "chunking_strategy": {
                "type": "string",
                "enum": [
                    "SAFE_SPEC_PARSER",
                    "HEADING_SECTIONS",
                    "PAGE_WINDOWS",
                    "DOCUMENT_CATALOG",
                    "WORD_TABLE_AND_PARAGRAPH",
                    "SHEET_PER_TABLE",
                    "MANUAL_REVIEW",
                ],
            },
            "section_regions": {
                "type": "array",
                "description": "可选切块导航区域：按 survey 中的真实标题或工作表名标注各段角色（TABLE_METADATA/CODE_SET/RULE_EVIDENCE/NARRATIVE/IGNORE），供程序精准切块并跳过目录封面等无效区。",
                "items": {
                    "type": "object",
                    "properties": {
                        "start_heading": {"type": "string", "description": "必须逐字取自 survey.headings 或 sheet 名"},
                        "end_heading": {"type": "string", "description": "可选，区域结束标题（不含该标题本身）"},
                        "role": {"type": "string", "enum": ["TABLE_METADATA", "CODE_SET", "RULE_EVIDENCE", "NARRATIVE", "IGNORE"]},
                        "reason": {"type": "string"},
                    },
                    "required": ["start_heading", "role"],
                },
            },
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
                        "severity": {
                            "type": "string",
                            "enum": ["ERROR", "WARNING", "INFO"],
                            "description": "只有来源原文明确写出该要求时才可用 ERROR；凭业务常识推断的一律用 WARNING。",
                        },
                        "code": {"type": "string"},
                        "field_en": {"type": "string"},
                        "message": {"type": "string"},
                        "evidence": {
                            "type": "string",
                            "description": "支持这条判断的来源原文片段（逐字摘录）。原文没有相关内容时填 NO_SOURCE。",
                        },
                    },
                    "required": ["severity", "code", "field_en", "message", "evidence"],
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
            },
            "golden_overrides": {
                "type": "object",
                "description": "跨表外键场景下按 {table_code: {field: value}} 对齐各表黄金样本引用值（单表场景可省略）。",
            },
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
        "field_names": [field.name for field in spec.fields],
        "terms_text": spec.terms_text[:6000],
    }


_BASE_PROMPT = "你是监管报送装配器，只能调用指定工具进行结构化参数赋值，禁止输出 SQL 或自由文本。"

_RULE_OPERATOR_PROMPT = (
    "所有字段必须来自输入白名单。DICTIONARY 仅用于数据字典明确表达的规则；"
    "术语推理、额外格式限制或不确定拓展必须使用 TERM_INFERENCE。"
    "算子参数契约：REQUIRED={}；ENUM/NOT_IN={values:[...]}；"
    "COND_REQUIRED={if_field,if_operator:EQ|NE,if_values:[...]}；"
    "COND_VALUE={if_field,if_operator:EQ|NE|IN|NOT_IN,if_values:[...],then_operator:ENUM|NOT_IN|REGEXP|LENGTH_MAX|NUM_RANGE,then_values/pattern/length/min/max}；"
    "LENGTH_MAX={length:最大长度}；REGEXP={pattern:正则}；FOREIGN_KEY={references_table,references_field}；"
    "NUM_RANGE={min,max,min_inclusive,max_inclusive}（数值上下限，边界默认含）；"
    "FIELD_COMPARE={right_field,operator:LT|LE|GT|GE|EQ|NE}（左字段必须满足该关系，如到期日>=起息日写 GE）；"
    "ARITHMETIC_BALANCE={terms:[{field,coef}],operator:EQ|GE|LE|GT|LT,target_field,tolerance}（表内勾稽平衡式）；"
    "UNIQUE_KEY={key_fields:[...]}（单列或联合主键唯一性）。"
    "长度口径：字典或表结构里的「字符型，N」是列宽上限，只能推出「最多 N 位」，用 LENGTH_MAX。"
    "不要用精确长度约束——变长字段填短值是合法的，卡成「必须正好 N 位」会把正常数据判成违规。"
    "确实需要「必须正好 N 位」（如统一社会信用代码 18 位、YYYYMMDD 8 位）时，用 REGEXP 把格式写死。"
)

_DATA_AND_AUDIT_PROMPT = (
    "所有字段名、规则键和码表取值必须严格来自输入白名单，禁止虚构字段、规则或 SQL。"
)

_INGESTION_AND_ASSET_PROMPT = (
    "严格依据输入原文与结构化摘要进行判断，不得凭常识虚构原文未声明的字段、类型、长度或码值。"
)


def tool_prompt_preamble(tool_name: str) -> str:
    """按工具作用域拼装最小必要的前置契约，避免第二阶段的规则算子说明书污染第一阶段解析与复核。"""
    if tool_name == "assign_rules":
        return f"{_BASE_PROMPT}{_RULE_OPERATOR_PROMPT}"
    if tool_name in {"assign_generation_plan", "assign_schema", "assign_generators", "assign_audit_cases"}:
        return f"{_BASE_PROMPT}{_DATA_AND_AUDIT_PROMPT}"
    return f"{_BASE_PROMPT}{_INGESTION_AND_ASSET_PROMPT}"


_tool_prompt_preamble = tool_prompt_preamble
