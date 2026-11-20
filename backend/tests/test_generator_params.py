"""黄金记录生成器的参数必须真的是数字。

实测故障：AI 把 SEQUENCE 的 start/step 写成字符串

    {"step": "100.00", "start": "1000.00"}

于是 `start + index * step` 变成 `"1000.00" + "100.00"`（字符串重复拼接），
生成出 "1000.00100.00"，插进 NUMBER 列直接 ORA-01722，整次审计失败。
"""

import pytest

from app.infra.domain import FieldSpec, SpecDocument
from app.engine.binding import bind_generators, materialize_golden_record
from app.engine.rules import RuleValidationError

SPEC = SpecDocument(
    version="TEST",
    table_code="T1",
    table_name="测试表",
    source_name="sample.pdf",
    source_sha256="0" * 64,
    section="6.7.1.2",
    fields=[
        FieldSpec(name="BASERE", content="余额", data_type="NUMBER", length=22, decimal_places=2, evidence_page="1"),
        FieldSpec(name="CODE", content="编号", length=28, evidence_page="1"),
    ],
    terms_text="",
    dictionary_text="",
)


def _generators(basere_params, code_params=None):
    return [
        {"field": "BASERE", "generator": "SEQUENCE", "params": basere_params},
        {"field": "CODE", "generator": "SEQUENCE", "params": code_params or {"start": 1, "step": 1}},
    ]


def test_string_numbers_are_coerced_to_numbers():
    bindings = bind_generators(SPEC, _generators({"start": "1000.00", "step": "100.00"}))
    record = materialize_golden_record(SPEC, bindings, index=1)
    # 关键是没有变成 "1000.00100.00"；materialize 统一返回字符串，交给入库前裁剪
    assert record["BASERE"] == "1100.0"


def test_non_numeric_string_is_rejected():
    with pytest.raises(RuleValidationError):
        bind_generators(SPEC, _generators({"start": "1000元", "step": 1}))


def test_width_works_as_padding_alias():
    bindings = bind_generators(
        SPEC,
        _generators({"start": 0, "step": 1}, {"start": 1, "step": 1, "width": 10, "prefix": "EXG"}),
    )
    record = materialize_golden_record(SPEC, bindings, index=0)
    assert record["CODE"] == "EXG0000000001"


def test_integer_start_stays_integer():
    bindings = bind_generators(SPEC, _generators({"start": 100, "step": 1}, {"start": "7", "step": "1"}))
    record = materialize_golden_record(SPEC, bindings, index=2)
    assert record["BASERE"] == "102"
    assert record["CODE"] == "9"
