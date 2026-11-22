"""必填规则的确定性补齐。

线上实测：SAFE_6_7_1_2 的 BASERE（担保责任余额）字典校验列写的是
「必填项，0<=担保责任余额<=保函金额。」，元数据 required=True，
生成计划的 rationale 里也明说「BASERE 必填」，但最终规则集里 BASERE 一条规则都没有。
必填不需要 AI 推理，靠原文前缀就能判，所以这里把判定抽出来做确定性补齐。
"""

from app.infra.contracts import FieldSpec, SpecDocument
from app.engine.workflow import _required_backfill_candidates

SPEC = SpecDocument(
    version="TEST",
    table_code="T1",
    table_name="测试表",
    source_name="sample.pdf",
    source_sha256="0" * 64,
    section="6.7.1.2",
    fields=[
        FieldSpec(name="BASERE", content="担保责任余额", check_text="必填项，0<=担保责任余额<=保函金额。", evidence_page="196"),
        FieldSpec(name="ACTIONTYPE", content="操作类型", check_text="必填项。A-新建C-修改D-删除", evidence_page="196"),
        FieldSpec(name="ACTIONDESC", content="删除原因", check_text="非必填项，当操作类型（ACTIONTYPE）为删除时必填", evidence_page="196"),
        FieldSpec(name="REMARK", content="备注", check_text="非必填项。", evidence_page="196"),
        FieldSpec(name="NOTE", content="说明", check_text="条件必填，当状态为有效时必填", evidence_page="196"),
    ],
    terms_text="",
    dictionary_text="",
)


class _StubRule:
    def __init__(self, table_code, field, operator):
        self.table_code = table_code
        self.field = field
        self.operator = operator


def test_fields_without_required_rule_are_candidates():
    names = [field.name for _, field in _required_backfill_candidates([SPEC], [])]
    assert names == ["BASERE", "ACTIONTYPE"]


def test_conditional_and_optional_fields_are_not_candidates():
    # 「非必填项，当…时必填」和「条件必填」不是无条件必填
    names = {field.name for _, field in _required_backfill_candidates([SPEC], [])}
    assert "ACTIONDESC" not in names
    assert "NOTE" not in names
    assert "REMARK" not in names


def test_existing_required_rule_is_not_duplicated():
    rules = [_StubRule("T1", "BASERE", "REQUIRED")]
    names = [field.name for _, field in _required_backfill_candidates([SPEC], rules)]
    assert names == ["ACTIONTYPE"]


def test_cond_required_does_not_count_as_unconditional():
    # 有条件必填仍然不是无条件必填，该补还得补
    rules = [_StubRule("T1", "BASERE", "COND_REQUIRED")]
    names = [field.name for _, field in _required_backfill_candidates([SPEC], rules)]
    assert "BASERE" in names
