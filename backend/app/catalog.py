import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List

from .domain import SpecDocument
from .spec_parser import SpecParseError, _between, _extract_pdf_text, _parse_fields

MARKERS = {"数据术语解释", "数据格式", "数据字典"}


def _table_code(section: str) -> str:
    return "SAFE_" + section.replace(".", "_")


def _heading_before(text: str, section: str, position: int):
    matches = list(re.finditer(rf"(?m)^\s*{re.escape(section)}\s+([^\n]+)$", text[:position]))
    if not matches:
        raise SpecParseError(f"Cannot find table heading for {section}")
    match = matches[-1]
    return match.start(), f"{section} {match.group(1).strip()}"


def parse_document_catalog(pdf_path: str) -> List[Dict[str, Any]]:
    path = Path(pdf_path).resolve()
    if not path.is_file():
        raise SpecParseError(f"Source PDF not found: {path}")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    full_text = _extract_pdf_text(path)
    chapter_start = full_text.rfind("第六章 数据接口格式")
    chapter_end = full_text.find("第七章", chapter_start)
    if chapter_start < 0:
        raise SpecParseError("Chapter 6 was not found")
    text = full_text[chapter_start : chapter_end if chapter_end >= 0 else len(full_text)]
    version_match = re.search(r"数据采集规范（V([0-9.]+)）", full_text[:5000])
    document_version = f"SAFE_{version_match.group(1)}" if version_match else "UNKNOWN"
    marker_pattern = re.compile(r"(?m)^\s*(6(?:\.\d+){1,4})\s+(数据术语解释|数据格式|数据字典)\s*$")
    markers = list(marker_pattern.finditer(text))
    term_markers = [match for match in markers if match.group(2) == "数据术语解释"]
    entries: List[Dict[str, Any]] = []
    for index, marker in enumerate(term_markers):
        numbered_section = marker.group(1)
        section = numbered_section.rsplit(".", 1)[0]
        title_start, table_name = _heading_before(text, section, marker.start())
        next_start = term_markers[index + 1].start() if index + 1 < len(term_markers) else len(text)
        next_title_start = next_start
        if index + 1 < len(term_markers):
            next_section = term_markers[index + 1].group(1).rsplit(".", 1)[0]
            next_title_start, _ = _heading_before(text, next_section, next_start)
        section_text = text[title_start:next_title_start]
        code = _table_code(section)
        page = max(1, full_text[: chapter_start + title_start].count("\f") + 1 - 12)
        base = {
            "table_code": code,
            "table_name": table_name,
            "section": section,
            "eligible": False,
            "reason": "",
            "spec": None,
        }
        try:
            format_marker = f"{section}.2 数据格式"
            dictionary_marker = f"{section}.3 数据字典"
            terms_text = _between(section_text, marker.group(0), format_marker)
            format_text = _between(section_text, format_marker, dictionary_marker)
            dictionary_text = section_text.split(dictionary_marker, 1)[1]
            fields = _parse_fields(
                format_text,
                dictionary_text,
                str(page),
                repair_layout=section == "6.1",
            )
            spec = SpecDocument(
                version=document_version,
                table_code=code,
                table_name=table_name,
                source_name=path.name,
                source_sha256=digest,
                section=section,
                fields=fields,
                terms_text=terms_text.strip(),
                dictionary_text=dictionary_text.strip(),
            )
            base.update(
                {
                    "eligible": True,
                    "reason": "术语、XML 格式、数据字典及字段类型均可结构化提取",
                    "field_count": len(fields),
                    "spec": spec.model_dump(),
                }
            )
        except (SpecParseError, ValueError) as exc:
            base["reason"] = f"暂不符合第一步结构化解析条件：{exc}"
        entries.append(base)
    if not entries:
        raise SpecParseError("No table candidates were discovered in chapter 6")
    return entries
