import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from docx import Document
from openpyxl import load_workbook

from app.ingestion.catalog import parse_document_catalog, parse_document_code_sets
from app.infra.domain import canonical_json

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


def extract_pdf(path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    entries = parse_document_catalog(str(path))
    blocks = []
    for index, entry in enumerate(entries):
        raw = ""
        if entry.get("spec"):
            raw = f"{entry['spec']['terms_text']}\n{entry['spec']['dictionary_text']}"
        else:
            raw = entry["reason"]
        blocks.append(
            _block(
                index,
                entry.get("candidate_role", "TABLE_CANDIDATE"),
                raw,
                {"section": entry["section"], "table_code": entry["table_code"]},
                entry,
            )
        )
    code_sets = parse_document_code_sets(str(path))
    for cs in code_sets:
        blocks.append(
            _block(
                len(blocks),
                "CODE_SET_CANDIDATE",
                cs["raw_content"],
                {"section": cs["section"], "code_set_code": cs["code"], "page": cs["page"]},
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


def extract_docx(path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    document = Document(str(path))
    blocks = []
    for index, table in enumerate(document.tables):
        rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
        if not rows or not any(any(cell for cell in row) for row in rows):
            continue
        raw = "\n".join(" | ".join(row) for row in rows)
        code_spec = _sniff_tabular_code_set(f"{path.stem}_表{index + 1}", rows[0], rows)
        payload: Dict[str, Any] = {"headers": rows[0], "rows": rows, "table_index": index}
        if code_spec:
            payload["candidate_role"] = "CODE_SET"
            payload["code_set_spec"] = code_spec
            payload["reason"] = f"DOCX 码值表候选（{len(code_spec['values'])} 项）"
            payload["field_count"] = len(code_spec["values"])
        blocks.append(
            _block(
                len(blocks),
                "DOCX_TABLE",
                raw,
                {"table_index": index, "row_count": len(rows)},
                payload,
            )
        )
    narrative = "\n".join(paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip())
    if narrative:
        blocks.append(
            _block(
                len(blocks),
                "NARRATIVE",
                narrative[:50000],
                {"paragraphs": len(document.paragraphs)},
                {"text": narrative[:50000]},
            )
        )
    return "UNKNOWN", blocks


def extract_xlsx(path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    workbook = load_workbook(path, read_only=False, data_only=False)
    blocks = []
    for sheet in workbook.worksheets:
        rows = []
        for row in sheet.iter_rows(min_row=1, max_row=min(sheet.max_row, 2000), values_only=True):
            values = ["" if value is None else str(value) for value in row]
            while values and values[-1] == "":
                values.pop()
            if values:
                rows.append(values)
        if not rows:
            continue
        raw = "\n".join(" | ".join(row) for row in rows)
        code_spec = _sniff_tabular_code_set(sheet.title, rows[0], rows)
        payload: Dict[str, Any] = {"headers": rows[0], "rows": rows, "sheet": sheet.title}
        if code_spec:
            payload["candidate_role"] = "CODE_SET"
            payload["code_set_spec"] = code_spec
            payload["reason"] = f"Excel 码值表候选（{len(code_spec['values'])} 项）"
            payload["field_count"] = len(code_spec["values"])
        blocks.append(
            _block(
                len(blocks),
                "XLSX_SHEET",
                raw,
                {
                    "sheet": sheet.title,
                    "row_count": len(rows),
                    "truncated": sheet.max_row > 2000,
                    "merged_ranges": [str(item) for item in sheet.merged_cells.ranges],
                },
                payload,
            )
        )
    workbook.close()
    return "UNKNOWN", blocks


def survey_document(path: Path, file_type: str) -> Dict[str, Any]:
    if file_type == "pdf":
        from app.ingestion.spec_parser import _extract_pdf_text

        text = _extract_pdf_text(path)
        headings = []
        for line in text.splitlines():
            title = re.sub(r"\s+", " ", line).strip()
            if not title or len(title) > 100:
                continue
            if re.match(r"^(?:第[一二三四五六七八九十百]+章|[一二三四五六七八九十]+、|\d+(?:\.\d+){0,4}\s+\S+)", title):
                headings.append(title)
        return {
            "file_type": file_type,
            "page_count": max(1, text.count("\f")),
            "headings": headings[:300],
            "heading_count": len(headings),
            "content_preview": text[:5000],
            "available_chunk_strategy": "DOCUMENT_CATALOG",
        }
    if file_type == "docx":
        document = Document(str(path))
        paragraphs = [p.text.strip() for p in document.paragraphs if p.text.strip()]
        tables = []
        for index, table in enumerate(document.tables[:80]):
            rows = [[cell.text.strip() for cell in row.cells] for row in table.rows[:4]]
            tables.append({"table_index": index, "row_count": len(table.rows), "sample_rows": rows})
        return {
            "file_type": file_type,
            "paragraph_count": len(paragraphs),
            "headings": paragraphs[:160],
            "table_count": len(document.tables),
            "tables": tables,
            "available_chunk_strategy": "WORD_TABLE_AND_PARAGRAPH",
        }
    if file_type == "xlsx":
        workbook = load_workbook(path, read_only=False, data_only=False)
        sheets = []
        for sheet in workbook.worksheets:
            sample_rows = []
            for row in sheet.iter_rows(min_row=1, max_row=min(sheet.max_row, 6), values_only=True):
                values = ["" if value is None else str(value) for value in row]
                while values and values[-1] == "":
                    values.pop()
                if values:
                    sample_rows.append(values)
            sheets.append({"sheet": sheet.title, "row_count": sheet.max_row, "column_count": sheet.max_column, "sample_rows": sample_rows})
        workbook.close()
        return {
            "file_type": file_type,
            "sheet_count": len(sheets),
            "sheets": sheets,
            "available_chunk_strategy": "SHEET_PER_TABLE",
        }
    raise ValueError(f"Unsupported file type: {file_type}")


def extract_document(path: Path, file_type: str) -> Tuple[str, List[Dict[str, Any]]]:
    if file_type == "pdf":
        return extract_pdf(path)
    if file_type == "docx":
        return extract_docx(path)
    if file_type == "xlsx":
        return extract_xlsx(path)
    raise ValueError(f"Unsupported file type: {file_type}")
