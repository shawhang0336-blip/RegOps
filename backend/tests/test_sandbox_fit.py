"""沙箱变异值不能被裁到声明长度。

线上真实故障：审计用例的变异值原本也走 fit_record_values，被裁到字段声明长度后
插进 VARCHAR2(4000) 的沙箱宽表，于是 LENGTH / LENGTH_MAX 规则永远见不到超长值，
整类长度规则变成永不触发的死规则——一次 33 条用例的审计里 8 条长度用例全部
误报为「规则该命中却没命中」。
"""

from app.infra.domain import FieldSpec, SpecDocument
from app.engine.oracle import SANDBOX_TEXT_LIMIT, fit_sandbox_values

SPEC = SpecDocument(
    version="TEST",
    table_code="T1",
    table_name="测试表",
    source_name="sample.pdf",
    source_sha256="0" * 64,
    section="6.1",
    fields=[
        FieldSpec(name="REMARK", content="备注", data_type="VARCHAR", length=8, evidence_page="1"),
        FieldSpec(name="AMT", content="金额", data_type="NUMBER", length=6, decimal_places=2, evidence_page="1"),
    ],
    terms_text="",
    dictionary_text="",
)


def test_overlong_text_survives_for_length_rules():
    fitted = fit_sandbox_values(SPEC, {"REMARK": "x" * 257})
    assert len(fitted["REMARK"]) == 257


def test_sandbox_hard_limit_still_applies():
    fitted = fit_sandbox_values(SPEC, {"REMARK": "x" * (SANDBOX_TEXT_LIMIT + 10)})
    assert len(fitted["REMARK"]) == SANDBOX_TEXT_LIMIT


def test_none_and_numbers_pass_through():
    fitted = fit_sandbox_values(SPEC, {"REMARK": None, "AMT": "1234.5678", "record_id": "c1"})
    assert fitted["REMARK"] is None
    assert fitted["AMT"] == "1234.5678"
    assert fitted["record_id"] == "c1"
