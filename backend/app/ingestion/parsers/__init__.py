"""纯离线文档拆箱、PDF 解析与确定性硬伤体检子包（Ingestion Parsers，零 DB、零 LLM）。

职责边界：
- `extractors.py`：PDF / DOCX / XLSX 统一抽块入口（消费 AI ① `chunking_plan` 蓝图）与全文摘要 `survey_document`；
- `pdf_catalog.py`：外管局已知规范高精插件（V1.3 / 外统 2025 目录与附录码表扫描）；
- `pdf_generic.py`：通用监管 PDF 二级章节切块（`HEADING_SECTIONS`）与三级分页兜底（`PAGE_WINDOWS`）；
- `pdf_parser.py`：PDF `pdftotext` 提取、表格折行重排与字段精拆；
- `quality.py`：原始块零 AI 确定性硬伤体检。
"""

from .extractors import (
    ALLOWED_STRATEGIES_BY_FILE_TYPE,
    convert_legacy_office_bytes,
    extract_document,
    extract_docx,
    extract_pdf,
    extract_xlsx,
    infer_document_role,
    survey_document,
    unpack_zip_archive,
)
from .pdf_catalog import extract_code_pairs_from_section, parse_document_catalog, parse_document_code_sets
from .pdf_generic import (
    default_pdf_strategy,
    detect_pdf_profiles,
    extract_pdf_generic_sections,
    extract_pdf_headings,
    extract_pdf_page_windows,
)
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
    "ALLOWED_STRATEGIES_BY_FILE_TYPE",
    "SpecParseError",
    "convert_legacy_office_bytes",
    "default_pdf_strategy",
    "detect_pdf_profiles",
    "extract_code_pairs_from_section",
    "extract_code_reference_from_text",
    "extract_document",
    "extract_docx",
    "extract_inline_enum_values",
    "extract_pdf",
    "extract_pdf_generic_sections",
    "extract_pdf_headings",
    "extract_pdf_page_windows",
    "extract_xlsx",
    "infer_document_role",
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
    "unpack_zip_archive",
]
