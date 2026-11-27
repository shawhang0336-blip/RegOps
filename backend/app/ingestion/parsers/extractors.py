"""纯离线多格式监管文档拆箱器（PDF / DOCX / XLSX / ZIP / 遗留 Office，零 DB、零 LLM）。

面向真实多监管体系混装文档包（SAFE 外管局、一表通 2.0、EAST 5.0、1104 非现场报表、人行征信/反洗钱）：
- ZIP 压缩包安全拆解（防 Zip Slip 路径穿越与解压炸弹）；
- Word (`.docx`)：标题树上下文绑定（表前引言 + 表后字段/校验说明）、XML 合并单元格还原、多级表头折叠、角色分流；
- Excel (`.xlsx`)：双模式读取（展示值 + 公式线索）、表头行自动探测（前 15 行）、单 Sheet 多表切分、目录/矩阵(1104)/检核规则/码表/明细字典识别；
- 通用与高精 PDF：外管局高精插件 → 通用章节树切块 → 分页窗口兜底。
"""

import hashlib
import io
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from openpyxl import Workbook, load_workbook

from app.infra.contracts import canonical_json
from app.ingestion.parsers.pdf_catalog import parse_document_catalog, parse_document_code_sets
from app.ingestion.parsers.pdf_generic import (
    default_pdf_strategy,
    detect_pdf_profiles,
    extract_pdf_generic_sections,
    extract_pdf_headings,
    extract_pdf_page_windows,
)
from app.ingestion.parsers.pdf_parser import SpecParseError, _extract_pdf_text

# 单 Sheet 行上限：20,000 行可完整覆盖行政区划（3,200+ 行）等大型监管码表，同时防百万行恶意表格撑爆内存
MAX_XLSX_ROWS = 20000
MAX_ZIP_FILES = 64
MAX_ZIP_TOTAL_BYTES = 80 * 1024 * 1024
SUPPORTED_INNER_EXTS = {"pdf", "docx", "xlsx", "doc", "xls"}

CODE_HEADER_VAL_ALIASES = {
    "value", "code", "码值", "代码", "编码", "代码值", "枚举值", "码值代码", "选项代码",
    "国家/地区代码", "币种代码", "行业分类代码", "外汇局系统行业标识码", "行政区划代码",
    "交易代码", "商品类别代码", "账户性质代码", "结汇用途代码", "表号",
}
CODE_HEADER_LABEL_ALIASES = {
    "label_cn", "name", "含义", "中文含义", "名称", "中文名称", "代码名称", "码值含义",
    "中文简称", "中文全称", "中文释义", "代码说明", "枚举含义", "行业类别名称", "币种名称",
    "行政区划名称", "商品类别名称", "账户性质名称", "结汇用途", "表名", "业务类型",
}
META_HEADER_ALIASES = {
    "field_en", "field_cn", "字段", "字段名", "英文名", "字段英文名", "物理字段名", "英文标识",
    "字段中文名", "数据项", "数据项名称", "数据项中文名", "数据项代码", "类型（长度）", "数据类型",
    "数据格式", "字段校验", "校验", "数据长度", "是否必填", "是否主键", "代码项",
}
RULE_HEADER_HINTS = {
    "规则编号", "校验编号", "检核编号", "检核规则", "校验逻辑", "检核逻辑", "校验公式",
    "检核sql", "校验sql", "左侧表达式", "右侧表达式", "强弱校验", "跨表标志", "校验类型",
}
CATALOG_HEADER_CODE_HINTS = {"表号", "物理表名", "英文表名", "表英文名", "报表编号", "table_code"}
CATALOG_HEADER_NAME_HINTS = {"表名", "中文表名", "表中文名", "报表名称", "table_name"}
IGNORE_TABLE_HINTS = {"修订人", "修订日期", "修改记录", "版本历史", "修订版本号", "页码"}
MATRIX_DIMENSION_HINTS = ("本外币", "人民币", "外币", "期末", "期初", "年初", "本月", "本年", "累计", "平均", "上年")
MATRIX_ROW_HINTS = ("行次", "指标代码", "指标编号", "项目", "指标名称")

_NOISE_CODE_TOKENS = {
    "DOCX", "XLSX", "PDF", "TABLE", "SHEET", "HTTP", "HTTPS", "JSON", "XML",
    "HTML", "UTF", "VARCHAR", "VARCHAR2", "NUMBER", "DECIMAL", "CHAR", "DATE",
    "TIMESTAMP", "TRUE", "FALSE", "NULL", "NONE", "AUTO", "SAFE", "EAST", "PBOC",
}

ALLOWED_STRATEGIES_BY_FILE_TYPE: Dict[str, Tuple[str, ...]] = {
    "pdf": ("SAFE_SPEC_PARSER", "HEADING_SECTIONS", "PAGE_WINDOWS", "DOCUMENT_CATALOG"),
    "docx": ("WORD_TABLE_AND_PARAGRAPH",),
    "xlsx": ("SHEET_PER_TABLE",),
}


def _block(
    ordinal: int,
    block_type: str,
    raw_content: str,
    source_locator: Dict[str, Any],
    content_json: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "ordinal": ordinal,
        "block_type": block_type,
        "raw_content": raw_content,
        "source_locator": source_locator,
        "content_json": content_json,
        "raw_hash": hashlib.sha256(canonical_json(content_json).encode("utf-8")).hexdigest(),
    }


def unpack_zip_archive(
    content: bytes,
    *,
    max_files: int = MAX_ZIP_FILES,
    max_total_bytes: int = MAX_ZIP_TOTAL_BYTES,
) -> List[Tuple[str, bytes]]:
    """安全解压监管规范 ZIP 包（防御 Zip Slip 路径穿越、符号链接与解压炸弹）。"""
    extracted: List[Tuple[str, bytes]] = []
    total_bytes = 0
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            raw_name = (info.filename or "").replace("\\", "/")
            if not raw_name or raw_name.startswith("/") or raw_name.startswith("__MACOSX/"):
                raise ValueError(f"Unsafe or malformed ZIP entry path: {info.filename}")
            parts = [p for p in raw_name.split("/") if p]
            if any(p == ".." for p in parts):
                raise ValueError(f"Zip Slip path traversal blocked: {info.filename}")
            leaf = parts[-1] if parts else ""
            if not leaf or leaf.startswith(".") or leaf.startswith("~$"):
                continue
            ext = Path(leaf).suffix.lower().lstrip(".")
            if ext not in SUPPORTED_INNER_EXTS:
                continue
            if len(extracted) >= max_files:
                raise ValueError(f"ZIP archive exceeds maximum {max_files} supported files")
            data = zf.read(info)
            total_bytes += len(data)
            if total_bytes > max_total_bytes:
                raise ValueError("ZIP archive uncompressed size exceeds limit")
            extracted.append((raw_name, data))
    if not extracted:
        raise ValueError("ZIP archive contains no supported PDF/DOCX/XLSX files")
    return extracted


def convert_legacy_office_bytes(filename: str, content: bytes) -> Tuple[str, bytes]:
    """将遗留 `.doc` / `.xls` 附件转换为 `.docx` / `.xlsx`（缺少本机依赖时返回清晰中文指引）。"""
    clean_name = Path(filename or "legacy").name
    ext = Path(clean_name).suffix.lower().lstrip(".")
    stem = Path(clean_name).stem or "document"
    if ext == "doc":
        soffice = shutil.which("soffice") or shutil.which("libreoffice")
        if not soffice:
            raise ValueError("检测到旧版 .doc 文件，当前环境未安装 LibreOffice，请先另存为 .docx 后上传")
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / clean_name
            src.write_bytes(content)
            proc = subprocess.run(
                [soffice, "--headless", "--convert-to", "docx", "--outdir", tmpdir, str(src)],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            out_path = Path(tmpdir) / f"{stem}.docx"
            if proc.returncode != 0 or not out_path.exists():
                raise ValueError(f"旧版 .doc 转换失败：{proc.stderr.strip() or proc.stdout.strip() or '未知错误'}")
            return f"{stem}.docx", out_path.read_bytes()
    if ext == "xls":
        try:
            import xlrd  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ValueError("检测到旧版 .xls 文件，当前环境未安装 xlrd，请先另存为 .xlsx 后上传") from exc
        book = xlrd.open_workbook(file_contents=content)
        wb = Workbook()
        # 移除默认空白页后再按真实 sheet 名写入
        wb.remove(wb.active)
        for sheet in book.sheets():
            ws = wb.create_sheet(title=sheet.name[:31])
            for r_idx in range(sheet.nrows):
                ws.append(sheet.row_values(r_idx))
        buf = io.BytesIO()
        wb.save(buf)
        wb.close()
        return f"{stem}.xlsx", buf.getvalue()
    raise ValueError(f"Unsupported legacy Office extension: {ext}")


def _norm_cell(val: Any) -> str:
    return re.sub(r"\s+", " ", str(val or "")).strip()


def _norm_header(val: Any) -> str:
    return re.sub(r"\s+", "", str(val or "")).lower()


def _extract_table_code_hint(text: str, table_index_map: Optional[Dict[str, Dict[str, str]]] = None) -> str:
    clean = str(text or "").strip()
    if not clean:
        return ""
    if table_index_map:
        norm_title = re.sub(r"[\s_\-：:（）()]+", "", clean).lower()
        for key, entry in table_index_map.items():
            if key and (key == norm_title or key in norm_title or norm_title in key):
                if entry.get("table_code"):
                    return str(entry["table_code"]).strip().upper()
    # 优先匹配带下划线的物理表名 (YBT_CUST_BASE / S_LOAN_JJ / A_CORP_BASIC) 或标准表号 (A01-1 / G01 / F01 / Z01)
    for pattern in (
        r"\b([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)\b",
        r"(?:表号|表名|报表代码|表)[：:\s]*([A-Za-z]\d{1,3}(?:-[A-Za-z0-9]+)?|[A-Za-z]{2,8}\d{0,4})\b",
        r"\b([GS]\d{2}(?:[_\-][IVX\d]+)?|[A-Z]\d{2}(?:-\d)?)\b",
        r"\b([A-Za-z][A-Za-z0-9_]{2,31})\b",
    ):
        for m in re.finditer(pattern, clean):
            cand = m.group(1).strip().upper()
            if cand and cand not in _NOISE_CODE_TOKENS and not cand.isdigit():
                return cand[:90]
    return ""


def _sniff_tabular_code_set(title: str, headers: List[str], rows: List[List[str]]) -> Optional[Dict[str, Any]]:
    norm_headers = [_norm_header(h) for h in headers]
    if any(h in {m.lower() for m in META_HEADER_ALIASES} for h in norm_headers):
        return None
    if any(h in {r.lower() for r in RULE_HEADER_HINTS} for h in norm_headers):
        return None
    val_idx = next(
        (
            i
            for i, h in enumerate(norm_headers)
            if h in {a.lower() for a in CODE_HEADER_VAL_ALIASES}
            or "代码值" in h
            or "码值" in h
            or "枚举" in h
            or h.endswith("代码")
            or h.endswith("编码")
        ),
        None,
    )
    label_idx = next(
        (
            i
            for i, h in enumerate(norm_headers)
            if i != val_idx
            and (
                h in {a.lower() for a in CODE_HEADER_LABEL_ALIASES}
                or "名称" in h
                or "含义" in h
                or "释义" in h
                or "简称" in h
            )
        ),
        None,
    )
    if val_idx is None or label_idx is None:
        return None
    en_idx = next((i for i, h in enumerate(norm_headers) if i not in {val_idx, label_idx} and ("英文" in h or "label_en" in h)), None)
    def_idx = next(
        (
            i
            for i, h in enumerate(norm_headers)
            if i not in {val_idx, label_idx, en_idx} and ("说明" in h or "解释" in h or "定义" in h or "备注" in h or "definition" in h)
        ),
        None,
    )

    values = []
    seen = set()
    for row in rows[1:]:
        if val_idx >= len(row) or label_idx >= len(row):
            continue
        v = str(row[val_idx] or "").strip()
        lbl = str(row[label_idx] or "").strip()
        if not v or not lbl or v in seen:
            continue
        seen.add(v)
        values.append(
            {
                "value": v,
                "label_cn": lbl,
                "label_en": str(row[en_idx] or "").strip() if en_idx is not None and en_idx < len(row) else "",
                "definition": str(row[def_idx] or "").strip() if def_idx is not None and def_idx < len(row) else "",
            }
        )
    if not values:
        return None
    clean_code = re.sub(r"[^A-Z0-9_]", "_", title.upper()).strip("_")
    if not clean_code or not re.match(r"^[A-Z]", clean_code):
        clean_code = f"CS_{hashlib.sha1(title.encode('utf-8')).hexdigest()[:8].upper()}"
    return {
        "code": clean_code[:90],
        "name": title,
        "definition": f"从表格 {title} 结构化提取的码值字典",
        "source_type": "DOCUMENT_TABLE",
        "values": values,
        "eligible": True,
    }


def _row_header_score(row: List[str]) -> int:
    norms = [_norm_header(c) for c in row if _norm_header(c)]
    if not norms:
        return -10
    distinct = len(set(norms))
    if distinct == 1 and len(row) > 1:
        # 整行合并横幅（通常是表标题/说明，绝非表头）
        return -5
    score = 0
    meta_lower = {m.lower() for m in META_HEADER_ALIASES}
    code_val_lower = {c.lower() for c in CODE_HEADER_VAL_ALIASES}
    code_lbl_lower = {c.lower() for c in CODE_HEADER_LABEL_ALIASES}
    rule_lower = {r.lower() for r in RULE_HEADER_HINTS}
    cat_lower = {c.lower() for c in (CATALOG_HEADER_CODE_HINTS | CATALOG_HEADER_NAME_HINTS)}
    for h in norms:
        if h in meta_lower or any(k in h for k in ("字段", "数据项", "类型", "长度", "校验", "必填", "格式", "代码项")):
            score += 4
        elif h in rule_lower or any(k in h for k in ("规则", "检核", "表达式", "公式", "sql")):
            score += 4
        elif h in code_val_lower or h in code_lbl_lower:
            score += 3
        elif h in cat_lower:
            score += 3
        elif h in {"序号", "no", "主题域", "所属模块", "说明", "备注"}:
            score += 1
        elif len(h) > 25:
            score -= 2
    if any("指标" in h or "行次" in h for h in norms) and any(d in "".join(norms) for d in MATRIX_DIMENSION_HINTS):
        score += 6
    return score


def _locate_header_and_rows(rows: List[List[str]], max_scan: int = 12) -> Tuple[int, List[str], List[List[str]], List[str]]:
    """探测表格真实表头行（支持前导说明行与两级合并表头折叠），返回 (header_idx, headers, data_rows, notes)。"""
    if not rows:
        return 0, [], [], []
    scan_limit = min(len(rows), max_scan)
    best_idx = 0
    best_score = -999
    for idx in range(scan_limit):
        sc = _row_header_score(rows[idx])
        if sc > best_score:
            best_score = sc
            best_idx = idx

    notes: List[str] = []
    for pre in rows[:best_idx]:
        dedup: List[str] = []
        for c in pre:
            nc = _norm_cell(c)
            if nc and (not dedup or dedup[-1] != nc):
                dedup.append(nc)
        joined = " ".join(dedup)
        if joined:
            notes.append(joined)

    header_row = list(rows[best_idx])
    data_start = best_idx + 1
    # 两级表头检测：必须存在上一行横向跨列重复或空槽位，且下一行符合子表头特征，才将下一行折叠进表头
    if data_start < len(rows):
        nxt = list(rows[data_start])
        nxt_norms = [_norm_header(c) for c in nxt]
        cur_norms = [_norm_header(c) for c in header_row]
        sub_hints = ("总长", "小数", "精度", "中文", "英文", "人民币", "外币", "合计", "期初", "期末", "借方", "贷方")
        width_cmp = min(len(cur_norms), len(nxt_norms))
        has_parent_span = any(
            cur_norms[i] == cur_norms[i - 1] and cur_norms[i]
            for i in range(1, width_cmp)
        )
        has_blank_slot = any(
            (not cur_norms[i]) and bool(nxt_norms[i])
            for i in range(width_cmp)
        )
        looks_like_sub = (has_parent_span or has_blank_slot) and (
            any(any(s in h for s in sub_hints) for h in nxt_norms if h)
            or not any(re.fullmatch(r"\d+(?:\.\d+)?", c or "") for c in nxt_norms if c)
        )
        if looks_like_sub and _row_header_score(nxt) >= 0:
            merged_headers: List[str] = []
            width = max(len(header_row), len(nxt))
            for col_i in range(width):
                top = _norm_cell(header_row[col_i]) if col_i < len(header_row) else ""
                sub = _norm_cell(nxt[col_i]) if col_i < len(nxt) else ""
                if top and sub and top != sub:
                    merged_headers.append(f"{top}_{sub}")
                else:
                    merged_headers.append(sub or top)
            header_row = merged_headers
            data_start += 1

    clean_headers = [_norm_cell(h) for h in header_row]
    data_rows = rows[data_start:]
    return best_idx, clean_headers, data_rows, notes


def _sniff_catalog_index(headers: List[str], data_rows: List[List[str]]) -> Optional[Dict[str, Dict[str, str]]]:
    norm_headers = [_norm_header(h) for h in headers]
    if any(h in {m.lower() for m in META_HEADER_ALIASES if m not in {"表号", "表名"}} for h in norm_headers):
        # 含字段级列（字段英文名/数据类型），说明不是纯目录页
        if any(k in "".join(norm_headers) for k in ("字段", "数据项", "类型", "长度")):
            return None
    code_idx = next(
        (i for i, h in enumerate(norm_headers) if h in {c.lower() for c in CATALOG_HEADER_CODE_HINTS} or "物理表" in h or "英文表" in h or "表号" in h),
        None,
    )
    name_idx = next(
        (i for i, h in enumerate(norm_headers) if i != code_idx and (h in {c.lower() for c in CATALOG_HEADER_NAME_HINTS} or "中文表" in h or "表名" in h)),
        None,
    )
    if code_idx is None or name_idx is None:
        return None
    domain_idx = next((i for i, h in enumerate(norm_headers) if i not in {code_idx, name_idx} and ("主题" in h or "模块" in h or "类别" in h or "sheet" in h)), None)
    mapping: Dict[str, Dict[str, str]] = {}
    for row in data_rows:
        if code_idx >= len(row) or name_idx >= len(row):
            continue
        tcode = _norm_cell(row[code_idx]).upper()
        tname = _norm_cell(row[name_idx])
        if not tcode or not tname:
            continue
        domain = _norm_cell(row[domain_idx]) if domain_idx is not None and domain_idx < len(row) else ""
        entry = {"table_code": tcode, "table_name": tname, "domain": domain}
        for key_raw in (tcode, tname):
            norm_k = re.sub(r"[\s_\-：:（）()]+", "", key_raw).lower()
            if norm_k:
                mapping[norm_k] = entry
    return mapping or None


def _sniff_matrix_report(
    title: str,
    headers: List[str],
    data_rows: List[List[str]],
    formula_hints: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    norm_headers = [_norm_header(h) for h in headers]
    joined_headers = "".join(norm_headers)
    # 若包含明确字段定义列，属于关系表字典，绝不误判为矩阵报表
    if any(k in joined_headers for k in ("字段英文名", "field_en", "数据类型", "data_type", "字段校验", "检核sql")):
        return None
    has_row_col = any(any(k in h for k in MATRIX_ROW_HINTS) for h in norm_headers)
    has_dim_col = sum(1 for h in norm_headers if any(d in h for d in MATRIX_DIMENSION_HINTS)) >= 1
    is_g_or_s_sheet = bool(re.match(r"^[GS]\d{2}(?:[_\-\s]|$)", title.strip(), re.IGNORECASE))
    if not ((has_row_col and (has_dim_col or is_g_or_s_sheet)) or (is_g_or_s_sheet and data_rows)):
        return None
    item_idx = next((i for i, h in enumerate(norm_headers) if "项目" in h or "指标名称" in h), 0)
    code_idx = next((i for i, h in enumerate(norm_headers) if "指标代码" in h or "行次" in h or "编号" in h), None)
    sample_indicators = []
    for row in data_rows[:20]:
        label = _norm_cell(row[item_idx]) if item_idx < len(row) else ""
        code = _norm_cell(row[code_idx]) if code_idx is not None and code_idx < len(row) else ""
        if label:
            sample_indicators.append({"code": code, "name": label})
    return {
        "report_code": _extract_table_code_hint(title) or title[:32],
        "title": title,
        "dimensions": [h for h in headers if h],
        "indicator_count": len([r for r in data_rows if any(_norm_cell(c) for c in r)]),
        "formula_count": len(formula_hints or []),
        "sample_indicators": sample_indicators[:12],
    }


def _extract_rule_rows(
    headers: List[str],
    data_rows: List[List[str]],
    *,
    default_table_code: str = "",
    default_table_name: str = "",
    locator: Optional[Dict[str, Any]] = None,
    table_index_map: Optional[Dict[str, Dict[str, str]]] = None,
) -> Optional[List[Dict[str, Any]]]:
    norm_headers = [_norm_header(h) for h in headers]
    if not any(
        h in {r.lower() for r in RULE_HEADER_HINTS}
        or any(k in h for k in ("规则编号", "校验编号", "检核编号", "校验逻辑", "检核逻辑", "校验公式", "检核sql", "左侧表达式"))
        for h in norm_headers
    ):
        return None

    def _find_col(*patterns: str) -> Optional[int]:
        for pat in patterns:
            for i, h in enumerate(norm_headers):
                if h == pat.lower() or pat.lower() in h:
                    return i
        return None

    code_i = _find_col("规则编号", "校验编号", "检核编号", "规则代码")
    name_i = _find_col("规则名称", "检核名称", "校验项目")
    tbl_i = _find_col("表号", "物理表名", "英文表名", "表名", "目标表", "报表代码")
    tname_i = _find_col("中文表名", "报表名称")
    fld_i = _find_col("字段名", "字段英文名", "数据项", "涉及字段", "目标字段")
    sev_i = _find_col("校验类型", "强弱校验", "校验级别", "规则级别", "规则类型")
    scope_i = _find_col("跨表标志", "校验范围", "是否跨表")
    text_i = _find_col("校验逻辑", "检核逻辑", "校验规则", "检核规则", "校验公式", "规则说明", "核查规则", "校验内容")
    left_i = _find_col("左侧表达式")
    op_i = _find_col("关系符", "比较符")
    right_i = _find_col("右侧表达式")
    tol_i = _find_col("容差")
    sql_i = _find_col("检核sql", "校验sql", "sql")

    if text_i is None and left_i is None and sql_i is None:
        return None

    results: List[Dict[str, Any]] = []
    for r_idx, row in enumerate(data_rows):
        if not any(_norm_cell(c) for c in row):
            continue
        get = lambda idx: _norm_cell(row[idx]) if idx is not None and idx < len(row) else ""
        rule_code = get(code_i)
        rule_name = get(name_i)
        raw_tbl = get(tbl_i)
        raw_tname = get(tname_i)
        raw_fields = get(fld_i)
        sev_raw = get(sev_i)
        scope_raw = get(scope_i)
        ref_sql = get(sql_i)

        if left_i is not None and right_i is not None:
            left_s, op_s, right_s, tol_s = get(left_i), get(op_i) or "=", get(right_i), get(tol_i)
            rule_text = f"{left_s} {op_s} {right_s}".strip()
            if tol_s and tol_s not in {"0", "0.0"}:
                rule_text += f"（容差 <= {tol_s}）"
            if text_i is not None and get(text_i):
                rule_text = f"{get(text_i)}：{rule_text}"
        else:
            rule_text = get(text_i)
        if not rule_text and not ref_sql:
            continue

        # 确定目标表号与表名
        tcode = _extract_table_code_hint(raw_tbl, table_index_map) or raw_tbl.upper()
        if not tcode and left_i is not None:
            m_tbl = re.findall(r"\b([GS]\d{2}|[A-Z]\d{2}(?:-\d)?|[A-Z][A-Z0-9_]{2,})!", f"{get(left_i)} {get(right_i)}")
            if m_tbl:
                tcode = m_tbl[0].upper()
        tcode = (tcode or default_table_code or "GENERAL").upper()[:90]
        tname = raw_tname or default_table_name

        # 强/弱/表间分类
        severity = "SOFT" if any(k in sev_raw for k in ("弱", "提示", "人工", "警告", "WARN")) else "HARD"
        is_cross = (
            scope_raw in {"是", "1", "Y", "YES", "TRUE"}
            or "跨表" in scope_raw
            or "表间" in scope_raw
            or "表间" in sev_raw
            or bool(re.search(r"[、，,;；和与]", raw_tbl))
        )
        scope = "CROSS_TABLE" if is_cross else "SINGLE_TABLE"

        target_fields = [
            f.strip()
            for f in re.split(r"[,，、;；\s/]+", raw_fields)
            if f.strip() and re.match(r"^[A-Za-z_][\u4e00-\u9fffA-Za-z0-9_]*$", f.strip())
        ]
        if not target_fields and raw_fields:
            target_fields = [f.strip() for f in re.split(r"[,，、;；]+", raw_fields) if f.strip()]

        rule_hash = hashlib.sha256(
            canonical_json(
                {
                    "table_code": tcode,
                    "rule_code": rule_code,
                    "rule_text": rule_text,
                    "reference_sql": ref_sql,
                }
            ).encode("utf-8")
        ).hexdigest()
        results.append(
            {
                "table_code": tcode,
                "table_name": tname,
                "rule_code": rule_code[:90],
                "rule_name": rule_name[:250],
                "severity": severity,
                "scope": scope,
                "target_fields": target_fields,
                "rule_text": rule_text or ref_sql,
                "reference_sql": ref_sql,
                "source_locator": {**(locator or {}), "row_index": r_idx + 1},
                "rule_hash": rule_hash,
            }
        )
    return results or None


def _classify_tabular_region(
    title: str,
    headers: List[str],
    data_rows: List[List[str]],
    *,
    region_role: Optional[str] = None,
    formula_hints: Optional[List[Dict[str, Any]]] = None,
    table_index_map: Optional[Dict[str, Dict[str, str]]] = None,
    locator: Optional[Dict[str, Any]] = None,
    default_table_code: str = "",
) -> Tuple[str, Dict[str, Any]]:
    norm_headers = [_norm_header(h) for h in headers]
    if any(h in {x.lower() for x in IGNORE_TABLE_HINTS} for h in norm_headers) and not any(
        h in {m.lower() for m in META_HEADER_ALIASES} for h in norm_headers
    ):
        return "IGNORE", {"reason": "修订记录/目录等非规范实体表"}

    cat_map = _sniff_catalog_index(headers, data_rows)
    if cat_map:
        return "CATALOG_INDEX", {
            "candidate_role": "CATALOG_INDEX",
            "eligible": False,
            "table_index_map": cat_map,
            "reason": f"目录/表清单索引（共登记 {len(cat_map) // 2 or len(cat_map)} 张表，不单独建表）",
        }

    matrix = _sniff_matrix_report(title, headers, data_rows, formula_hints)
    if matrix:
        return "MATRIX_REPORT", {
            "candidate_role": "MATRIX_REPORT",
            "eligible": False,
            "matrix_profile": matrix,
            "formula_hints": (formula_hints or [])[:100],
            "reason": f"检测到二维指标矩阵报表（{matrix['report_code']}，{matrix['indicator_count']} 项指标），当前暂不按关系明细表建表",
        }

    rules = _extract_rule_rows(
        headers,
        data_rows,
        default_table_code=default_table_code,
        default_table_name=title,
        locator=locator,
        table_index_map=table_index_map,
    )
    if rules:
        return "RULE_EVIDENCE", {
            "candidate_role": "RULE_EVIDENCE",
            "eligible": False,
            "rule_rows": rules,
            "formula_hints": (formula_hints or [])[:50],
            "field_count": len(rules),
            "reason": f"结构化校验/检核规则清单（提取 {len(rules)} 条规则证据）",
        }

    full_rows = [headers, *data_rows]
    code_spec = _sniff_tabular_code_set(title, headers, full_rows)
    if code_spec or region_role == "CODE_SET":
        extra: Dict[str, Any] = {"candidate_role": "CODE_SET"}
        if code_spec:
            extra["code_set_spec"] = code_spec
            extra["reason"] = f"结构化码值表候选（{len(code_spec['values'])} 项）"
            extra["field_count"] = len(code_spec["values"])
        return "CODE_SET", extra

    has_meta_header = any(
        h in {m.lower() for m in META_HEADER_ALIASES}
        or any(k in h for k in ("字段", "数据项", "类型", "长度", "校验", "必填", "格式", "field", "column", "datatype"))
        for h in norm_headers
        if h
    )
    has_schema_rows = sum(
        1
        for r in data_rows[:40]
        if any(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,63}", _norm_cell(c)) for c in r[:4])
        or any(re.search(r"(?:VARCHAR|CHAR|NUMBER|DECIMAL|INT|DATE|字符型|数值型|C\.\.\d|N\.\.\d)", _norm_cell(c), re.I) for c in r[:6])
    ) >= 2
    if region_role not in {"TABLE_METADATA", "RULE_EVIDENCE", "NARRATIVE"} and not has_meta_header and not has_schema_rows and _row_header_score(headers) < 3:
        return "NARRATIVE", {
            "candidate_role": "NARRATIVE",
            "eligible": False,
            "reason": "非数据字典结构化表格（填报表单或展示表格），仅作上下文留痕",
        }

    extra_meta: Dict[str, Any] = {}
    if region_role in {"RULE_EVIDENCE", "NARRATIVE", "TABLE_METADATA"}:
        extra_meta["candidate_role"] = region_role
    if formula_hints:
        extra_meta["formula_hints"] = formula_hints[:50]
    return "TABLE_METADATA", extra_meta


def _region_roles_map(chunking_plan: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """把 AI ① 产出的 section_regions 索引为 {归一化标题/Sheet名: role}。"""
    if not isinstance(chunking_plan, dict):
        return {}
    regions = chunking_plan.get("section_regions")
    if not isinstance(regions, list):
        return {}
    mapping: Dict[str, str] = {}
    for item in regions:
        if not isinstance(item, dict):
            continue
        heading = re.sub(r"\s+", "", str(item.get("start_heading") or "")).lower()
        role = str(item.get("role") or "").upper()
        if heading and role in {"TABLE_METADATA", "CODE_SET", "RULE_EVIDENCE", "NARRATIVE", "IGNORE"}:
            mapping[heading] = role
    return mapping


def _lookup_region_role(role_map: Dict[str, str], label: str) -> Optional[str]:
    if not role_map or not label:
        return None
    norm = re.sub(r"\s+", "", label).lower()
    if norm in role_map:
        return role_map[norm]
    for key, role in role_map.items():
        if key in norm or norm in key:
            return role
    return None


def _extract_safe_pdf_blocks(
    resolved: Path,
    full_text: str,
    digest: str,
    chunking_plan: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    role_map = _region_roles_map(chunking_plan)
    entries = parse_document_catalog(str(resolved), full_text=full_text, digest=digest)
    blocks: List[Dict[str, Any]] = []
    for entry in entries:
        sec_label = f"{entry.get('section', '')} {entry.get('table_name', '')}".strip()
        if _lookup_region_role(role_map, sec_label) == "IGNORE":
            continue
        raw = (
            f"{entry['spec']['terms_text']}\n{entry['spec']['dictionary_text']}"
            if entry.get("spec")
            else entry["reason"]
        )
        blocks.append(
            _block(
                len(blocks),
                entry.get("candidate_role", "TABLE_CANDIDATE"),
                raw,
                {"section": entry["section"], "table_code": entry["table_code"], "strategy": "SAFE_SPEC_PARSER"},
                entry,
            )
        )
    code_sets = parse_document_code_sets(str(resolved), full_text=full_text)
    for cs in code_sets:
        cs_label = f"{cs.get('section', '')} {cs.get('name', '')}".strip()
        if _lookup_region_role(role_map, cs_label) == "IGNORE":
            continue
        blocks.append(
            _block(
                len(blocks),
                "CODE_SET_CANDIDATE",
                cs["raw_content"],
                {"section": cs["section"], "code_set_code": cs["code"], "page": cs["page"], "strategy": "SAFE_SPEC_PARSER"},
                {
                    "table_code": cs["code"],
                    "table_name": cs["name"],
                    "section": cs["section"],
                    "eligible": cs["eligible"],
                    "candidate_role": "CODE_SET",
                    "reason": f"公共代码表候选（预提取 {len(cs['values'])} 个码值项）",
                    "field_count": len(cs["values"]),
                    "code_set_spec": {k: v for k, v in cs.items() if k != "raw_content"},
                },
            )
        )
    version = next((entry["spec"]["version"] for entry in entries if entry.get("spec")), "UNKNOWN")
    return version, blocks


def extract_pdf(
    path: Path,
    chunking_plan: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """三级通用 PDF 抽块路由：高精已知规范插件 → 通用章节切块（支持 AI ① 蓝图）→ 分页窗口兜底。"""
    resolved = path.resolve()
    digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
    full_text = _extract_pdf_text(resolved)

    requested = (
        str((chunking_plan or {}).get("chunking_strategy") or "").strip().upper()
        if isinstance(chunking_plan, dict)
        else ""
    )
    if requested == "DOCUMENT_CATALOG":
        requested = "SAFE_SPEC_PARSER"
    if requested not in {"SAFE_SPEC_PARSER", "HEADING_SECTIONS", "PAGE_WINDOWS"}:
        requested = default_pdf_strategy(full_text)

    # 1) 一级：已知外管局高精解析器（未命中时优雅降级至通用章节/分页，绝不抛异常拒收陌生 PDF）
    if requested == "SAFE_SPEC_PARSER":
        try:
            version, blocks = _extract_safe_pdf_blocks(resolved, full_text, digest, chunking_plan=chunking_plan)
            if blocks:
                return version, blocks
        except SpecParseError:
            pass

    # 2) 二级：通用章节切块器（面向 EAST / 1104 / 人行征信 / 反洗钱等任意监管 PDF）
    if requested in {"SAFE_SPEC_PARSER", "HEADING_SECTIONS"}:
        generic_items = extract_pdf_generic_sections(full_text, resolved, digest, chunking_plan=chunking_plan)
        if generic_items:
            blocks = [
                _block(idx, item["block_type"], item["raw_content"], item["source_locator"], item["content_json"])
                for idx, item in enumerate(generic_items)
            ]
            return "GENERIC_PDF", blocks

    # 3) 三级：无章节标题 PDF 分页窗口兜底
    window_items = extract_pdf_page_windows(full_text, resolved, digest, chunking_plan=chunking_plan)
    blocks = [
        _block(idx, item["block_type"], item["raw_content"], item["source_locator"], item["content_json"])
        for idx, item in enumerate(window_items)
    ]
    return "GENERIC_PDF", blocks


def _iter_docx_body_items(document: Document):
    """按文档真实阅读顺序依次产出 Paragraph 与 Table，使每个表格能精确绑定上下相邻段落说明。"""
    parent_elm = document.element.body
    for child in parent_elm.iterchildren():
        if child.tag.endswith("}p"):
            yield Paragraph(child, document)
        elif child.tag.endswith("}tbl"):
            yield Table(child, document)


def _heading_level_docx(paragraph: Paragraph, text: str) -> Optional[int]:
    if not text or len(text) > 100:
        return None
    style_name = (getattr(paragraph.style, "name", "") or "").lower()
    m_style = re.search(r"(?:heading|标题)\s*(\d+)", style_name)
    if m_style:
        return max(1, min(6, int(m_style.group(1))))
    if "heading" in style_name or "标题" in style_name:
        return 1
    if re.match(r"^(?:第[一二三四五六七八九十百]+[章部分篇]|附[录表])", text):
        return 1
    if re.match(r"^[一二三四五六七八九十]+、\s*\S+", text):
        return 2
    if re.match(r"^(?:（[一二三四五六七八九十]+）|第[一二三四五六七八九十百]+节|表\s*[A-Za-z0-9\-_.]+)", text):
        return 3
    m_num = re.match(r"^(\d+(?:\.\d+){1,4})\s+\S+", text)
    if m_num:
        return min(6, 1 + m_num.group(1).count("."))
    if re.match(r"^\d+[.、]\s*\S+", text):
        return 3
    return None


def _docx_table_matrix(table: Table) -> List[List[str]]:
    """展开 Word 表格的横向合并（w:gridSpan）与纵向合并（w:vMerge），还原矩形二维行矩阵。"""
    grid_rows: List[List[str]] = []
    active_vmerge: Dict[int, str] = {}
    for tr in table._tbl.tr_lst:
        row_cells: List[str] = []
        col_idx = 0
        for tc in tr.tc_lst:
            tcPr = tc.tcPr
            grid_span = 1
            vmerge_mode: Optional[str] = None
            if tcPr is not None:
                gs = tcPr.find(qn("w:gridSpan"))
                if gs is not None and gs.get(qn("w:val")):
                    try:
                        grid_span = max(1, int(gs.get(qn("w:val"))))
                    except ValueError:
                        grid_span = 1
                vm = tcPr.find(qn("w:vMerge"))
                if vm is not None:
                    vmerge_mode = vm.get(qn("w:val")) or "continue"
            p_texts = [
                "".join((t.text or "") for t in p.findall(".//" + qn("w:t"))).strip()
                for p in tc.findall(qn("w:p"))
            ]
            text = _norm_cell(" ".join(pt for pt in p_texts if pt))
            for offset in range(grid_span):
                c_pos = col_idx + offset
                if vmerge_mode == "continue":
                    val = active_vmerge.get(c_pos, text)
                else:
                    val = text
                    if vmerge_mode == "restart":
                        active_vmerge[c_pos] = val
                    else:
                        active_vmerge.pop(c_pos, None)
                row_cells.append(val)
            col_idx += grid_span
        if row_cells:
            grid_rows.append(row_cells)
    if not grid_rows:
        return []
    width = max(len(r) for r in grid_rows)
    return [r + [""] * (width - len(r)) for r in grid_rows]


def extract_docx(
    path: Path,
    chunking_plan: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """章节树 + 上下文绑定的高精 Word 解析器（不截断、不断裂表格与其下方字段校验口径）。"""
    document = Document(str(path))
    role_map = _region_roles_map(chunking_plan)
    items = list(_iter_docx_body_items(document))

    # 第一遍：记录所有元素，并建立每个段落与表格在阅读流里的前后归属
    stream: List[Tuple[str, Any]] = []
    heading_stack: List[Tuple[int, str]] = []
    table_index = 0
    for item in items:
        if isinstance(item, Paragraph):
            text = item.text.strip()
            if not text:
                continue
            lvl = _heading_level_docx(item, text)
            if lvl is not None:
                while heading_stack and heading_stack[-1][0] >= lvl:
                    heading_stack.pop()
                heading_stack.append((lvl, text))
                stream.append(("HEADING", (lvl, text, [h for _, h in heading_stack])))
            else:
                stream.append(("PARA", (text, [h for _, h in heading_stack])))
        elif isinstance(item, Table):
            matrix = _docx_table_matrix(item)
            if matrix and any(any(c for c in r) for r in matrix):
                stream.append(("TABLE", (table_index, matrix, [h for _, h in heading_stack])))
            table_index += 1

    blocks: List[Dict[str, Any]] = []
    pending_tables: List[Dict[str, Any]] = []
    section_texts: Dict[str, List[str]] = {}
    table_index_map: Dict[str, Dict[str, str]] = {}

    for pos, (kind, data) in enumerate(stream):
        if kind in {"HEADING", "PARA"}:
            text = data[1] if kind == "HEADING" else data[0]
            h_path = data[2] if kind == "HEADING" else data[1]
            top_sec = h_path[0] if h_path else "正文"
            cur_heading = h_path[-1] if h_path else ""
            if _lookup_region_role(role_map, cur_heading) != "IGNORE":
                section_texts.setdefault(top_sec, []).append(text)
            continue

        idx, rows, h_path = data
        cur_heading = h_path[-1] if h_path else ""
        heading_path = " > ".join(h_path)
        region_role = _lookup_region_role(role_map, cur_heading)
        if region_role == "IGNORE":
            continue

        # 向后采集表后紧邻段落（直到下一个 Heading 或下一张 Table）
        after_parts: List[str] = []
        for look_pos in range(pos + 1, len(stream)):
            n_kind, n_data = stream[look_pos]
            if n_kind in {"HEADING", "TABLE"}:
                break
            after_parts.append(n_data[0])
        context_after = "\n".join(after_parts)[:4000]

        # 向前采集本节内在上一张 Table/Heading 之后的说明段落
        before_parts: List[str] = []
        for look_pos in range(pos - 1, -1, -1):
            p_kind, p_data = stream[look_pos]
            if p_kind in {"HEADING", "TABLE"}:
                break
            before_parts.append(p_data[0])
        before_parts.reverse()
        context_before = "\n".join(before_parts)[:2500]

        _, headers, data_rows, pre_notes = _locate_header_and_rows(rows)
        if pre_notes:
            context_before = "\n".join([*pre_notes, context_before]).strip()[:2500]
        if not headers and not data_rows:
            continue

        title_candidates = " ".join([*pre_notes, cur_heading, f"{path.stem}_表{idx + 1}"])
        title = pre_notes[0] if (pre_notes and len(pre_notes[0]) <= 80) else (cur_heading or f"{path.stem}_表{idx + 1}")
        tcode = _extract_table_code_hint(title_candidates, table_index_map)
        locator: Dict[str, Any] = {"table_index": idx, "row_count": len(data_rows) + 1}
        if cur_heading:
            locator["section"] = cur_heading[:64]
            locator["title"] = cur_heading[:120]
        if heading_path:
            locator["heading_path"] = heading_path[:200]
        if tcode:
            locator["table_code"] = tcode

        role, extra = _classify_tabular_region(
            title,
            headers,
            data_rows,
            region_role=region_role,
            table_index_map=table_index_map,
            locator=locator,
            default_table_code=tcode,
        )
        if role == "IGNORE":
            continue
        if role == "CATALOG_INDEX" and extra.get("table_index_map"):
            table_index_map.update(extra["table_index_map"])

        norm_rows = [headers, *data_rows]
        raw_table = "\n".join(" | ".join(r) for r in norm_rows)
        raw_parts = [p for p in (f"【章节路径】{heading_path}" if heading_path else "", f"【表前说明】\n{context_before}" if context_before else "", raw_table, f"【表后说明与校验口径】\n{context_after}" if context_after else "") if p]
        raw_content = "\n\n".join(raw_parts)

        payload: Dict[str, Any] = {
            "headers": headers,
            "rows": norm_rows,
            "table_index": idx,
            "table_name": title,
            "heading_path": heading_path,
            "context_before": context_before,
            "context_after": context_after,
            **extra,
        }
        if tcode:
            payload.setdefault("table_code", tcode)
        if cur_heading:
            payload["section"] = cur_heading[:64]

        block_type = role if role in {"CATALOG_INDEX", "MATRIX_REPORT"} else "DOCX_TABLE"
        pending_tables.append(
            _block(len(pending_tables), block_type, raw_content, locator, payload)
        )

    blocks.extend(pending_tables)

    # 非表正文按顶层章节分块保留（每块 ≤ 12,000 字），并自动识别独立核查规则 / 修订说明章节
    for sec_title, parts in section_texts.items():
        if any(tok in sec_title for tok in ("目录", "目 录", "封面")):
            continue
        joined = "\n".join(parts).strip()
        if not joined:
            continue
        if any(k in sec_title for k in ("核查规则", "校验规则", "勾稽关系", "检核规则")):
            btype, crole = "RULE_EVIDENCE", "RULE_EVIDENCE"
        elif any(k in sec_title for k in ("修订说明", "修订对照", "修改对照")):
            btype, crole = "REVISION_DIFF", "REVISION_DIFF"
        else:
            btype, crole = "NARRATIVE", "NARRATIVE"
        for offset in range(0, len(joined), 12000):
            slice_text = joined[offset : offset + 12000]
            blocks.append(
                _block(
                    len(blocks),
                    btype,
                    slice_text,
                    {"section": sec_title[:64], "paragraphs": len(parts)},
                    {
                        "text": slice_text,
                        "section": sec_title[:64],
                        "candidate_role": crole,
                        "eligible": False,
                    },
                )
            )
    return "UNKNOWN", blocks


def _expand_sheet_matrix(ws_val, ws_fml) -> Tuple[List[List[str]], Dict[int, List[Dict[str, Any]]]]:
    """双模式读取 Excel Sheet：结合展示值、公式线索及合并单元格还原。"""
    max_r = min(int(ws_val.max_row or 0), MAX_XLSX_ROWS)
    max_c = int(ws_val.max_column or 0)
    if max_r <= 0 or max_c <= 0:
        return [], {}

    raw_grid: List[List[str]] = []
    formulas_by_row: Dict[int, List[Dict[str, Any]]] = {}
    for r_i, (val_row, fml_row) in enumerate(
        zip(
            ws_val.iter_rows(min_row=1, max_row=max_r, max_col=max_c, values_only=True),
            ws_fml.iter_rows(min_row=1, max_row=max_r, max_col=max_c, values_only=True),
        )
    ):
        row_strs: List[str] = []
        for c_i in range(max_c):
            v = val_row[c_i] if c_i < len(val_row) else None
            f = fml_row[c_i] if c_i < len(fml_row) else None
            if isinstance(f, str) and f.startswith("="):
                coord = f"{ws_fml.cell(row=r_i + 1, column=c_i + 1).coordinate}"
                formulas_by_row.setdefault(r_i, []).append({"cell": coord, "formula": f, "evaluated": v})
                cell_val = _norm_cell(v) if v is not None else f
            else:
                cell_val = _norm_cell(v if v is not None else f)
            row_strs.append(cell_val)
        raw_grid.append(row_strs)

    # 展开工作表内合并单元格（跳过整表超大异常合并，上限前 20,000 行）
    for merged_range in ws_fml.merged_cells.ranges:
        min_r, min_c, m_max_r, m_max_c = (
            merged_range.min_row - 1,
            merged_range.min_col - 1,
            min(merged_range.max_row - 1, len(raw_grid) - 1),
            min(merged_range.max_col - 1, max_c - 1),
        )
        if min_r >= len(raw_grid) or min_c >= max_c:
            continue
        anchor = raw_grid[min_r][min_c]
        if not anchor:
            continue
        for rr in range(min_r, m_max_r + 1):
            for cc in range(min_c, m_max_c + 1):
                if not raw_grid[rr][cc]:
                    raw_grid[rr][cc] = anchor

    # 裁剪每行末尾空白列，同时保留全局下标对应
    return raw_grid, formulas_by_row


def _split_sheet_regions(raw_grid: List[List[str]]) -> List[Tuple[int, int, List[List[str]]]]:
    """按空白间隔行或新表头特征拆分单 Sheet 内的多张子表。"""
    regions: List[Tuple[int, int, List[List[str]]]] = []
    n = len(raw_grid)
    i = 0
    while i < n:
        while i < n and not any(raw_grid[i]):
            i += 1
        if i >= n:
            break
        start = i
        block_rows: List[List[str]] = []
        while i < n:
            if any(raw_grid[i]):
                block_rows.append(raw_grid[i])
                i += 1
            else:
                # 连续空行后，检查剩余非空区是否像独立新表（新标题 banner 或新表头）
                j = i
                while j < n and not any(raw_grid[j]):
                    j += 1
                if j >= n:
                    break
                lookahead = raw_grid[j : min(n, j + 4)]
                looks_like_new_table = any(_row_header_score(r) >= 4 for r in lookahead) or (
                    len({c for c in raw_grid[j] if c}) == 1 and len(lookahead) >= 2 and _row_header_score(lookahead[1]) >= 2
                )
                if looks_like_new_table and len(block_rows) >= 2:
                    break
                i = j
        trimmed: List[List[str]] = []
        for r in block_rows:
            vals = list(r)
            while vals and vals[-1] == "":
                vals.pop()
            if vals:
                trimmed.append(vals)
        if trimmed:
            regions.append((start, i - 1, trimmed))
    return regions


def extract_xlsx(
    path: Path,
    chunking_plan: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """双模式多表 Excel 解析器：支持目录 Sheet、表头自动探测、单 Sheet 多表拆分、公式留痕与 1104 矩阵分流。"""
    wb_val = load_workbook(path, read_only=False, data_only=True)
    wb_fml = load_workbook(path, read_only=False, data_only=False)
    role_map = _region_roles_map(chunking_plan)
    blocks: List[Dict[str, Any]] = []
    table_index_map: Dict[str, Dict[str, str]] = {}

    try:
        sheet_payloads: List[Tuple[Any, Any, List[List[str]], Dict[int, List[Dict[str, Any]]]]] = []
        for ws_val, ws_fml in zip(wb_val.worksheets, wb_fml.worksheets):
            if _lookup_region_role(role_map, ws_val.title) == "IGNORE":
                continue
            grid, fml_by_row = _expand_sheet_matrix(ws_val, ws_fml)
            if not grid:
                continue
            sheet_payloads.append((ws_val, ws_fml, grid, fml_by_row))

        # 第一遍：优先从目录/表清单 Sheet 预提取 table_code <-> table_name 映射，反哺后续各 Sheet
        for ws_val, _, grid, _ in sheet_payloads:
            if any(k in ws_val.title for k in ("目录", "表清单", "接口清单", "报表清单", "主题域")):
                for _, _, reg_rows in _split_sheet_regions(grid):
                    _, headers, data_rows, _ = _locate_header_and_rows(reg_rows)
                    cat = _sniff_catalog_index(headers, data_rows)
                    if cat:
                        table_index_map.update(cat)

        for ws_val, _, grid, fml_by_row in sheet_payloads:
            region_role = _lookup_region_role(role_map, ws_val.title)
            sheet_max_row = int(ws_val.max_row or 0)
            regions = _split_sheet_regions(grid)
            for reg_idx, (start_r, end_r, reg_rows) in enumerate(regions):
                _, headers, data_rows, notes = _locate_header_and_rows(reg_rows)
                if not headers and not data_rows:
                    continue
                sheet_notes = "\n".join(notes)[:2500]
                region_title = (
                    notes[0][:80]
                    if (notes and len(notes[0]) <= 80 and (len(regions) > 1 or _extract_table_code_hint(notes[0], table_index_map)))
                    else (ws_val.title if len(regions) == 1 else f"{ws_val.title}_{reg_idx + 1}")
                )
                region_formulas = [
                    hint
                    for r_i in range(start_r, end_r + 1)
                    for hint in fml_by_row.get(r_i, [])
                ]
                tcode = (
                    _extract_table_code_hint(ws_val.title, table_index_map)
                    or _extract_table_code_hint(region_title, table_index_map)
                    or _extract_table_code_hint(sheet_notes, table_index_map)
                )
                locator: Dict[str, Any] = {
                    "sheet": ws_val.title,
                    "region_index": reg_idx,
                    "row_count": len(data_rows) + 1,
                    "truncated": sheet_max_row > MAX_XLSX_ROWS,
                    "merged_ranges": [str(item) for item in ws_val.merged_cells.ranges][:50],
                }
                if tcode:
                    locator["table_code"] = tcode

                role, extra = _classify_tabular_region(
                    region_title,
                    headers,
                    data_rows,
                    region_role=region_role,
                    formula_hints=region_formulas,
                    table_index_map=table_index_map,
                    locator=locator,
                    default_table_code=tcode,
                )
                if role == "IGNORE":
                    continue
                if role == "CATALOG_INDEX" and extra.get("table_index_map"):
                    table_index_map.update(extra["table_index_map"])

                norm_rows = [headers, *data_rows]
                raw_table = "\n".join(" | ".join(r) for r in norm_rows)
                raw = f"【表前说明】\n{sheet_notes}\n\n{raw_table}" if sheet_notes else raw_table
                payload: Dict[str, Any] = {
                    "headers": headers,
                    "rows": norm_rows,
                    "sheet": ws_val.title,
                    "region_index": reg_idx,
                    "table_name": region_title,
                    "sheet_notes": sheet_notes,
                    **extra,
                }
                if tcode:
                    payload.setdefault("table_code", tcode)
                block_type = role if role in {"CATALOG_INDEX", "MATRIX_REPORT"} else "XLSX_SHEET"
                blocks.append(_block(len(blocks), block_type, raw, locator, payload))
    finally:
        wb_val.close()
        wb_fml.close()
    return "UNKNOWN", blocks


def infer_document_role(file_name: str, blocks: List[Dict[str, Any]]) -> str:
    """结合文件名与切块角色分布，自动识别多文件规范包中当前文件的角色。"""
    active = [b for b in blocks if not (b.get("content_json") or {}).get("superseded") and b.get("block_type") != "IGNORE"]
    roles = []
    for b in active:
        cj = b.get("content_json") or {}
        role = str(cj.get("candidate_role") or "")
        if not role:
            if b.get("block_type") in {"CODE_SET", "CODE_SET_CANDIDATE"}:
                role = "CODE_SET"
            elif b.get("block_type") in {"RULE_EVIDENCE", "REVISION_DIFF", "CATALOG_INDEX", "MATRIX_REPORT"}:
                role = str(b.get("block_type"))
            else:
                role = "TABLE_METADATA"
        roles.append(role)

    fname = (file_name or "").lower()
    if any(k in fname for k in ("修订", "对照", "revision")):
        return "REVISION_NOTE"
    if any(k in fname for k in ("核查规则", "检核规则", "校验规则", "校验关系", "勾稽")):
        if not any(r == "TABLE_METADATA" for r in roles):
            return "RULE_SUPPLEMENT"
    if any(k in fname for k in ("代码表", "码表", "交易代码", "字典代码", "编码表", "code_set", "code_table")):
        if not any(r == "TABLE_METADATA" for r in roles):
            return "CODE_SET_APPENDIX"

    meta_cnt = sum(1 for r in roles if r == "TABLE_METADATA")
    code_cnt = sum(1 for r in roles if r == "CODE_SET")
    rule_cnt = sum(1 for r in roles if r == "RULE_EVIDENCE")
    rev_cnt = sum(1 for r in roles if r == "REVISION_DIFF")
    ext = Path(fname).suffix.lower().lstrip(".")

    if rev_cnt and meta_cnt == 0 and rule_cnt == 0:
        return "REVISION_NOTE"
    if rule_cnt and meta_cnt == 0:
        return "RULE_SUPPLEMENT"
    if code_cnt and meta_cnt == 0:
        return "CODE_SET_APPENDIX"
    if ext == "xlsx" and meta_cnt > 0:
        return "SCHEMA_APPENDIX"
    if ext in {"docx", "pdf"} and (meta_cnt > 0 or any(r == "NARRATIVE" for r in roles)):
        return "MAIN_SPEC"
    return "MAIN_SPEC"


def survey_document(path: Path, file_type: str) -> Dict[str, Any]:
    """生成给 AI ① 整文档体检使用的结构化情报（带页码标题、结构指纹、目录页与矩阵报表摘要）。"""
    if file_type == "pdf":
        text = _extract_pdf_text(path)
        headings_info = extract_pdf_headings(text)
        profiles = detect_pdf_profiles(text)
        recommended = default_pdf_strategy(text, headings_info)
        mid_offset = len(text) // 3 if len(text) > 6000 else 0
        tail_offset = max(0, len(text) - 2000) if len(text) > 8000 else 0
        return {
            "file_type": file_type,
            "page_count": max(1, text.count("\f") + (1 if text.strip() else 0)),
            "detected_profiles": profiles,
            "headings": [item["text"] for item in headings_info[:250]],
            "headings_with_pages": [
                {"text": item["text"], "page": item["page"]} for item in headings_info[:200]
            ],
            "heading_count": len(headings_info),
            "content_preview": text[:3000],
            "body_preview": text[mid_offset : mid_offset + 1500] if mid_offset else "",
            "appendix_preview": text[tail_offset:] if tail_offset else "",
            "available_chunk_strategy": recommended,
            "allowed_chunk_strategies": list(ALLOWED_STRATEGIES_BY_FILE_TYPE["pdf"]),
        }
    if file_type == "docx":
        document = Document(str(path))
        paragraphs: List[str] = []
        headings: List[str] = []
        tables: List[Dict[str, Any]] = []
        current_heading = ""
        table_index = 0
        for item in _iter_docx_body_items(document):
            if isinstance(item, Paragraph):
                text = item.text.strip()
                if not text:
                    continue
                paragraphs.append(text)
                if _heading_level_docx(item, text) is not None:
                    current_heading = text
                    headings.append(text)
            elif isinstance(item, Table):
                if table_index < 80:
                    matrix = _docx_table_matrix(item)
                    tables.append(
                        {
                            "table_index": table_index,
                            "nearest_heading": current_heading,
                            "row_count": len(matrix),
                            "sample_rows": matrix[:4],
                        }
                    )
                table_index += 1
        return {
            "file_type": file_type,
            "paragraph_count": len(paragraphs),
            "headings": (headings or paragraphs)[:160],
            "table_count": table_index,
            "tables": tables,
            "available_chunk_strategy": "WORD_TABLE_AND_PARAGRAPH",
            "allowed_chunk_strategies": list(ALLOWED_STRATEGIES_BY_FILE_TYPE["docx"]),
        }
    if file_type == "xlsx":
        wb_val = load_workbook(path, read_only=False, data_only=True)
        wb_fml = load_workbook(path, read_only=False, data_only=False)
        sheets = []
        try:
            for ws_val, ws_fml in zip(wb_val.worksheets, wb_fml.worksheets):
                sheet_max_row = int(ws_val.max_row or 0)
                sample_rows = []
                if sheet_max_row > 0:
                    for row_v, row_f in zip(
                        ws_val.iter_rows(min_row=1, max_row=min(sheet_max_row, 8), values_only=True),
                        ws_fml.iter_rows(min_row=1, max_row=min(sheet_max_row, 8), values_only=True),
                    ):
                        values = [
                            _norm_cell(v if v is not None else f)
                            for v, f in zip(row_v, row_f)
                        ]
                        while values and values[-1] == "":
                            values.pop()
                        if values:
                            sample_rows.append(values)
                sheets.append(
                    {
                        "sheet": ws_val.title,
                        "row_count": sheet_max_row,
                        "column_count": int(ws_val.max_column or 0),
                        "sample_rows": sample_rows,
                    }
                )
        finally:
            wb_val.close()
            wb_fml.close()
        return {
            "file_type": file_type,
            "sheet_count": len(sheets),
            "sheet_names": [s["sheet"] for s in sheets],
            "headings": [s["sheet"] for s in sheets],
            "sheets": sheets,
            "available_chunk_strategy": "SHEET_PER_TABLE",
            "allowed_chunk_strategies": list(ALLOWED_STRATEGIES_BY_FILE_TYPE["xlsx"]),
        }
    raise ValueError(f"Unsupported file type: {file_type}")


def extract_document(
    path: Path,
    file_type: str,
    chunking_plan: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    if file_type == "pdf":
        return extract_pdf(path, chunking_plan=chunking_plan)
    if file_type == "docx":
        return extract_docx(path, chunking_plan=chunking_plan)
    if file_type == "xlsx":
        return extract_xlsx(path, chunking_plan=chunking_plan)
    raise ValueError(f"Unsupported file type: {file_type}")
