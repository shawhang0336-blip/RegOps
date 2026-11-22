"""固定值识别：这不是码表引用。

AI 抽取时把「本项固定填“中国”」填进了 code_reference_text，于是一路变成
"码表引用解析不到"，在码值页堆出一堆要补的占位码表——用户点进去只看到一张
空表，完全不知道要改什么。

难点在于不能一刀切：
    B0202  本项应填"中国"                → 无条件固定值
    E0901  如为个人主体，填写为"个人"      → 有条件
把 E0901 也卡成必须等于"个人"，非个人主体时的真实名称全会被判违规。
"""

from app.catalog.code_sets.parser import is_conditional_phrase, parse_fixed_value


def test_unconditional_fixed_value():
    assert parse_fixed_value("本项固定填“中国”") == "中国"
    assert parse_fixed_value("固定填“中国”") == "中国"


def test_conditional_fixed_value_still_yields_a_value():
    # 值照常提出来，条件性由 is_conditional_phrase 另行判断
    assert parse_fixed_value("个人主体固定取值“N/A”") == "N/A"


def test_conditional_phrase_is_detected_from_check_text():
    assert is_conditional_phrase("如为个人主体，填写为“个人”。") is True
    assert is_conditional_phrase("如为个人主体，则此项填写“N/A”。") is True
    assert is_conditional_phrase("被代理人或委托人必须为中国居民，本项应填“中国”。") is False


def test_two_values_is_not_a_fixed_value():
    # 「0 或 1」是两个取值，不是固定值
    assert parse_fixed_value("固定填 0 或 1") is None


def test_plain_code_set_reference_is_not_matched():
    assert parse_fixed_value("币种代码表") is None
    assert parse_fixed_value("是否附有银行承兑汇票（F0102 内联枚举）") is None
    assert parse_fixed_value("参照数据采集规范最新版本中的相关代码表") is None


def test_empty_and_none_are_safe():
    assert parse_fixed_value("") is None
    assert parse_fixed_value(None) is None
    assert is_conditional_phrase("") is False


def _spec(fields):
    from app.infra.contracts import SpecDocument

    return SpecDocument(
        version="TEST",
        table_code="T1",
        table_name="测试表",
        source_name="sample.pdf",
        source_sha256="0" * 64,
        section="6.3.1",
        fields=fields,
        terms_text="",
        dictionary_text="",
    )


def test_unconditional_fixed_value_becomes_a_rule_candidate():
    """B0202 那种「本项应填中国」，应该直接生成 ENUM{中国}。

    原来它被当成码表引用，在码值页堆出一张空占位表等人去补——根本没有码表可补。
    """
    from app.infra.contracts import FieldSpec
    from app.engine.workflow import _fixed_value_candidates

    spec = _spec(
        [
            FieldSpec(
                name="B0202",
                content="被代理人所属国家/地区",
                check_text="被代理人或委托人必须为中国居民，本项应填“中国”。",
                code_reference="本项固定填“中国”",
                evidence_page="1",
            )
        ]
    )
    candidates = _fixed_value_candidates([spec], [])
    assert [(field.name, value) for _, field, value in candidates] == [("B0202", "中国")]


def test_conditional_fixed_value_is_not_a_rule_candidate():
    """E0901 那种「如为个人主体，填写为个人」，不能卡成必须等于 个人。"""
    from app.infra.contracts import FieldSpec
    from app.engine.workflow import _fixed_value_candidates

    spec = _spec(
        [
            FieldSpec(
                name="E0901",
                content="境内交易主体名称",
                check_text="填写通过本机构与非居民之间发生贸易往来的境内交易主体名称。如为个人主体，填写为“个人”。",
                code_reference="个人主体固定取值“个人”",
                evidence_page="1",
            )
        ]
    )
    assert _fixed_value_candidates([spec], []) == []


def test_field_already_covered_by_enum_is_skipped():
    from app.infra.contracts import FieldSpec
    from app.engine.workflow import _fixed_value_candidates

    spec = _spec(
        [
            FieldSpec(
                name="B0202",
                content="",
                check_text="本项应填“中国”。",
                code_reference="本项固定填“中国”",
                evidence_page="1",
            )
        ]
    )

    class _Rule:
        table_code = "T1"
        field = "B0202"
        operator = "ENUM"

    assert _fixed_value_candidates([spec], [_Rule()]) == []
