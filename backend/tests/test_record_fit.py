from decimal import Decimal

from app.engine.domain import FieldSpec, SpecDocument
from app.engine.oracle import fit_record_values

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
        FieldSpec(name="CNT", content="数量", data_type="NUMBER", length=3, decimal_places=0, evidence_page="1"),
    ],
    terms_text="",
    dictionary_text="",
)


def test_overlong_text_is_truncated_to_field_length():
    # 线上真实故障：REMARK 是 VARCHAR(256)，AI 的变异用例给了 257 个字符 → ORA-12899
    fitted = fit_record_values(SPEC, {"REMARK": 8}, {"REMARK": "x" * 257})
    assert len(fitted["REMARK"]) == 8


def test_schema_length_wins_when_it_is_shorter():
    fitted = fit_record_values(SPEC, {"REMARK": 4}, {"REMARK": "abcdefgh"})
    assert fitted["REMARK"] == "abcd"


def test_number_is_rounded_to_scale_not_rejected():
    fitted = fit_record_values(SPEC, {"AMT": 6}, {"AMT": "1234.5678"})
    assert fitted["AMT"] == Decimal("1234.56")


def test_number_is_capped_to_precision():
    fitted = fit_record_values(SPEC, {"AMT": 6}, {"AMT": "12345678.9"})
    # NUMBER(6,2) 最多 4 位整数 + 2 位小数
    assert fitted["AMT"] == Decimal("9999.99")


def test_integer_field_drops_decimals():
    fitted = fit_record_values(SPEC, {"CNT": 3}, {"CNT": "7.9"})
    assert fitted["CNT"] == Decimal("7")


def test_unknown_fields_and_none_pass_through():
    fitted = fit_record_values(SPEC, {}, {"REMARK": None, "record_id": "case-1", "OTHER": "keep-me"})
    assert fitted["REMARK"] is None
    assert fitted["OTHER"] == "keep-me"
    assert fitted["record_id"] == "case-1"


def test_non_numeric_text_in_number_field_is_left_alone():
    fitted = fit_record_values(SPEC, {"AMT": 6}, {"AMT": "abc"})
    assert fitted["AMT"] == "abc"
