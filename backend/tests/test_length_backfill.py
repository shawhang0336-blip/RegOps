"""长度规则的确定性补齐。

LENGTH 已从模型算子白名单移除（列宽是上限，不是定长），但移除之后模型干脆
不生成任何长度规则了——实测同一份原文三次运行：

    run A: LENGTH=28      （错，把所有短于 28 位的编号判违规）
    run B: LENGTH_MAX=28  （对）
    run C: 无              （漏）

「字符型，28」只能推出「最多 28 位」，这是机械结论，不该由模型决定要不要生成。
"""

from app.infra.contracts import FieldSpec, SpecDocument
from app.engine.workflow import _length_backfill_candidates


def _field(name, length, data_type="VARCHAR"):
    return FieldSpec(name=name, content="", data_type=data_type, length=length, evidence_page="196")


SPEC = SpecDocument(
    version="TEST",
    table_code="T1",
    table_name="测试表",
    source_name="sample.pdf",
    source_sha256="0" * 64,
    section="6.7.1.2",
    fields=[
        _field("EXGUARANCODE", 28),
        _field("REMARK", 256),
        _field("BASERE", 22, "NUMBER"),
        _field("NOLEN", 0),
    ],
    terms_text="",
    dictionary_text="",
)


class _StubRule:
    def __init__(self, table_code, field, operator):
        self.table_code = table_code
        self.field = field
        self.operator = operator


def test_text_fields_with_declared_width_are_candidates():
    names = [field.name for _, field in _length_backfill_candidates([SPEC], [])]
    assert names == ["EXGUARANCODE", "REMARK"]


def test_numeric_field_is_not_a_length_candidate():
    # NUMBER 的「22.2」是精度不是字符数，长度类规则对它没有意义
    names = {field.name for _, field in _length_backfill_candidates([SPEC], [])}
    assert "BASERE" not in names


def test_field_without_declared_length_is_skipped():
    names = {field.name for _, field in _length_backfill_candidates([SPEC], [])}
    assert "NOLEN" not in names


def test_existing_length_rule_is_not_duplicated():
    rules = [_StubRule("T1", "EXGUARANCODE", "LENGTH_MAX")]
    names = [field.name for _, field in _length_backfill_candidates([SPEC], rules)]
    assert names == ["REMARK"]


def test_exact_length_rule_also_counts_as_covered():
    # 硬编码的定长规则（如统一社会信用代码 18 位）同样算已覆盖，不该再补一条
    rules = [_StubRule("T1", "EXGUARANCODE", "LENGTH")]
    names = [field.name for _, field in _length_backfill_candidates([SPEC], rules)]
    assert names == ["REMARK"]
