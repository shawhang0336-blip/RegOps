import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from .domain import FieldSpec, SpecDocument


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
    except subprocess.CalledProcessError as exc:
        raise SpecParseError(exc.stderr.decode("utf-8", errors="replace")) from exc
    return result.stdout.decode("utf-8")


def _between(text: str, start_marker: str, end_marker: str, start: int = 0) -> str:
    start_index = text.find(start_marker, start)
    if start_index < 0:
        raise SpecParseError(f"Missing section marker: {start_marker}")
    end_index = text.find(end_marker, start_index + len(start_marker))
    if end_index < 0:
        raise SpecParseError(f"Missing section marker: {end_marker}")
    return text[start_index:end_index]


def _parse_fields(
    format_text: str,
    dictionary_text: str,
    evidence_page: str,
    expected_fields: int = 0,
    repair_layout: bool = True,
) -> List[FieldSpec]:
    xml_names = re.findall(r"<([A-Z][A-Z0-9]*)>", format_text)
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
        index = compact.find(search_name, cursor)
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
        type_match = re.search(r"(字符型|数值型|日期型)[，,](\d+)(?:\.(\d+))?", entry)
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


def field_map(spec: SpecDocument) -> Dict[str, FieldSpec]:
    return {field.name: field for field in spec.fields}
