"""纯离线文档拆箱、PDF 解析与确定性硬伤体检子包（Ingestion Parsers，零 DB、零 LLM）。

职责边界：
- `extractors.py`：PDF / DOCX / XLSX 统一抽块入口与全文摘要 `survey_document`；
- `pdf_catalog.py`：PDF 章节目录与附录公共码表扫描；
- `pdf_parser.py`：PDF `pdftotext` 提取、表格折行重排与字段精拆；
- `quality.py`：原始块零 AI 确定性硬伤体检。
"""

from .extractors import extract_document, extract_docx, extract_pdf, extract_xlsx, survey_document
from .pdf_catalog import extract_code_pairs_from_section, parse_document_catalog, parse_document_code_sets
from .pdf_parser import (
    SpecParseError,
    extract_code_reference_from_text,
    extract_inline_enum_values,
    lookup_term_definition,
    parse_safe_13_organization_spec,
    parse_safe_chapter6_slice,
    reflow_dictionary_rows,
    reflow_spec_terms,
)
from .quality import inspect_block, inspect_spec, summarize

__all__ = [
    "SpecParseError",
    "extract_code_pairs_from_section",
    "extract_code_reference_from_text",
    "extract_document",
    "extract_docx",
    "extract_inline_enum_values",
    "extract_pdf",
    "extract_xlsx",
    "inspect_block",
    "inspect_spec",
    "lookup_term_definition",
    "parse_document_catalog",
    "parse_document_code_sets",
    "parse_safe_13_organization_spec",
    "parse_safe_chapter6_slice",
    "reflow_dictionary_rows",
    "reflow_spec_terms",
    "summarize",
    "survey_document",
]
