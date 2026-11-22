import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.infra.contracts import FieldSpec, SpecDocument


class SpecParseError(ValueError):
    pass


@dataclass(frozen=True)
class SectionLocation:
    table_code: str
    table_name: str
    start_marker: str
    end_marker: str
    terms_marker: str
    format_marker: str
    dictionary_marker: str
    section: str
    pages: str
    expected_fields: int


SECTIONS = (
    SectionLocation(
        "SAFE_6_1_ORGANIZATION",
        "6.1 组织机构基本情况表",
        "6.1 组织机构基本情况表",
        "\n6.2 涉外收支",
        "6.1.1 数据术语解释",
        "6.1.2 数据格式",
        "6.1.3 数据字典",
        "6.1",
        "87-92",
        33,
    ),
    SectionLocation(
        "SAFE_6_2_1_INCOME_BASE",
        "6.2.1 涉外收入-基础信息",
        "6.2.1 涉外收入-基础信息",
        "\n6.2.2 涉外收入-申报信息",
        "6.2.1.1 数据术语解释",
        "6.2.1.2 数据格式",
        "6.2.1.3 数据字典",
        "6.2.1",
        "92-96",
        23,
    ),
    SectionLocation(
        "SAFE_6_2_2_INCOME_DECL",
        "6.2.2 涉外收入-申报信息",
        "6.2.2 涉外收入-申报信息",
        "\n6.2.3 ",
        "6.2.2.1 数据术语解释",
        "6.2.2.2 数据格式",
        "6.2.2.3 数据字典",
        "6.2.2",
        "96-99",
        17,
    ),
)

LAYOUT_REPAIRS = {
    "CUSTNAME": ("机构名称", "必填项，当常驻国为中国时必须输中文。"),
    "SHORTNAME": ("组织机构简称", "非必填项，机构使用的中文简称。"),
    "CUSTOMERENGNAME": (
        "组织机构英文名称",
        "常驻国家（地区）为非中国时必填；常驻国家（地区）为中国时选填。英文名称可填特殊机构代码赋码通知书上载明名称，不应包含全角字符。",
    ),
    "CREDITCODE": ("统一社会信用代码", "非必填项，18位统一社会信用代码。"),
}

NON_XML_FIELD_CODE_MAP = {
    "业务编号": "BIZCODE",
    "开户银行代码": "OPENBANKCODE",
    "开户境内代理机构代码": "AGENTORGCODE",
    "开户境内代理机构名称": "AGENTORGNAME",
    "境内专用外汇账户账号": "SPECACCTNO",
    "关户日期": "CLOSEDATE",
    "关户原因": "CLOSEREASON",
    "关户后资金处置类型": "DISPOSALTYPE",
    "结汇所得人民币资金划往的账户开户行代码": "SETTLEBANKCODE",
    "结汇所得人民币资金划往的账户户名": "SETTLEACCTNAME",
    "结汇所得人民币资金划往的账户账号": "SETTLEACCTNO",
    "划往境内外汇账户开户行代码": "TRANSBANKCODE",
    "划往境内外汇账户户名": "TRANSACCTNAME",
    "划往境内外汇账户账号": "TRANSACCTNO",
    "汇出境外收款账户开户行代码": "REMITBANKCODE",
    "汇出境外收款账户户名": "REMITACCTNAME",
    "汇出境外收款账户账号": "REMITACCTNO",
    "报告期": "BUOCMONTH",
    "币种": "CURRENCY",
    "月初余额": "INITBALANCE",
    "汇入金额": "INWARDAMT",
    "购汇汇入金额": "PURCHASEINAMT",
    "个人外汇储蓄账户划入金额": "SAVINGINAMT",
    "境外投资本金及收益汇回金额": "INVESTRETURNAMT",
    "汇出金额": "OUTWARDAMT",
    "汇往境外金额": "REMITOUTAMT",
    "结汇金额": "SETTLEAMT",
    "划入个人外汇储蓄账户金额": "SAVINGOUTAMT",
    "月末账户余额": "ENDBALANCE",
    "其他收支净额": "OTHERNETAMT",
    "备注": "REMARK",
}


# ---------------------------------------------------------------------------
# PDF 表格折行重排
#
# pdftotext -layout 保留列位置，但一个单元格的字数超过列宽时会折到下一行，且
# 另一列（常见是"校验""定义"）也可能同时折行。直接把文本按 \s+ 压平再按字段名
# 锚点切片，就会把"列 2 的折行残片"拼进"列 4 的校验原文"：
#   STRDECODE  人民币结构性存  字符型，28  必填项，唯一性编码。
#              款编号
#   → content=人民币结构性存（截断）、check_text=必填项，唯一性编码。款编号（串行）
# 这里按列位置把折行拼回各自所属的单元格，保证下游切片的输入是"一行一条记录"。
# ---------------------------------------------------------------------------

_TYPE_DECL = (
    "定长字符型|变长字符型|定长数字型|变长数值型|长整数型|定长型|变长型|"
    "字符型|数值型|日期型|整数型|时间型|日期时间型"
)
_TYPE_DECL_RE = re.compile(rf"(?:{_TYPE_DECL})\s*[，,]?\s*\d+(?:\.\d+)?|(?:{_TYPE_DECL})")
_ROW_HEADER_RE = re.compile(r"^\s*(?:字段|基础数据项|数据项|序号|栏目)\s")
_PAGE_NOISE_RE = re.compile(r"^\s*\d{1,4}\s*$|国家外汇管理局.*采集规范|^\s*[-—]?\s*\d+\s*[-—]?\s*$")
# 跨页重复的表头可能只剩后半截（"内容  类型（长度）  校验"），不能当成续行拼接
_HEADER_TOKEN_RE = re.compile(r"字段(?:名|英文名|代码)?|内容|类型（长度）|类型|数据长度|长度|校验|名称")


def _is_header_residue(stripped: str) -> bool:
    remainder = _HEADER_TOKEN_RE.sub("", stripped)
    return not re.sub(r"[\s（）()／/|、，,]", "", remainder)


def _is_noise_line(line: str) -> bool:
    stripped = line.replace("\f", "").strip()
    if not stripped:
        return True
    if _PAGE_NOISE_RE.search(stripped):
        return True
    if _ROW_HEADER_RE.match(stripped):
        return True
    if "类型" in stripped and _is_header_residue(stripped):
        return True
    # 跨页重复的表头：字段 内容 类型（长度） 校验
    if re.match(r"^字段\s", stripped) and ("类型" in stripped or "内容" in stripped):
        return True
    return False


def _next_nonspace(text: str, start: int) -> Optional[int]:
    for index in range(start, len(text)):
        if not text[index].isspace():
            return index
    return None


def _second_token_start(line: str) -> Optional[int]:
    """返回同一行第二个"词"的起始列，用来定位第二列的列位。"""
    lead = len(line) - len(line.lstrip())
    cursor = lead
    while cursor < len(line) and not line[cursor].isspace():
        cursor += 1
    return _next_nonspace(line, cursor)


def reflow_dictionary_rows(text: str) -> str:
    """把 4 列数据字典（字段/内容/类型（长度）/校验）的折行拼回单行记录。

    输出每行形如 ``FIELD_EN 中文名 类型，长度 校验原文``，下游压平后按字段锚点
    切片即可得到干净的单元格，不再串列。
    """
    if not text:
        return text
    rows: List[Dict[str, Any]] = []
    for raw_line in text.replace("\f", "").split("\n"):
        if _is_noise_line(raw_line):
            continue
        decl = _TYPE_DECL_RE.search(raw_line)
        if decl:
            name_start = len(raw_line) - len(raw_line.lstrip())
            type_start = decl.start()
            check_start = _next_nonspace(raw_line, decl.end())
            rows.append(
                {
                    "name": raw_line[name_start:type_start].strip(),
                    "decl": decl.group(0).strip(),
                    "check": raw_line[check_start:].strip() if check_start is not None else "",
                    "name_start": name_start,
                    "type_start": type_start,
                    "check_start": check_start if check_start is not None else decl.end(),
                }
            )
            continue
        if not rows:
            continue
        current = rows[-1]
        name_part = raw_line[current["name_start"] : current["type_start"]].strip()
        check_part = raw_line[current["check_start"] :].strip()
        if name_part:
            current["name"] += re.sub(r"\s+", "", name_part)
        if check_part:
            current["check"] += check_part
    return "\n".join(f"{row['name']} {row['decl']} {row['check']}".strip() for row in rows)


def reflow_spec_terms(text: str) -> str:
    """把「基础数据项 / 定义」两列折行拼回 ``名称 \\t 定义`` 的单行形式。

    定义列宽度固定，名称折行时续行只有名称列有字。识别新术语的条件是：该行定义列
    有内容，且上一条定义已以句末标点收尾；否则视为上一条的名称续行。
    """
    if not text:
        return text
    lines = [line.replace("\f", "") for line in text.split("\n")]
    def_col: Optional[int] = None
    for line in lines:
        if _is_noise_line(line):
            continue
        second = _second_token_start(line)
        if second is not None:
            def_col = second
            break
    if def_col is None or def_col < 4:
        return text

    entries: List[Dict[str, str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        name_part = line[:def_col].strip()
        def_part = line[def_col:].strip() if len(line) > def_col else ""
        if "定义" in name_part and not def_part:
            continue
        if "定义" in def_part and not entries:
            continue
        if not entries:
            if name_part and def_part:
                entries.append({"name": name_part, "definition": def_part})
            continue
        current = entries[-1]
        if not def_part:
            if name_part:
                current["name"] += re.sub(r"\s+", "", name_part)
            continue
        if not name_part:
            current["definition"] += def_part
            continue
        finished = current["definition"].rstrip().endswith(("。", "；", "！", "？"))
        if finished or len(name_part) > 8:
            entries.append({"name": name_part, "definition": def_part})
        else:
            current["name"] += re.sub(r"\s+", "", name_part)
            current["definition"] += def_part
    if not entries:
        return text
    return "\n".join(f"{entry['name']}\t{entry['definition']}" for entry in entries)


def lookup_term_definition(terms_text: str, field_cn: str) -> str:
    """在（已重排的）术语文本里取该字段的定义，取不到返回空串。"""
    name = (field_cn or "").strip()
    if not name or not terms_text:
        return ""
    for line in terms_text.split("\n"):
        if "\t" not in line:
            continue
        term, definition = line.split("\t", 1)
        if term.strip() == name:
            return definition.strip()
    index = terms_text.find(name)
    if index < 0:
        return ""
    tail = terms_text[index + len(name) :].split("\n", 1)[0]
    # 非制表符展平文本常把相邻字段定义拼在同一行（"字段A 定义A。 字段B 见XX代码表。"），
    # 仅保留到首个句号，防止把后面字段的码表引用串味到当前字段。
    if "。" in tail:
        tail = tail.split("。", 1)[0] + "。"
    return tail.strip(" \t：:")


def extract_code_reference_from_text(text: str) -> str:
    if not text:
        return ""
    patterns = [
        r"(?:参?见|参照)[^，。；,\n]*?(附录[（(][一二三四五六七八九十]+[)）][^\s，。；,（）()]{2,20})",
        r"(?:参?见|参照)\s*([A-Za-z0-9\u4e00-\u9fa5/（）()—\-]{2,30}?(?:代码表|分类标准|名录|所属行业表|部门表|关系表))",
    ]
    for pat in patterns:
        match = re.search(pat, text)
        if match:
            ref = match.group(1).strip()
            ref = re.sub(r"^(?:数据采集规范最新版本中的相关代码表或本制度|本制度)", "", ref)
            return ref.strip()
    return ""


def extract_inline_enum_values(text: str) -> List[Dict[str, str]]:
    if not text:
        return []
    values: List[Dict[str, str]] = []
    seen = set()

    # Pattern 1: Circled numbers ①金融机构；②综合调查主体 ... or ○11 不动产
    circled_map = {
        "①": "1", "②": "2", "③": "3", "④": "4", "⑤": "5",
        "⑥": "6", "⑦": "7", "⑧": "8", "⑨": "9", "⑩": "10",
    }
    for m in re.finditer(r"([①②③④⑤⑥⑦⑧⑨⑩])\s*([^；;。，,\n（(]+)", text):
        code = circled_map[m.group(1)]
        label = m.group(2).strip()
        if label and code not in seen:
            seen.add(code)
            values.append({"value": code, "label_cn": label})

    # Pattern 2: Code-Label pairs like "A-新建", "1-主体标识码", "110.内资-国有企业", "01 居民身份证"
    for m in re.finditer(r"(?<![A-Za-z0-9])([A-Z]|\d{1,4})\s*[-—.．]\s*([\u4e00-\u9fa5][^\n；;。]{1,36})", text):
        code = m.group(1).strip()
        label = m.group(2).strip().rstrip("，,。；; ")
        if any(bad in label for bad in ("位", "当", "如果", "必填", "选填", "格式", "大于", "小于")):
            continue
        if label and code not in seen:
            seen.add(code)
            values.append({"value": code, "label_cn": label})

    # Pattern 3: 2-digit space-separated code-label lines (e.g., "01 居民身份证")
    if len(values) < 2:
        for m in re.finditer(r"(?m)^\s*(\d{2})\s+([\u4e00-\u9fa5]{2,20})\s*$", text):
            code = m.group(1).strip()
            label = m.group(2).strip()
            if code not in seen:
                seen.add(code)
                values.append({"value": code, "label_cn": label})

    return values if len(values) >= 2 else []


def _extract_pdf_text(pdf_path: Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            check=True,
            capture_output=True,
            timeout=90,
        )
    except FileNotFoundError as exc:
        raise SpecParseError("pdftotext is required to import the source PDF") from exc
    except subprocess.TimeoutExpired as exc:
        raise SpecParseError("pdftotext timed out while extracting the source PDF") from exc
    except subprocess.CalledProcessError as exc:
        raise SpecParseError(exc.stderr.decode("utf-8", errors="replace")) from exc
    return result.stdout.decode("utf-8", errors="replace")


def _between(text: str, start_marker: str, end_marker: str, start: int = 0) -> str:
    start_index = text.find(start_marker, start)
    if start_index < 0:
        raise SpecParseError(f"Missing section marker: {start_marker}")
    end_index = text.find(end_marker, start_index + len(start_marker))
    if end_index < 0:
        raise SpecParseError(f"Missing section marker: {end_marker}")
    return text[start_index:end_index]


def _parse_fields_strict(
    format_text: str,
    dictionary_text: str,
    evidence_page: str,
    expected_fields: int = 0,
    repair_layout: bool = True,
) -> List[FieldSpec]:
    xml_names = re.findall(r"<([A-Za-z][A-Za-z0-9_]*)>", format_text)
    field_names = [name for name in xml_names if name not in {"REC", "BANKINFOS", "BANKINFO"}]
    if not field_names or len(set(field_names)) != len(field_names):
        raise SpecParseError("XML format does not contain a unique field whitelist")
    if expected_fields and len(field_names) != expected_fields:
        raise SpecParseError(f"Expected {expected_fields} unique fields, got {len(field_names)}")

    compact = re.sub(r"\s+", "", dictionary_text)
    compact = re.sub(
        r"\d{1,3}国家外汇管理局金融机构外汇业务数据采集规范（V1\.3）字段内容类型（长度）校验",
        "",
        compact,
    )
    compact = compact.replace("字段内容类型（长度）校验", "")
    positions: List[Tuple[str, int, int]] = []
    cursor = 0
    for name in field_names:
        search_name = "INCORPORATORIDCO" if name == "INCORPORATORIDCODE" else name
        index = compact.lower().find(search_name.lower(), cursor)
        if index < 0:
            raise SpecParseError(f"Field {name} is absent from the data dictionary")
        positions.append((name, index, index + len(search_name)))
        cursor = index + len(search_name)

    fields: List[FieldSpec] = []
    for position, (name, _, entry_start) in enumerate(positions):
        entry_end = positions[position + 1][1] if position + 1 < len(positions) else len(compact)
        entry = compact[entry_start:entry_end]
        if name == "INCORPORATORIDCODE":
            entry = entry.replace("DE份证件号码", "")
        type_match = re.search(r"(定长字符型|变长字符型|定长|变长|字符型|数值型|日期型)[，,](\d+)(?:\.(\d+))?", entry)
        if name == "REGDATE" and not type_match:
            fields.append(
                FieldSpec(
                    name=name,
                    content="注册/开业成立日期",
                    data_type="VARCHAR",
                    length=8,
                    check_text="非必填项；格式为 YYYYMMDD；指工商营业执照上的成立日期",
                    evidence_page="90",
                )
            )
            continue
        if not type_match:
            loose_match = re.search(r"(?:变长数值型|数值型)[，,]", entry)
            length_match = loose_match and re.search(r"(\d+)\.(\d+)", entry[loose_match.end() :])
            if loose_match and length_match:
                check_tail = entry[loose_match.end() :]
                fields.append(
                    FieldSpec(
                        name=name,
                        content=entry[: loose_match.start()].strip("，,。 "),
                        data_type="NUMBER",
                        length=int(length_match.group(1)),
                        decimal_places=int(length_match.group(2)),
                        check_text=check_tail[: length_match.start()] + check_tail[length_match.end() :],
                        evidence_page=evidence_page,
                    )
                )
                continue
            raise SpecParseError(f"Cannot determine the type of field {name}")
        data_type = "NUMBER" if type_match.group(1) == "数值型" else "VARCHAR"
        content = entry[: type_match.start()].strip("，,。 ")
        if name == "INCORPORATORIDCODE":
            content = "法定代表人身份证件号码"
        fields.append(
            FieldSpec(
                name=name,
                content=content,
                data_type=data_type,
                length=int(type_match.group(2)),
                decimal_places=int(type_match.group(3)) if type_match.group(3) else None,
                check_text=entry[type_match.end() :].strip("，,。 "),
                evidence_page=evidence_page,
            )
        )
    if repair_layout:
        for field in fields:
            if field.name in LAYOUT_REPAIRS:
                field.content, field.check_text = LAYOUT_REPAIRS[field.name]
    return fields


def _parse_type_from_entry(entry: str, xml_label: str) -> Tuple[str, str, int, Optional[int], str, List[str]]:
    """Extract field properties and non-fatal parsing warnings from a dictionary entry."""
    warnings: List[str] = []
    # Remove any residual wrapped suffix of field_en at start of entry (e.g. "T签约金额..." or "PE操作类型...")
    cleaned = re.sub(r"^[A-Za-z0-9_]{1,8}(?=[\u4e00-\u9fa5])", "", entry)

    # 1. Standard type with length: 字符型，20 / 数值型，22.2 / 定长数字型，12 / 定长型，6 / 日期型，8 / 字符128 / 数值22.2
    type_match = re.search(
        r"(定长字符型|变长字符型|定长数字型|定长型|定长|变长数值型|变长|字符型|数值型|日期型|整数型|时间型|日期时间型|字符|数值)"
        r"(?:[，,:\s]*(\d+)(?:[.,](\d+))?)?",
        cleaned,
    )
    if type_match:
        kind = type_match.group(1)
        raw_len = type_match.group(2)
        raw_dec = type_match.group(3)
        content = cleaned[: type_match.start()].strip("，,。 ") or xml_label
        # Clean stray wrapped English suffix inside content if xml_label is known
        if xml_label and len(content) > len(xml_label) + 6:
            content = xml_label
        check_text = cleaned[type_match.end() :].strip("，,。 ")
        if kind in {"数值型", "变长数值型", "数值", "整数型"}:
            data_type = "NUMBER"
            if raw_len:
                length = int(raw_len)
                decimal_places = int(raw_dec) if raw_dec else None
            elif kind == "整数型":
                length = 10
                decimal_places = 0
                warnings.append("整数型未提供长度，暂按 NUMBER(10,0) 处理")
            else:
                len_m = re.search(r"([1-9]\d*)\.(\d+)", check_text)
                length = int(len_m.group(1)) if len_m else 22
                decimal_places = int(len_m.group(2)) if len_m else 2
                warnings.append("数值型未提供明确精度，需人工确认长度与小数位")
            if decimal_places is not None and decimal_places > length:
                length, decimal_places = 22, min(decimal_places, 8)
        elif kind in {"日期型"}:
            data_type = "VARCHAR"
            length = int(raw_len) if raw_len else 8
            decimal_places = None
            if not raw_len:
                warnings.append("日期型未提供长度，暂按 VARCHAR(8) 处理")
        elif kind in {"时间型", "日期时间型"}:
            data_type = "VARCHAR"
            length = int(raw_len) if raw_len else 19
            decimal_places = None
            if not raw_len:
                warnings.append("时间类型未提供长度，暂按 VARCHAR(19) 处理")
        else:
            data_type = "VARCHAR"
            length = int(raw_len) if raw_len else 128
            decimal_places = None
            if not raw_len:
                warnings.append("字符类型未提供长度，暂按 VARCHAR(128) 处理")
        return content or xml_label or "字段", data_type, max(1, length), decimal_places, check_text, warnings

    return xml_label or "字段", "VARCHAR", 128, None, cleaned.strip("，,。 "), ["未识别字段类型，暂按 VARCHAR(128) 处理"]


def _parse_non_xml_dictionary(dictionary_text: str, evidence_page: str, parse_warnings: Optional[List[str]] = None) -> List[FieldSpec]:
    cleaned = re.sub(
        r"国家外汇管理局金融机构外汇业务数据采集规范（V1\.[0-9]）",
        "",
        dictionary_text,
    )
    pattern = re.compile(
        r"(?m)^\s*([\u4e00-\u9fa5][^\n]{0,24}?)\s+"
        r"(定长字符型|变长字符型|定长数字型|定长型|字符型|数值型|日期型|整数型|时间型)\s*[，,]\s*(\d+)(?:\.(\d+))?\s+([^\n]+)"
    )
    matches = list(pattern.finditer(cleaned))
    if not matches:
        raise SpecParseError("No dictionary rows found in non-XML section")
    fields: List[FieldSpec] = []
    seen_names = set()
    for idx, m in enumerate(matches):
        cn_name = re.sub(r"\s+", "", m.group(1)).strip()
        kind = m.group(2)
        length = int(m.group(3))
        dec = int(m.group(4)) if m.group(4) else None
        tail_start = m.start(5)
        tail_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(cleaned)
        check_raw = re.sub(r"\s+", "", cleaned[tail_start:tail_end])
        check_raw = re.sub(r"\d{1,3}内容类型（长度）校验", "", check_raw)
        en_name = NON_XML_FIELD_CODE_MAP.get(cn_name, "")
        if not en_name and len(cn_name) >= 6:
            prefix_matches = [(full_name, code) for full_name, code in NON_XML_FIELD_CODE_MAP.items() if full_name.startswith(cn_name)]
            unused_matches = [item for item in prefix_matches if item[1] not in seen_names]
            if unused_matches:
                cn_name, en_name = unused_matches[0]
                if len(prefix_matches) > 1 and parse_warnings is not None:
                    parse_warnings.append(f"{cn_name}：多个字段共享截断前缀，已按文档顺序暂映射为 {en_name}")
        if not en_name:
            en_name = f"FIELD_{idx + 1:02d}"
        if en_name in seen_names:
            en_name = f"{en_name}_{idx + 1}"
        seen_names.add(en_name)
        fields.append(
            FieldSpec(
                name=en_name,
                content=cn_name,
                data_type="NUMBER" if "数值" in kind or "整数" in kind else "VARCHAR",
                length=length,
                decimal_places=dec,
                check_text=check_raw.strip("，,。 "),
                evidence_page=evidence_page,
            )
        )
    return fields


def _find_field_anchor(compact: str, compact_lower: str, candidate: str, start: int) -> int:
    """在去空白文本里定位字段定义的起点，跳过"公式引用"造成的假命中。

    数据字典的校验列常出现 `A0116=A0117+A0118+A0119+A0120+A0121` 这种公式，
    按字符串首次出现定位会把 A0117 的起点落到公式里，导致该字段的类型/长度解析失败、
    校验原文退化成 "+"。真正的字段起点两侧不会紧邻运算符。
    """
    # 公式里字段名两侧常是运算符或连接词（A0130x和A0131x、A0117+A0118）。
    # 注意这里只能用"符号 + 单字连接词"做集合判断：早先写成字符串包含判断，
    # 里面混进了"加减乘除"四个字，导致前面一个字是"除"的正常行（…D-删除ACTIONDESC）
    # 被当成公式命中而跳过，字段锚点丢失、整行被上一条吞掉。
    formula_neighbors = {"+", "-", "=", "—", "<", ">", "≤", "≥", "（", "(", "【", "[", "、", "，", ",", "和", "及", "与"}
    pos = max(0, start)
    while True:
        index = compact_lower.find(candidate, pos)
        if index < 0:
            return -1
        after = compact[index + len(candidate) : index + len(candidate) + 1]
        before = compact[index - 1 : index] if index > 0 else ""
        if after and after in formula_neighbors:
            pos = index + 1
            continue
        if before and before in formula_neighbors:
            pos = index + 1
            continue
        return index


def _parse_fields_flexible(
    format_text: str,
    dictionary_text: str,
    evidence_page: str,
    parse_warnings: Optional[List[str]] = None,
) -> List[FieldSpec]:
    # Extract leaf XML tags: <TAG>中文描述</TAG> (ignoring container tags like <PROJECTS>, <INVESTS>, <BeneficiaryS>)
    leaf_matches = re.findall(
        r"<\s*([A-Za-z][A-Za-z0-9_]*)\s*>\s*([^<\n]+?)\s*</\s*[A-Za-z][A-Za-z0-9_]*\s*>",
        format_text,
    )
    if not leaf_matches:
        fields = _parse_non_xml_dictionary(dictionary_text, evidence_page, parse_warnings)
        if parse_warnings is not None:
            parse_warnings.extend(
                f"{field.content}：未提供字段英文名，暂使用 {field.name}"
                for field in fields
                if re.fullmatch(r"FIELD_\d+(?:_\d+)?", field.name)
            )
        return fields

    xml_items: List[Tuple[str, str]] = []
    seen = set()
    for raw_name, raw_label in leaf_matches:
        name = raw_name.strip()
        if name in {"REC", "BANKINFOS", "BANKINFO"} or name.upper() in seen:
            continue
        seen.add(name.upper())
        xml_items.append((name, re.sub(r"\s+", "", raw_label.strip())))

    compact = re.sub(r"\s+", "", dictionary_text)
    compact = re.sub(
        r"\d{1,3}国家外汇管理局金融机构外汇业务数据采集规范（V1\.[0-9]）(?:字段(?:名)?(?:内容|基础数据项)(?:数据)?类型(?:（长度）|数据长度)校验)?",
        "",
        compact,
    )
    compact = re.sub(r"字段(?:名)?(?:内容|基础数据项)(?:数据)?类型(?:（长度）|数据长度)校验", "", compact)
    compact_lower = compact.lower()

    matched_positions: List[Tuple[str, str, int, int]] = []
    cursor = 0
    for name, xml_label in xml_items:
        target_lower = name.lower()
        found_idx = -1
        found_len = len(name)

        # 1. Search forward from cursor for full name or prefix (len >= 6 for wrapped column-1 names)
        for prefix_len in [len(name), min(len(name), 14), min(len(name), 12), min(len(name), 10), min(len(name), 8)]:
            if prefix_len < min(6, len(name)):
                continue
            cand = target_lower[:prefix_len]
            idx = _find_field_anchor(compact, compact_lower, cand, cursor)
            if idx >= 0:
                found_idx = idx
                found_len = prefix_len
                break

        # 2. Search globally if out of order in XML vs dictionary (e.g., A0111 in 6.9.4)
        if found_idx < 0:
            for prefix_len in [len(name), min(len(name), 12), min(len(name), 8)]:
                if prefix_len < min(5, len(name)):
                    continue
                cand = target_lower[:prefix_len]
                idx = _find_field_anchor(compact, compact_lower, cand, 0)
                if idx >= 0:
                    found_idx = idx
                    found_len = prefix_len
                    break

        if found_idx >= 0:
            matched_positions.append((name, xml_label, found_idx, found_idx + found_len))
            if found_idx >= cursor:
                cursor = found_idx + found_len
        else:
            # Field present in XML but omitted in dictionary text (e.g. REMARK in 6.9.29 E03)
            matched_positions.append((name, xml_label, -1, -1))

    # Sort found positions by index in dictionary_text to slice entries cleanly
    valid_sorted = sorted([item for item in matched_positions if item[2] >= 0], key=lambda x: x[2])
    entry_by_name: Dict[str, str] = {}
    for idx, (name, xml_label, start_pos, end_pos) in enumerate(valid_sorted):
        next_start = valid_sorted[idx + 1][2] if idx + 1 < len(valid_sorted) else len(compact)
        entry_by_name[name] = compact[end_pos:next_start]

    fields: List[FieldSpec] = []
    for name, xml_label, start_pos, _ in matched_positions:
        if start_pos < 0:
            if parse_warnings is not None:
                parse_warnings.append(f"{name}：XML 中存在，但未在数据字典中定位到字段定义")
            fields.append(
                FieldSpec(
                    name=name,
                    content=xml_label or name,
                    data_type="VARCHAR",
                    length=256 if "REMARK" in name.upper() else 128,
                    decimal_places=None,
                    check_text="非必填项" if "REMARK" in name.upper() else "见数据格式定义",
                    evidence_page=evidence_page,
                )
            )
            continue
        raw_entry = entry_by_name.get(name, "")
        content, data_type, length, dec, check_text, field_warnings = _parse_type_from_entry(raw_entry, xml_label)
        if parse_warnings is not None:
            parse_warnings.extend(f"{name}：{warning}" for warning in field_warnings)
        fields.append(
            FieldSpec(
                name=name,
                content=content,
                data_type=data_type,
                length=length,
                decimal_places=dec,
                check_text=check_text,
                evidence_page=evidence_page,
            )
        )
    return fields


def _parse_fields(
    format_text: str,
    dictionary_text: str,
    evidence_page: str,
    expected_fields: int = 0,
    repair_layout: bool = True,
    parse_warnings: Optional[List[str]] = None,
) -> List[FieldSpec]:
    # 折行重排必须在切分之前：按字段名锚点切片时，折行残片会串到相邻单元格。
    # 少数表（6.1）原文列本身错位，重排反而更乱，因此保留原文兜底。
    reflowed = reflow_dictionary_rows(dictionary_text)
    if reflowed != dictionary_text:
        try:
            return _parse_fields_strict(format_text, reflowed, evidence_page, expected_fields, repair_layout)
        except SpecParseError:
            pass
    try:
        return _parse_fields_strict(format_text, dictionary_text, evidence_page, expected_fields, repair_layout)
    except SpecParseError:
        if expected_fields or repair_layout:
            raise
        return _parse_fields_flexible(format_text, reflowed, evidence_page, parse_warnings)


def _parse_location(
    text: str,
    location: SectionLocation,
    source_name: str,
    source_sha256: str,
) -> SpecDocument:
    chapter_start = text.rfind("第六章 数据接口格式")
    if chapter_start < 0:
        raise SpecParseError("Chapter 6 was not found")
    section = _between(text, location.start_marker, location.end_marker, chapter_start)
    terms_text = _between(section, location.terms_marker, location.format_marker)
    format_text = _between(section, location.format_marker, location.dictionary_marker)
    dictionary_text = section.split(location.dictionary_marker, 1)[1]
    fields = _parse_fields(format_text, dictionary_text, location.pages, location.expected_fields)
    return SpecDocument(
        version="SAFE_1.3",
        table_code=location.table_code,
        table_name=location.table_name,
        source_name=source_name,
        source_sha256=source_sha256,
        section=location.section,
        fields=fields,
        terms_text=terms_text.strip(),
        dictionary_text=dictionary_text.strip(),
    )


def parse_safe_chapter6_slice(pdf_path: str) -> List[SpecDocument]:
    path = Path(pdf_path).resolve()
    if not path.is_file():
        raise SpecParseError(f"Source PDF not found: {path}")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    text = _extract_pdf_text(path)
    return [_parse_location(text, location, path.name, digest) for location in SECTIONS]


def parse_safe_13_organization_spec(pdf_path: str) -> SpecDocument:
    path = Path(pdf_path).resolve()
    if not path.is_file():
        raise SpecParseError(f"Source PDF not found: {path}")
    raw = path.read_bytes()
    text = _extract_pdf_text(path)
    return _parse_location(text, SECTIONS[0], path.name, hashlib.sha256(raw).hexdigest())
