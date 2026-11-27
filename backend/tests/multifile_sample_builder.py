"""高保真多监管体系（SAFE / 一表通 2.0 / EAST 5.0 / 1104 / 人行）混装样本生成器。

用于自动化回归测试与本地真实验证，覆盖真实监管文档中的典型版式陷阱：
1. Word 章节树 + 横/纵合并单元格 + 两级折叠表头 + 表后紧邻校验口径段落 + 修订历史表过滤；
2. Excel 前导说明行（自动表头探测）+ 目录索引 Sheet + 单 Sheet 多张码表拆分 + 双模式公式（=SUM）+ 1104 二维指标矩阵识别；
3. 独立核查规则 / 检核 SQL 清单（SAFE/EAST/一表通/1104）；
4. 规范包 ZIP 打包。
"""

import io
import zipfile
from pathlib import Path
from typing import Dict

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from openpyxl import Workbook


def _set_cell_text(cell, text: str) -> None:
    cell.text = text


def _merge_horizontal(row, start_col: int, end_col: int, text: str) -> None:
    c = row.cells[start_col]
    c.text = text
    c.merge(row.cells[end_col])


def build_safe_word_docx() -> bytes:
    """SAFE 外管局 Word 接口规范：含修订记录表、两级/横跨标题、合并数据行与表后紧邻字段校验说明。"""
    doc = Document()
    doc.add_heading("金融机构外汇业务数据采集规范（Word 补充接口规范）", level=0)

    # 1. 修订记录表（应自动识别为 IGNORE）
    doc.add_heading("一、修订记录", level=1)
    rev_tbl = doc.add_table(rows=3, cols=3)
    for i, h in enumerate(["修订版本号", "修订日期", "修订人"]):
        rev_tbl.rows[0].cells[i].text = h
    rev_tbl.rows[1].cells[0].text = "V1.4"
    rev_tbl.rows[1].cells[1].text = "2025-05-01"
    rev_tbl.rows[1].cells[2].text = "SAFE_SPEC_TEAM"

    # 2. 涉外收入接口规范表（带横跨 banner + 两级折叠表头 + 表后校验口径）
    doc.add_heading("二、涉外收支接口规范", level=1)
    doc.add_heading("2.1 涉外收入基础信息表 (BOP_A)", level=2)
    doc.add_paragraph("本表采集金融机构办理的涉外收入基础申报数据，按日通过消息传输系统报送。")

    tbl = doc.add_table(rows=7, cols=6)
    # Row 0: 横跨 banner（不应被误认为表头）
    _merge_horizontal(tbl.rows[0], 0, 5, "表 BOP_A 涉外收入基础信息字段定义表")
    # Row 1-2: 两级表头（"长度" 横跨 col 3..4，子表头为 总长 / 小数位）
    tbl.rows[1].cells[0].text = "数据项名称"
    tbl.rows[1].cells[1].text = "英文标识"
    tbl.rows[1].cells[2].text = "数据类型"
    _merge_horizontal(tbl.rows[1], 3, 4, "长度")
    tbl.rows[1].cells[5].text = "校验要求"

    tbl.rows[2].cells[0].text = "数据项名称"
    tbl.rows[2].cells[1].text = "英文标识"
    tbl.rows[2].cells[2].text = "数据类型"
    tbl.rows[2].cells[3].text = "总长"
    tbl.rows[2].cells[4].text = "小数位"
    tbl.rows[2].cells[5].text = "校验要求"

    data = [
        ["申报号码", "RPTNO", "VARCHAR", "22", "0", "必填，不可重复"],
        ["收款人名称", "OPPUSER", "VARCHAR", "128", "0", "必填"],
        ["收入款币种", "TXCCY", "CHAR", "3", "0", "必填，见币种代码表"],
        ["收入款金额", "TXAMT", "NUMBER", "22", "2", "必填，大于 0"],
    ]
    for r_i, row in enumerate(data, start=3):
        for c_i, val in enumerate(row):
            tbl.rows[r_i].cells[c_i].text = val

    # 表后紧邻说明段落（必须绑定到 BOP_A 的 context_after）
    doc.add_paragraph("RPTNO（申报号码）：前 12 位为金融机构标识码，后 6 位为日期 YYMMDD，后 4 位为当日流水号。")
    doc.add_paragraph("TXAMT（收入款金额）：必须大于 0；当 CUSTYPE=C 时，收款人组织机构代码必填。")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def build_safe_codeset_xlsx() -> bytes:
    """SAFE 公共代码表 Excel：单 Sheet 内含前导说明行 + 2 张独立码表（通过空白行与新表头分隔）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "公共代码附录"

    ws.append(["国家外汇管理局通用代码字典（节选）"])
    ws.append(["币种代码表"])
    ws.append(["代码", "中文名称", "英文名称", "说明"])
    ws.append(["CNY", "人民币", "CNY", "中国法定货币"])
    ws.append(["USD", "美元", "USD", "美国货币"])
    ws.append(["EUR", "欧元", "EUR", "欧元区货币"])
    ws.append([])
    ws.append(["证件类型代码表"])
    ws.append(["代码值", "代码名称", "代码说明"])
    ws.append(["110", "居民身份证", "境内自然人"])
    ws.append(["120", "护照", "境外或境内个人护照"])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_ybt_word_docx() -> bytes:
    """一表通 2.0 Word 业务口径规范（含客户表与产品表业务定义及故意的字段长度差异以验证冲突告警）。"""
    doc = Document()
    doc.add_heading("国家金融监督管理总局 一表通 2.0 业务与填报口径规范", level=0)
    doc.add_heading("一、客户域模型定义", level=1)
    doc.add_heading("1.1 客户基本信息表 YBT_CUST_BASE", level=2)
    doc.add_paragraph("记录境内银行对公、零售、同业统一客户主数据口径。")

    tbl = doc.add_table(rows=5, cols=6)
    headers = ["字段中文名", "字段英文名", "数据类型", "长度", "是否必填", "业务定义"]
    for i, h in enumerate(headers):
        tbl.rows[0].cells[i].text = h
    rows = [
        ["客户编号", "CUST_ID", "VARCHAR(40)", "40", "M", "全行唯一客户标识（不得重复）"],
        ["客户名称", "CUST_NAME", "VARCHAR(120)", "120", "M", "客户全称，应与证照一致"],
        # 故意把 CUST_TYPE 写成 VARCHAR(10)，与 Excel 中的 VARCHAR(4) 制造跨文件显式冲突！
        ["客户类型", "CUST_TYPE", "VARCHAR(10)", "10", "M", "见一表通客户类型代码表"],
        ["国籍地区代码", "COUNTRY_CD", "CHAR(3)", "3", "C", "当客户为境外机构时必填，见币种或国别代码表"],
    ]
    for r_i, row in enumerate(rows, start=1):
        for c_i, val in enumerate(row):
            tbl.rows[r_i].cells[c_i].text = val
    doc.add_paragraph("补充口径：CUST_ID 主键唯一；若 CUST_TYPE=01（企业），则统一社会信用代码不得为空。")

    doc.add_heading("二、产品域模型定义", level=1)
    doc.add_heading("2.1 金融产品信息表 YBT_PROD_INFO", level=2)
    ptbl = doc.add_table(rows=4, cols=5)
    for i, h in enumerate(["字段中文名", "字段英文名", "数据类型", "是否必填", "校验要求"]):
        ptbl.rows[0].cells[i].text = h
    prows = [
        ["产品代码", "PROD_ID", "VARCHAR(32)", "M", "必填，唯一"],
        ["产品名称", "PROD_NAME", "VARCHAR(128)", "M", "必填"],
        ["是否绿色金融标识", "IS_GREEN", "CHAR(1)", "M", "必填，1-是；0-否"],
    ]
    for r_i, row in enumerate(prows, start=1):
        for c_i, val in enumerate(row):
            ptbl.rows[r_i].cells[c_i].text = val

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def build_ybt_excel_dict_xlsx() -> bytes:
    """一表通 2.0 Excel 数据字典与码表：含 表清单(CATALOG_INDEX)、客户表、产品表、贷款借据表与码表页。"""
    wb = Workbook()
    idx_ws = wb.active
    idx_ws.title = "表清单"
    idx_ws.append(["一表通 2.0 监管数据模型清单（共 3 张核心表）"])
    idx_ws.append(["主题域", "物理表名", "中文表名"])
    idx_ws.append(["客户域", "YBT_CUST_BASE", "客户基本信息表"])
    idx_ws.append(["产品域", "YBT_PROD_INFO", "金融产品信息表"])
    idx_ws.append(["贷款域", "YBT_LOAN_RECEIPT", "贷款借据明细表"])

    # Sheet 2: YBT_CUST_BASE（带前导说明 + 与 Word 互补/冲突字段）
    ws_c = wb.create_sheet("客户基本信息")
    ws_c.append(["一表通可信区接口表定义 - 客户域"])
    ws_c.append(["数据项名称", "物理字段名", "数据类型", "数据长度", "是否必填", "代码项", "取数逻辑"])
    ws_c.append(["客户编号", "CUST_ID", "VARCHAR", 40, "M", "", "全行统一客户号"])
    ws_c.append(["客户名称", "CUST_NAME", "VARCHAR", 120, "M", "", "必填"])
    ws_c.append(["客户类型", "CUST_TYPE", "VARCHAR", 4, "M", "一表通客户类型代码表", "必填"])
    ws_c.append(["统一社会信用代码", "USCC", "CHAR", 18, "C", "", "条件必填：对公客户填 18 位编码"])

    # Sheet 3: YBT_LOAN_RECEIPT（含 DECIMAL(18,2) 与跨字段校验）
    ws_l = wb.create_sheet("YBT_LOAN_RECEIPT")
    ws_l.append(["字段中文名", "字段英文名", "数据类型", "是否必填", "校验要求", "代码项"])
    ws_l.append(["借据号", "RECEIPT_NO", "VARCHAR(32)", "M", "必填，唯一", ""])
    ws_l.append(["客户编号", "CUST_ID", "VARCHAR(40)", "M", "必填，需在 YBT_CUST_BASE 存在", ""])
    ws_l.append(["产品代码", "PROD_ID", "VARCHAR(32)", "M", "必填，需在 YBT_PROD_INFO 存在", ""])
    ws_l.append(["借据本金余额", "LOAN_BAL", "DECIMAL(18,2)", "M", "必填，大于等于 0", ""])
    ws_l.append(["发放日期", "START_DT", "DATE", "M", "必填，格式 YYYYMMDD", ""])
    ws_l.append(["到期日期", "MATURE_DT", "DATE", "M", "必填，到期日期不得早于发放日期", ""])

    # Sheet 4: 一表通代码表
    ws_cs = wb.create_sheet("一表通客户类型代码表")
    ws_cs.append(["代码", "名称", "含义"])
    ws_cs.append(["01", "对公客户", "境内法人企业"])
    ws_cs.append(["02", "个人客户", "自然人"])
    ws_cs.append(["03", "同业金融机构", "银行、证券、保险等"])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_ybt_rules_xlsx() -> bytes:
    """一表通 2.0 可信区检核规则清单 Excel（单表逻辑 + 跨表勾稽 + 检核 SQL）。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "可信区检核规则"
    ws.append(["规则编号", "规则名称", "表名", "字段名", "校验类型", "跨表标志", "校验逻辑", "检核SQL"])
    ws.append([
        "YBT_CUST_001",
        "企业统一信用代码必填",
        "YBT_CUST_BASE",
        "USCC,CUST_TYPE",
        "强校验",
        "否",
        "当 CUST_TYPE='01' 时，USCC 不能为空且长度为 18 位",
        "SELECT * FROM YBT_CUST_BASE WHERE CUST_TYPE='01' AND (USCC IS NULL OR LENGTH(USCC)<>18)",
    ])
    ws.append([
        "YBT_LOAN_002",
        "借据客户关联一致性",
        "YBT_LOAN_RECEIPT",
        "CUST_ID",
        "强校验",
        "是",
        "YBT_LOAN_RECEIPT 的 CUST_ID 必须在 YBT_CUST_BASE 中存在有效记录",
        "SELECT t.RECEIPT_NO FROM YBT_LOAN_RECEIPT t LEFT JOIN YBT_CUST_BASE c ON t.CUST_ID=c.CUST_ID WHERE c.CUST_ID IS NULL",
    ])
    ws.append([
        "YBT_LOAN_003",
        "借据日期先后校验",
        "YBT_LOAN_RECEIPT",
        "START_DT,MATURE_DT",
        "强校验",
        "否",
        "借据到期日期 MATURE_DT 必须大于等于发放日期 START_DT",
        "SELECT * FROM YBT_LOAN_RECEIPT WHERE MATURE_DT < START_DT",
    ])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_east5_zip_bundle() -> bytes:
    """EAST 5.0 规范包（ZIP：Word 说明 + Excel 数据结构&码表 + Excel 检核规则清单）。"""
    # 1. Word
    doc = Document()
    doc.add_heading("银行业金融机构监管数据标准化规范（EAST 5.0 测试节选）", level=0)
    doc.add_heading("一、报送范围与 SM3 脱敏要求", level=1)
    doc.add_paragraph("所有自然人证件号码均需采用 SM3 杂凑算法脱敏后入库；仅报送未结清及当月结清借据。")
    doc_buf = io.BytesIO()
    doc.save(doc_buf)

    # 2. Excel 表结构与码表
    wb = Workbook()
    ws_idx = wb.active
    ws_idx.title = "目录"
    ws_idx.append(["主题域", "物理表名", "中文表名"])
    ws_idx.append(["客户域", "A_CORP_BASIC", "对公客户基础信息表"])
    ws_idx.append(["贷款域", "S_LOAN_JJ", "信贷借据表"])

    ws_a = wb.create_sheet("A_CORP_BASIC")
    ws_a.append(["字段中文名", "字段英文名", "数据类型", "是否必填", "代码项", "字段校验"])
    ws_a.append(["客户号", "CUST_ID", "VARCHAR(32)", "M", "", "必填，唯一"])
    ws_a.append(["证件类型", "CERT_TYPE", "CHAR(3)", "M", "证件类型代码表", "必填"])
    ws_a.append(["证件号码", "CERT_CODE", "VARCHAR(64)", "M", "", "必填，需经 SM3 脱敏"])

    ws_s = wb.create_sheet("S_LOAN_JJ")
    ws_s.append(["字段中文名", "字段英文名", "数据类型", "是否必填", "字段校验"])
    ws_s.append(["借据号", "JJH", "VARCHAR(40)", "M", "必填"])
    ws_s.append(["初始借款金额", "CJKJE", "DECIMAL(18,2)", "M", "必填，大于 0"])
    ws_s.append(["借据余额", "JJYE", "DECIMAL(18,2)", "M", "必填，不得大于初始借款金额"])

    ws_cs = wb.create_sheet("证件类型代码表")
    ws_cs.append(["代码", "中文名称"])
    ws_cs.append(["201", "统一社会信用代码"])
    ws_cs.append(["202", "组织机构代码"])

    xlsx_buf = io.BytesIO()
    wb.save(xlsx_buf)

    # 3. 检核规则 Excel
    wb_r = Workbook()
    ws_r = wb_r.active
    ws_r.title = "EAST5检核规则"
    ws_r.append(["规则编号", "规则名称", "表名", "字段名", "校验类型", "跨表标志", "校验逻辑", "检核SQL"])
    ws_r.append([
        "EAST_LN_01",
        "借据余额不大于借款金额",
        "S_LOAN_JJ",
        "JJYE,CJKJE",
        "强校验",
        "否",
        "借据余额 JJYE 不得大于初始借款金额 CJKJE",
        "SELECT * FROM S_LOAN_JJ WHERE JJYE > CJKJE",
    ])
    rule_buf = io.BytesIO()
    wb_r.save(rule_buf)

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("EAST5.0_报送业务说明.docx", doc_buf.getvalue())
        zf.writestr("EAST5.0_数据结构与代码表.xlsx", xlsx_buf.getvalue())
        zf.writestr("EAST5.0_监管标准化检核规则.xlsx", rule_buf.getvalue())
    return zbuf.getvalue()


def build_1104_bundle() -> Dict[str, bytes]:
    """1104 非现场报表三件套：G01/G04 二维表样模板（含合并表头与 =SUM 公式）+ 校验关系表 + Word 填报说明。"""
    # 1. 表样 Excel (2D matrix + formulas)
    wb_t = Workbook()
    ws = wb_t.active
    ws.title = "G01资产负债项目统计表"
    ws.append(["G01 资产负债项目统计表（第 251 版）"])
    ws.merge_cells("A1:D1")
    ws.append(["项目", "行次", "人民币", "本外币合计"])
    ws.append(["一、资产项目", "1", "", ""])
    ws.append(["1. 现金及存放中央银行款项", "2", 1000.0, "=C4"])
    ws.append(["2. 各项贷款", "3", 5000.0, "=C5"])
    ws.append(["资产总计", "4", "=SUM(C4:C5)", "=SUM(D4:D5)"])

    t_buf = io.BytesIO()
    wb_t.save(t_buf)

    # 2. 校验关系表 Excel
    wb_chk = Workbook()
    ws_chk = wb_chk.active
    ws_chk.title = "G01校验关系"
    ws_chk.append(["校验编号", "校验类型", "左侧表达式", "关系符", "右侧表达式", "容差范围", "校验说明"])
    ws_chk.append(["CHK_G01_01", "表内校验", "G01!C6", "=", "SUM(G01!C4:C5)", "0", "资产总计=现金+各项贷款"])
    ws_chk.append(["CHK_G01_G11_02", "表间校验", "G01!D5", "=", "G11!D20", "100", "G01 各项贷款合计与 G11 五级分类贷款总额一致"])
    chk_buf = io.BytesIO()
    wb_chk.save(chk_buf)

    # 3. 填报说明 Word
    doc = Document()
    doc.add_heading("1104 非现场监管报表 G01 填报说明", level=0)
    doc.add_heading("一、指标口径", level=1)
    doc.add_paragraph("行次 4 资产总计应与行次 2 至行次 3 之和严格平衡，跨表须与 G11 五级分类贷款保持同源一致。")
    w_buf = io.BytesIO()
    doc.save(w_buf)

    return {
        "1104_G01_表样模板.xlsx": t_buf.getvalue(),
        "1104_校验关系表.xlsx": chk_buf.getvalue(),
        "1104_G01_填报说明.docx": w_buf.getvalue(),
    }


def export_all_samples(target_dir: Path) -> Dict[str, Path]:
    """将四套监管混装真实测试样本落盘到指定目录，便于手工测试或脚本直采。"""
    written: Dict[str, Path] = {}
    bundles = {
        "safe_bundle/SAFE_涉外收入接口规范_Word.docx": build_safe_word_docx(),
        "safe_bundle/SAFE_公共代码表.xlsx": build_safe_codeset_xlsx(),
        "ybt_2_0_bundle/一表通2.0_业务与填报口径规范.docx": build_ybt_word_docx(),
        "ybt_2_0_bundle/一表通2.0_数据字典与代码表.xlsx": build_ybt_excel_dict_xlsx(),
        "ybt_2_0_bundle/一表通2.0_可信区检核规则清单.xlsx": build_ybt_rules_xlsx(),
        "east5_bundle/EAST5.0_标准规范包.zip": build_east5_zip_bundle(),
    }
    for name, data in build_1104_bundle().items():
        bundles[f"r1104_bundle/{name}"] = data

    for rel, content in bundles.items():
        dest = target_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        written[rel] = dest
    return written
