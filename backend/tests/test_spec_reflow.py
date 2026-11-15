from app.spec_parser import lookup_term_definition, reflow_dictionary_rows, reflow_spec_terms

WRAPPED_DICTIONARY = """字段         内容     类型（长度）             校验
ACTIONTYPE   操作类型      字符型，1       必填项。
                             216
              国家外汇管理局金融机构外汇业务数据采集规范（V1.3）


     字段            内容   类型（长度）                校验
                                   A-新建
                                   C-修改
                                   D-删除
STRDECODE    人民币结构性存   字符型，28     必填项，唯一性编码。
              款编号
INPAYRMBAM   付息人民币支付   数值型，22.2   非必填项，大于等于 0。付息人民币支付金
              金额                   额与付息外币支付金额至少填一个。
INPAYCURR    付息外币支付币   字符型，3      非必填项。付息外币支付币种和付息外币支
              种                    付金额为一组数据，两者同时为空或者不为
                                   空。
REMARK       备注        字符型，256    非必填项。
"""

WRAPPED_TERMS = """基础数据项                             定义
人民币结构性       该笔利息给付情况对应的人民币结构性存款编号。利息给付信息不产生新的人
存款编号         民币结构性存款编号，利息给付信息中的人民币结构性存款编号必须为签约信
             息中已经报送的人民币结构性存款编号。
金融机构标识       国家外汇管理局及其分支局签发托管银行 12 位金融机构标识码。
码
付息外币支付       以外币形式支付人民币结构性存款利息金额。
金额
备注           需要说明的备注事项。
"""


def test_wrapped_name_is_joined_not_leaked_into_check_text():
    rows = reflow_dictionary_rows(WRAPPED_DICTIONARY).split("\n")
    strdecode = next(row for row in rows if row.startswith("STRDECODE"))
    assert "人民币结构性存款编号" in strdecode
    assert strdecode.endswith("必填项，唯一性编码。")
    assert "款编号" not in strdecode.split("字符型，28")[1]


def test_check_text_continuation_is_joined_in_place():
    rows = reflow_dictionary_rows(WRAPPED_DICTIONARY).split("\n")
    inpayrmbam = next(row for row in rows if row.startswith("INPAYRMBAM"))
    assert "付息人民币支付金额 数值型，22.2" in inpayrmbam
    assert "付息人民币支付金额与付息外币支付金额至少填一个。" in inpayrmbam


def test_multiline_check_text_and_enumeration_cells_are_kept():
    rows = reflow_dictionary_rows(WRAPPED_DICTIONARY).split("\n")
    actiontype = next(row for row in rows if row.startswith("ACTIONTYPE"))
    assert actiontype.endswith("必填项。A-新建C-修改D-删除")
    inpaycurr = next(row for row in rows if row.startswith("INPAYCURR"))
    assert "付息外币支付币种 字符型，3" in inpaycurr
    assert inpaycurr.endswith("两者同时为空或者不为空。")


def test_page_noise_and_repeated_headers_are_dropped():
    rows = reflow_dictionary_rows(WRAPPED_DICTIONARY).split("\n")
    assert len(rows) == 5
    assert not any("国家外汇管理局" in row for row in rows)
    assert not any(row.strip().isdigit() for row in rows)


def test_terms_are_reflowed_into_name_definition_pairs():
    lines = reflow_spec_terms(WRAPPED_TERMS).split("\n")
    names = [line.split("\t", 1)[0] for line in lines]
    assert names == ["人民币结构性存款编号", "金融机构标识码", "付息外币支付金额", "备注"]
    assert lookup_term_definition(reflow_spec_terms(WRAPPED_TERMS), "金融机构标识码").startswith(
        "国家外汇管理局及其分支局签发托管银行 12 位"
    )


def test_term_lookup_falls_back_and_tolerates_missing_terms():
    assert lookup_term_definition("备注\t需要说明的备注事项。", "不存在的字段") == ""
    assert lookup_term_definition("", "备注") == ""


def test_reflow_is_idempotent_for_already_flat_rows():
    flat = "STRDECODE 人民币结构性存款编号 字符型，28 必填项，唯一性编码。\n"
    assert reflow_dictionary_rows(flat).strip() == flat.strip()
