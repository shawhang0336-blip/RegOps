import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from docx import Document
from openpyxl import load_workbook

from .catalog import parse_document_catalog
from .domain import canonical_json


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
                "TABLE_CANDIDATE",
                raw,
                {"section": entry["section"], "table_code": entry["table_code"]},
                entry,
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
        blocks.append(
            _block(
                len(blocks),
                "DOCX_TABLE",
                raw,
                {"table_index": index, "row_count": len(rows)},
                {"headers": rows[0], "rows": rows, "table_index": index},
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
                {"headers": rows[0], "rows": rows, "sheet": sheet.title},
            )
        )
    workbook.close()
    return "UNKNOWN", blocks


def extract_document(path: Path, file_type: str) -> Tuple[str, List[Dict[str, Any]]]:
    if file_type == "pdf":
        return extract_pdf(path)
    if file_type == "docx":
        return extract_docx(path)
    if file_type == "xlsx":
        return extract_xlsx(path)
    raise ValueError(f"Unsupported file type: {file_type}")
