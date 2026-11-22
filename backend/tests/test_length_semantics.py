"""长度口径：列宽是上限，不是定长。

线上实测：同一份字典（`ExguaranCODE 字符型,28`）两次运行，一次生成 LENGTH=28
（必须正好 28 位），一次生成 LENGTH_MAX=28（最多 28 位）。前者是错的——
`VARCHAR2(28)` 本身只约束上限，短值插得进去；把它卡成定长，业务里所有短于
28 位的担保编号都会被判违规。而误报在报送场景比漏报更致命：正常数据也发不出去。

根因是「是定长还是变长」这个本有唯一答案的问题被交给了模型自由选择，
所以直接把 LENGTH 从算子白名单里去掉，需要「必须正好 N 位」时用 REGEXP 写死格式。
"""

import pytest

from app.infra.llm.schemas import RULE_PROPERTIES, TOOL_SCHEMAS


def test_operator_enum_has_no_exact_length():
    operators = RULE_PROPERTIES["operator"]["enum"]
    assert "LENGTH" not in operators
    assert "LENGTH_MAX" in operators
    assert "REGEXP" in operators


def test_every_rule_tool_schema_uses_the_restricted_operator_enum():
    seen = 0
    for name, schema in TOOL_SCHEMAS.items():
        item = (schema.get("properties") or {}).get("rules", {}).get("items") or {}
        operator = (item.get("properties") or {}).get("operator")
        if operator:
            seen += 1
            assert "LENGTH" not in operator["enum"], name
    assert seen, "没有找到任何带 operator 的工具 schema，测试失去意义"


def test_prompt_no_longer_offers_exact_length():
    """提示词里不能再出现「精确长度」这种把模型往 LENGTH 上引的说法。"""
    import inspect

    from app.infra.llm import schemas as generic_ai

    source = inspect.getsource(generic_ai)
    assert "LENGTH={length:精确长度}" not in source
    assert "LENGTH={length:整数}" not in source
    assert "列宽上限" in source


def test_bind_rules_rejects_exact_length_even_if_model_returns_it():
    """enum 只是提示，模型仍可能硬返回 LENGTH；绑定层要硬拦。"""
    from app.infra.domain import FieldSpec, SpecDocument
    from app.engine.binding import bind_rules
    from app.engine.rules import RuleValidationError

    spec = SpecDocument(
        version="TEST",
        table_code="T1",
        table_name="测试表",
        source_name="sample.pdf",
        source_sha256="0" * 64,
        section="6.7.1.2",
        fields=[FieldSpec(name="EXGUARANCODE", content="对外担保编号", length=28, check_text="必填项。", evidence_page="196")],
        terms_text="",
        dictionary_text="",
    )
    with pytest.raises(RuleValidationError):
        bind_rules(
            spec,
            [
                {
                    "field": "EXGUARANCODE",
                    "operator": "LENGTH",
                    "params": {"length": 28},
                    "rule_key": "T1_EXGUARANCODE_LENGTH",
                    "evidence_kind": "DICTIONARY",
                }
            ],
        )


def test_bind_rules_still_accepts_length_max():
    from app.infra.domain import FieldSpec, SpecDocument
    from app.engine.binding import bind_rules

    spec = SpecDocument(
        version="TEST",
        table_code="T1",
        table_name="测试表",
        source_name="sample.pdf",
        source_sha256="0" * 64,
        section="6.7.1.2",
        fields=[FieldSpec(name="EXGUARANCODE", content="对外担保编号", length=28, check_text="必填项。", evidence_page="196")],
        terms_text="",
        dictionary_text="",
    )
    rules = bind_rules(
        spec,
        [
            {
                "field": "EXGUARANCODE",
                "operator": "LENGTH_MAX",
                "params": {"length": 28},
                "rule_key": "T1_EXGUARANCODE_LENGTH_MAX",
                "evidence_kind": "DICTIONARY",
            }
        ],
    )
    assert rules[0].operator == "LENGTH_MAX"
