from pathlib import Path
from docx import Document
from openpyxl import Workbook

from app.catalog.ingestion.parsers import extractors
from app.catalog.ingestion.parsers.pdf_catalog import parse_document_catalog
from app.infra.config import get_settings
from app.catalog.ingestion.parsers.extractors import extract_document, extract_docx, extract_pdf, extract_xlsx, survey_document


def test_docx_tables_become_raw_blocks(tmp_path):
    document = Document()
    table = document.add_table(rows=2, cols=3)
    for index, value in enumerate(["字段中文名", "字段名", "字段校验"]):
        table.rows[0].cells[index].text = value
    for index, value in enumerate(["机构名称", "CUSTNAME", "必填"]):
        table.rows[1].cells[index].text = value
    path = tmp_path / "sample.docx"
    document.save(path)
    _, blocks = extract_docx(path)
    assert len(blocks) == 1
    assert blocks[0]["content_json"]["headers"] == ["字段中文名", "字段名", "字段校验"]
    assert blocks[0]["source_locator"]["table_index"] == 0


def test_docx_binds_nearest_heading_and_honors_chunking_plan(tmp_path):
    document = Document()
    document.add_heading("修订历史", level=1)
    rev_tbl = document.add_table(rows=2, cols=2)
    rev_tbl.rows[0].cells[0].text, rev_tbl.rows[0].cells[1].text = "版本号", "修订内容"
    rev_tbl.rows[1].cells[0].text, rev_tbl.rows[1].cells[1].text = "V1.0", "初稿"

    document.add_heading("2.1 贷款借据明细表", level=2)
    meta_tbl = document.add_table(rows=2, cols=3)
    for idx, val in enumerate(["字段中文名", "字段名", "字段校验"]):
        meta_tbl.rows[0].cells[idx].text = val
    for idx, val in enumerate(["借据编号", "DKJJH", "主键必填"]):
        meta_tbl.rows[1].cells[idx].text = val

    document.add_heading("附录一 贷款五级分类代码表", level=1)
    code_tbl = document.add_table(rows=3, cols=2)
    code_tbl.rows[0].cells[0].text, code_tbl.rows[0].cells[1].text = "代码", "含义"
    code_tbl.rows[1].cells[0].text, code_tbl.rows[1].cells[1].text = "1", "正常"
    code_tbl.rows[2].cells[0].text, code_tbl.rows[2].cells[1].text = "2", "关注"

    path = tmp_path / "east_spec.docx"
    document.save(path)

    survey = survey_document(path, "docx")
    assert survey["tables"][1]["nearest_heading"] == "2.1 贷款借据明细表"

    plan = {
        "chunking_strategy": "WORD_TABLE_AND_PARAGRAPH",
        "section_regions": [
            {"start_heading": "修订历史", "role": "IGNORE"},
            {"start_heading": "2.1 贷款借据明细表", "role": "TABLE_METADATA"},
            {"start_heading": "附录一 贷款五级分类代码表", "role": "CODE_SET"},
        ],
    }
    _, blocks = extract_docx(path, chunking_plan=plan)
    table_blocks = [b for b in blocks if b["block_type"] == "DOCX_TABLE"]
    assert len(table_blocks) == 2
    assert table_blocks[0]["content_json"]["table_name"] == "2.1 贷款借据明细表"
    assert table_blocks[0]["source_locator"]["section"] == "2.1 贷款借据明细表"
    assert table_blocks[1]["content_json"]["candidate_role"] == "CODE_SET"
    assert len(table_blocks[1]["content_json"]["code_set_spec"]["values"]) == 2


def test_xlsx_sheets_and_merged_ranges_become_raw_blocks(tmp_path):
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "字段字典"
    sheet.append(["字段中文名", "字段名", "字段校验"])
    sheet.append(["机构名称", "CUSTNAME", "必填"])
    sheet.merge_cells("A3:C3")
    sheet["A3"] = "说明"
    path = tmp_path / "sample.xlsx"
    workbook.save(path)
    _, blocks = extract_xlsx(path)
    assert len(blocks) == 1
    assert blocks[0]["content_json"]["rows"][1][1] == "CUSTNAME"
    assert blocks[0]["source_locator"]["sheet"] == "字段字典"
    assert blocks[0]["source_locator"]["merged_ranges"] == ["A3:C3"]


def test_xlsx_honors_chunking_plan_ignore_and_roles(tmp_path):
    workbook = Workbook()
    cover = workbook.active
    cover.title = "封面与修订记录"
    cover.append(["文档名称", "EAST 5.0 数据标准"])
    meta = workbook.create_sheet("对公客户表")
    meta.append(["字段中文名", "字段名", "数据类型"])
    meta.append(["客户编号", "KHBH", "VARCHAR(32)"])
    codes = workbook.create_sheet("币种代码")
    codes.append(["币种代码", "币种名称"])
    codes.append(["CNY", "人民币"])
    codes.append(["USD", "美元"])
    path = tmp_path / "east.xlsx"
    workbook.save(path)

    plan = {
        "chunking_strategy": "SHEET_PER_TABLE",
        "section_regions": [
            {"start_heading": "封面与修订记录", "role": "IGNORE"},
            {"start_heading": "对公客户表", "role": "TABLE_METADATA"},
            {"start_heading": "币种代码", "role": "CODE_SET"},
        ],
    }
    _, blocks = extract_xlsx(path, chunking_plan=plan)
    assert [b["source_locator"]["sheet"] for b in blocks] == ["对公客户表", "币种代码"]
    assert blocks[0]["content_json"]["candidate_role"] == "TABLE_METADATA"
    assert blocks[1]["content_json"]["candidate_role"] == "CODE_SET"
    assert len(blocks[1]["content_json"]["code_set_spec"]["values"]) == 2


def test_generic_pdf_heading_sections_with_and_without_chunking_plan(monkeypatch, tmp_path):
    east_pdf_text = (
        "银行业金融机构监管数据标准化规范（EAST 5.0）\n"
        "修订记录\n"
        "2024-01 发布 V5.0 正式版，补充普惠金融与绿色信贷表结构说明。\n\f"
        "目 录\n"
        "第一章 总则 ...................... 1\n"
        "第二章 客户与授信数据字典 .......... 2\n"
        "第三章 公共代码表 .................. 4\n\f"
        "第一章 总则\n"
        "本规范适用于全国银行业金融机构向监管机构报送标准化业务数据。\n\f"
        "第二章 客户与授信数据字典\n"
        "2.1 对公客户基本信息表\n"
        "序号 数据项名称 字段代码 数据类型 校验要求\n"
        "1 银行机构代码 YXJGDM C..30 必填，15位金融机构编码\n"
        "2 客户编号 KHBH VARCHAR(60) 主键，非空\n"
        "3 注册资本 ZCZB DECIMAL(20,2) 数值型，大于等于0\n"
        "4 客户状态 KHZT CHAR(1) 1-正常，2-冻结，3-销户\n"
        "2.2 贷款借据明细表\n"
        "序号 字段代码 数据项名称 数据类型 校验要求\n"
        "1 DKJJH 贷款借据号 VARCHAR2(64) 主键，非空\n"
        "2 DKJE 贷款金额 NUMBER(20,2) 必须大于0\n\f"
        "第三章 公共代码表\n"
        "3.1 币种代码表\n"
        "代码 名称\n"
        "CNY 人民币\n"
        "USD 美元\n"
        "EUR 欧元\n\f"
        "第四章 检核规则说明\n"
        "4.1 跨表一致性规则\n"
        "贷款借据明细表的客户编号必须存在于对公客户基本信息表的客户编号集合中。\n"
    )
    fake_pdf = tmp_path / "east_5_0.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 mock east")
    monkeypatch.setattr(extractors, "_extract_pdf_text", lambda _p: east_pdf_text)

    # 1) survey 准确识别非外管局 PDF，推荐 HEADING_SECTIONS
    survey = survey_document(fake_pdf, "pdf")
    assert survey["detected_profiles"] == ["GENERIC_PDF"]
    assert survey["available_chunk_strategy"] == "HEADING_SECTIONS"
    assert "2.1 对公客户基本信息表" in survey["headings"]
    assert "3.1 币种代码表" in survey["headings"]

    # 2) 带 AI ① 切块蓝图执行：忽略第一章总则与修订记录，精准抽取表字典、码表、规则
    plan = {
        "chunking_strategy": "HEADING_SECTIONS",
        "section_regions": [
            {"start_heading": "修订记录", "role": "IGNORE"},
            {"start_heading": "第一章 总则", "role": "IGNORE"},
            {"start_heading": "第二章 客户与授信数据字典", "end_heading": "第三章 公共代码表", "role": "TABLE_METADATA"},
            {"start_heading": "第三章 公共代码表", "end_heading": "第四章 检核规则说明", "role": "CODE_SET"},
            {"start_heading": "第四章 检核规则说明", "role": "RULE_EVIDENCE"},
        ],
    }
    version, blocks = extract_document(fake_pdf, "pdf", chunking_plan=plan)
    assert version == "GENERIC_PDF"
    titles = [b["content_json"]["table_name"] for b in blocks]
    assert "修订记录" not in titles
    assert "第一章 总则" not in titles
    assert "2.1 对公客户基本信息表" in titles
    assert "2.2 贷款借据明细表" in titles
    assert "3.1 币种代码表" in titles
    assert "4.1 跨表一致性规则" in titles

    cust_block = next(b for b in blocks if b["content_json"]["table_name"] == "2.1 对公客户基本信息表")
    assert cust_block["content_json"]["eligible"] is True
    assert [f["name"] for f in cust_block["content_json"]["spec"]["fields"]] == ["YXJGDM", "KHBH", "ZCZB", "KHZT"]
    assert cust_block["content_json"]["spec"]["fields"][2]["data_type"] == "NUMBER"
    assert cust_block["content_json"]["spec"]["fields"][2]["decimal_places"] == 2
    assert len(cust_block["content_json"]["inline_code_sets"]) == 1

    loan_block = next(b for b in blocks if b["content_json"]["table_name"] == "2.2 贷款借据明细表")
    assert [f["name"] for f in loan_block["content_json"]["spec"]["fields"]] == ["DKJJH", "DKJE"]

    code_block = next(b for b in blocks if b["content_json"]["table_name"] == "3.1 币种代码表")
    assert code_block["block_type"] == "CODE_SET_CANDIDATE"
    assert {v["value"] for v in code_block["content_json"]["code_set_spec"]["values"]} == {"CNY", "USD", "EUR"}

    rule_block = next(b for b in blocks if b["content_json"]["table_name"] == "4.1 跨表一致性规则")
    assert rule_block["block_type"] == "RULE_EVIDENCE"

    # 3) 离线无蓝图（chunking_plan=None）同样可稳定拆出陌生 PDF，绝不抛 SpecParseError
    version_offline, blocks_offline = extract_pdf(fake_pdf, chunking_plan=None)
    assert version_offline == "GENERIC_PDF"
    assert len(blocks_offline) >= 4


def test_headingless_pdf_falls_back_to_page_windows(monkeypatch, tmp_path):
    plain_pdf_text = (
        "机构编码 ORG_ID VARCHAR(32) 必填\n"
        "客户名称 CUST_NAME VARCHAR(128) 必填\n"
        "贷款余额 LOAN_BAL DECIMAL(18,2) 非负\n\f"
        "补充说明：本页列示季度报送口径及外汇折算汇率取数方式。\n"
    )
    fake_pdf = tmp_path / "no_headings.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 mock no headings")
    monkeypatch.setattr(extractors, "_extract_pdf_text", lambda _p: plain_pdf_text)

    survey = survey_document(fake_pdf, "pdf")
    assert survey["available_chunk_strategy"] == "PAGE_WINDOWS"

    # 即便误传 SAFE_SPEC_PARSER，也能在未命中时自动优雅降级到 PAGE_WINDOWS 而非抛异常
    version, blocks = extract_pdf(fake_pdf, chunking_plan={"chunking_strategy": "SAFE_SPEC_PARSER"})
    assert version == "GENERIC_PDF"
    assert len(blocks) == 1
    assert blocks[0]["source_locator"]["strategy"] == "PAGE_WINDOWS"
    assert blocks[0]["content_json"]["field_count"] == 3


def test_chapter6_tables_without_terms_are_included():
    entries = parse_document_catalog(get_settings().safe_13_spec_path)
    by_section = {entry["section"]: entry for entry in entries}

    assert len(entries) == 102
    for section in ["6.2.5", "6.2.8", "6.3.1", "6.3.2", "6.3.3", "6.3.4", "6.3.5", "6.3.6"]:
        entry = by_section[section]
        assert entry["eligible"] is True
        assert entry["field_count"] > 0
        first = entry["spec"]["fields"][0]
        assert first["name"] == "ACTIONTYPE"
        assert first["content"] == "操作类型"
        assert first["data_type"] == "VARCHAR"
        assert "A-新建" in first["check_text"]
