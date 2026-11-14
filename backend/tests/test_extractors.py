from docx import Document
from openpyxl import Workbook

from app.extractors import extract_docx, extract_xlsx


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
