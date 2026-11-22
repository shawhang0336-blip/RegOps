import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from openpyxl import load_workbook

from app.infra.contracts import canonical_json
from app.catalog.ingestion.parsers.pdf_catalog import parse_document_catalog, parse_document_code_sets
from app.catalog.ingestion.parsers.pdf_generic import (
    default_pdf_strategy,
    detect_pdf_profiles,
    extract_pdf_generic_sections,
    extract_pdf_headings,
    extract_pdf_page_windows,
)
from app.catalog.ingestion.parsers.pdf_parser import SpecParseError, _extract_pdf_text

# 单 Sheet 行上限：20,000 行可完整覆盖行政区划（3,200+ 行）等大型监管码表，同时防百万行恶意表格撑爆内存
MAX_XLSX_ROWS = 20000

CODE_HEADER_VAL_ALIASES = {
    "value", "code", "码值", "代码", "编码", "国家/地区代码", "币种代码",
    "行业分类代码", "外汇局系统行业标识码", "行政区划代码", "交易代码",
    "商品类别代码", "账户性质代码", "结汇用途代码", "表号",
}
CODE_HEADER_LABEL_ALIASES = {
    "label_cn", "name", "含义", "中文含义", "名称", "中文名称", "代码名称",
    "中文简称", "中文全称", "行业类别名称", "币种名称", "行政区划名称",
    "商品类别名称", "账户性质名称", "结汇用途", "表名", "业务类型",
}
META_HEADER_ALIASES = {
    "field_en", "field_cn", "字段", "字段名", "英文名", "字段中文名",
    "类型（长度）", "数据类型", "字段校验", "校验", "数据长度",
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


def _sniff_tabular_code_set(title: str, headers: List[str], rows: List[List[str]]) -> Optional[Dict[str, Any]]:
    norm_headers = [re.sub(r"\s+", "", str(h or "")).lower() for h in headers]
    if any(h in {m.lower() for m in META_HEADER_ALIASES} for h in norm_headers):
        return None
    val_idx = next((i for i, h in enumerate(norm_headers) if h in {a.lower() for a in CODE_HEADER_VAL_ALIASES} or "代码" in h or "码值" in h or "编码" in h), None)
    label_idx = next((i for i, h in enumerate(norm_headers) if i != val_idx and (h in {a.lower() for a in CODE_HEADER_LABEL_ALIASES} or "名称" in h or "含义" in h or "简称" in h)), None)
    if val_idx is None or label_idx is None:
        return None
    en_idx = next((i for i, h in enumerate(norm_headers) if i not in {val_idx, label_idx} and ("英文" in h or "label_en" in h)), None)
    def_idx = next((i for i, h in enumerate(norm_headers) if i not in {val_idx, label_idx, en_idx} and ("说明" in h or "解释" in h or "定义" in h or "备注" in h or "definition" in h)), None)

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
    """按文档真实阅读顺序依次产出 Paragraph 与 Table，使每个表格能精确绑定正上方最近的章节标题。"""
    parent_elm = document.element.body
    for child in parent_elm.iterchildren():
        if child.tag.endswith("}p"):
            yield Paragraph(child, document)
        elif child.tag.endswith("}tbl"):
            yield Table(child, document)


def _looks_like_heading(paragraph: Paragraph, text: str) -> bool:
    if not text or len(text) > 100:
        return False
    style_name = (getattr(paragraph.style, "name", "") or "").lower()
    if "heading" in style_name or "标题" in style_name:
        return True
    return bool(
        re.match(
            r"^(?:第[一二三四五六七八九十百]+[章部分篇节]|[一二三四五六七八九十]+、|（[一二三四五六七八九十]+）|附[录表]|表\s*[A-Za-z0-9\-_.]+|\d+(?:\.\d+){1,4}\s+\S+|\d+[.、]\s*\S+)",
            text,
        )
    )


def extract_docx(
    path: Path,
    chunking_plan: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    document = Document(str(path))
    role_map = _region_roles_map(chunking_plan)
    blocks: List[Dict[str, Any]] = []
    narrative_parts: List[str] = []
    current_heading = ""
    table_index = 0

    for item in _iter_docx_body_items(document):
        if isinstance(item, Paragraph):
            text = item.text.strip()
            if not text:
                continue
            if _looks_like_heading(item, text):
                current_heading = text
            if _lookup_region_role(role_map, current_heading) != "IGNORE":
                narrative_parts.append(text)
        elif isinstance(item, Table):
            idx = table_index
            table_index += 1
            rows = [[cell.text.strip() for cell in row.cells] for row in item.rows]
            if not rows or not any(any(cell for cell in row) for row in rows):
                continue
            region_role = _lookup_region_role(role_map, current_heading)
            if region_role == "IGNORE":
                continue
            title = current_heading or f"{path.stem}_表{idx + 1}"
            raw = "\n".join(" | ".join(row) for row in rows)
            code_spec = _sniff_tabular_code_set(title, rows[0], rows)
            payload: Dict[str, Any] = {
                "headers": rows[0],
                "rows": rows,
                "table_index": idx,
                "table_name": title,
            }
            if current_heading:
                payload["section"] = current_heading[:64]
            if code_spec or region_role == "CODE_SET":
                payload["candidate_role"] = "CODE_SET"
                if code_spec:
                    payload["code_set_spec"] = code_spec
                    payload["reason"] = f"DOCX 码值表候选（{len(code_spec['values'])} 项）"
                    payload["field_count"] = len(code_spec["values"])
            elif region_role in {"RULE_EVIDENCE", "NARRATIVE", "TABLE_METADATA"}:
                payload["candidate_role"] = region_role
            locator: Dict[str, Any] = {"table_index": idx, "row_count": len(rows)}
            if current_heading:
                locator["section"] = current_heading[:64]
                locator["title"] = current_heading[:120]
            blocks.append(_block(len(blocks), "DOCX_TABLE", raw, locator, payload))

    narrative = "\n".join(narrative_parts)
    if narrative:
        blocks.append(
            _block(
                len(blocks),
                "NARRATIVE",
                narrative[:50000],
                {"paragraphs": len(narrative_parts)},
                {"text": narrative[:50000]},
            )
        )
    return "UNKNOWN", blocks


def extract_xlsx(
    path: Path,
    chunking_plan: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    workbook = load_workbook(path, read_only=False, data_only=False)
    role_map = _region_roles_map(chunking_plan)
    blocks: List[Dict[str, Any]] = []
    try:
        for sheet in workbook.worksheets:
            region_role = _lookup_region_role(role_map, sheet.title)
            if region_role == "IGNORE":
                continue
            sheet_max_row = int(sheet.max_row or 0)
            if sheet_max_row <= 0:
                continue
            rows = []
            for row in sheet.iter_rows(min_row=1, max_row=min(sheet_max_row, MAX_XLSX_ROWS), values_only=True):
                values = ["" if value is None else str(value) for value in row]
                while values and values[-1] == "":
                    values.pop()
                if values:
                    rows.append(values)
            if not rows:
                continue
            raw = "\n".join(" | ".join(row) for row in rows)
            code_spec = _sniff_tabular_code_set(sheet.title, rows[0], rows)
            payload: Dict[str, Any] = {"headers": rows[0], "rows": rows, "sheet": sheet.title, "table_name": sheet.title}
            if code_spec or region_role == "CODE_SET":
                payload["candidate_role"] = "CODE_SET"
                if code_spec:
                    payload["code_set_spec"] = code_spec
                    payload["reason"] = f"Excel 码值表候选（{len(code_spec['values'])} 项）"
                    payload["field_count"] = len(code_spec["values"])
            elif region_role in {"RULE_EVIDENCE", "NARRATIVE", "TABLE_METADATA"}:
                payload["candidate_role"] = region_role
            blocks.append(
                _block(
                    len(blocks),
                    "XLSX_SHEET",
                    raw,
                    {
                        "sheet": sheet.title,
                        "row_count": len(rows),
                        "truncated": sheet_max_row > MAX_XLSX_ROWS,
                        "merged_ranges": [str(item) for item in sheet.merged_cells.ranges],
                    },
                    payload,
                )
            )
    finally:
        workbook.close()
    return "UNKNOWN", blocks


def survey_document(path: Path, file_type: str) -> Dict[str, Any]:
    """生成给 AI ① 整文档体检使用的结构化情报（带页码标题、结构指纹与中后段正文抽样）。"""
    if file_type == "pdf":
        text = _extract_pdf_text(path)
        headings_info = extract_pdf_headings(text)
        profiles = detect_pdf_profiles(text)
        recommended = default_pdf_strategy(text, headings_info)
        # 除封面/目录前 3000 字外，增补中段与后段抽样，避免 AI 只看到封面就盲判
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
                if _looks_like_heading(item, text):
                    current_heading = text
                    headings.append(text)
            elif isinstance(item, Table):
                if table_index < 80:
                    rows = [[cell.text.strip() for cell in row.cells] for row in item.rows[:4]]
                    tables.append(
                        {
                            "table_index": table_index,
                            "nearest_heading": current_heading,
                            "row_count": len(item.rows),
                            "sample_rows": rows,
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
        workbook = load_workbook(path, read_only=False, data_only=False)
        sheets = []
        try:
            for sheet in workbook.worksheets:
                sheet_max_row = int(sheet.max_row or 0)
                sample_rows = []
                if sheet_max_row > 0:
                    for row in sheet.iter_rows(min_row=1, max_row=min(sheet_max_row, 6), values_only=True):
                        values = ["" if value is None else str(value) for value in row]
                        while values and values[-1] == "":
                            values.pop()
                        if values:
                            sample_rows.append(values)
                sheets.append(
                    {
                        "sheet": sheet.title,
                        "row_count": sheet_max_row,
                        "column_count": int(sheet.max_column or 0),
                        "sample_rows": sample_rows,
                    }
                )
        finally:
            workbook.close()
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
