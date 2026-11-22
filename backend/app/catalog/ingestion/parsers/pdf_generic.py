"""通用监管 PDF 章节与分页切块解析器（Ingestion Parsers，零 DB、零 LLM）。

职责边界：
- 当 PDF 不属于内置外管局高精模板（或 AI ① 体检蓝图指定 `HEADING_SECTIONS` / `PAGE_WINDOWS`）时，
  面向任意监管报送 PDF（EAST、1104、人行征信、反洗钱、地方金融监管等）执行确定性通用切块；
- 严格遵循 AI ① 产出的 `chunking_plan`（策略选择 + `section_regions` 区域角色与忽略过滤），
  并在无蓝图或 AI 离线时自动走纯程序标题/分页兜底，彻底消除陌生 PDF 拒收死胡同。
"""

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.infra.contracts import FieldSpec, SpecDocument
from app.catalog.ingestion.parsers.pdf_catalog import extract_code_pairs_from_section
from app.catalog.ingestion.parsers.pdf_parser import (
    _parse_type_from_entry,
    extract_inline_enum_values,
    reflow_dictionary_rows,
)

# 单个通用块最大字符预算：既保留完整章节上下文，又不撑爆下游 AI 结构/字段提取窗口
MAX_GENERIC_CHUNK_CHARS = 6000

_TOC_DOT_LINE_RE = re.compile(r"(?:\.{4,}|…{2,}|·{4,})\s*\d+\s*$")
_HEADING_RE = re.compile(
    r"^(?:"
    r"目\s*录$|"
    r"(?:前\s*言|修订记录|修订历史|编写说明|版本说明)$|"
    r"第[一二三四五六七八九十百千0-9]+[章部分篇节](?:\s+\S.*|\s*$)|"
    r"[一二三四五六七八九十]+、\s*\S+|"
    r"（[一二三四五六七八九十]+）\s*\S+|"
    r"附[录表]\s*[一二三四五六七八九十0-9A-Za-z（）()\-\s]*\S*|"
    r"表\s*[A-Za-z0-9\-_.]+\s+\S+|"
    r"\d+(?:\.\d+){1,4}\s+\S+|"
    r"\d+[.、]\s*\S+"
    r")"
)
_DATA_ROW_TYPE_RE = re.compile(
    r"\b(?:VARCHAR2?|CHAR|NUMBER|DECIMAL|INT|INTEGER|DATE|TIMESTAMP|STRING|[CN]\.{0,2}\d+)\b|"
    r"(?:定长字符型|变长字符型|定长数字型|变长数值型|字符型|数值型|日期型|整数型)",
    re.IGNORECASE,
)
_CODE_SET_TITLE_HINTS = ("代码表", "码值表", "码表", "代码字典", "编码表", "分类代码", "行业分类", "币种", "国别", "行政区划")
_RULE_TITLE_HINTS = ("核查规则", "校验规则", "勾稽关系", "检核规则", "修订对照", "修订说明")
_IGNORE_TITLE_HINTS = ("目录", "目 录", "修订记录", "修订历史", "封面", "前言", "编写说明", "版本说明")

_TYPE_SPEC_PATTERN = (
    r"(定长字符型|变长字符型|定长数字型|变长数值型|字符型|数值型|日期型|整数型|时间型|日期时间型|"
    r"VARCHAR2?|CHAR|NUMBER|DECIMAL|NUMERIC|INT|INTEGER|DATE|TIMESTAMP|TEXT|STRING|"
    r"C\.{0,2}\d+|N\.{0,2}\d+(?:[.,]\d+)?)"
    r"(?:[ \t，,（(]*(\d+)(?:[.,]\s*(\d+))?[）)]?)?"
)

# 通用数据字典行 A（英文字段在前，如外管局/通用英文优先字典）：[序号] FIELD_EN 字段中文 类型(长度) [校验说明]
_GENERIC_DICT_ROW_EN_FIRST_RE = re.compile(
    rf"(?m)^[ \t]*(?:\d+[.、]?[ \t]+)?([A-Za-z][A-Za-z0-9_]{{1,40}})[ \t]+([^\s]{{1,40}})[ \t]+{_TYPE_SPEC_PATTERN}(?:[ \t]+(.*))?$",
    re.IGNORECASE,
)
# 通用数据字典行 B（中文数据项在前，如 EAST 5.0 / 1104 / 人行字典）：[序号] 字段中文 FIELD_EN 类型(长度) [校验说明]
_GENERIC_DICT_ROW_CN_FIRST_RE = re.compile(
    rf"(?m)^[ \t]*(?:\d+[.、]?[ \t]+)?([\u4e00-\u9fff][^\s]{{0,39}})[ \t]+([A-Za-z][A-Za-z0-9_]{{1,40}})[ \t]+{_TYPE_SPEC_PATTERN}(?:[ \t]+(.*))?$",
    re.IGNORECASE,
)
_TYPE_KEYWORDS_UPPER = {
    "VARCHAR", "VARCHAR2", "NUMBER", "DECIMAL", "NUMERIC", "CHAR", "DATE", "TIMESTAMP", "INT", "INTEGER", "STRING", "TEXT"
}


def _heading_level(text: str) -> int:
    """估算标题层级（数值越小层级越高），用于 section_regions 未指定 end_heading 时正确继承至子节。"""
    s = text.strip()
    if re.match(r"^(?:目\s*录|前\s*言|修订记录|修订历史|编写说明|版本说明|第[一二三四五六七八九十百千0-9]+[章部分篇]|附[录表])", s):
        return 1
    if re.match(r"^[一二三四五六七八九十]+、", s):
        return 2
    if re.match(r"^(?:（[一二三四五六七八九十]+）|第[一二三四五六七八九十百千0-9]+节)", s):
        return 3
    m = re.match(r"^(\d+(?:\.\d+){1,4})\s+", s)
    if m:
        return 1 + m.group(1).count(".")
    if re.match(r"^(?:\d+[.、]|表\s*[A-Za-z0-9\-_.]+)", s):
        return 3
    return 4


def detect_pdf_profiles(full_text: str) -> List[str]:
    """1ms 级结构指纹探针：告诉 AI ① 与路由层该 PDF 是否命中已知高精模板。"""
    profiles: List[str] = []
    if "第六章 数据接口格式" in full_text:
        profiles.append("SAFE_CHAPTER6")
    if "三、调查表式" in full_text and "四、主要指标解释" in full_text:
        profiles.append("SAFE_FAL_2025")
    if len(re.findall(r"\b[A-Z]\d{2}(?:-\d)?\s*表", full_text)) >= 5:
        profiles.append("SAFE_RULE_DIFF")
    profiles.append("GENERIC_PDF")
    return profiles


def extract_pdf_headings(full_text: str) -> List[Dict[str, Any]]:
    """逐页扫描 PDF 章节标题（过滤目录点线页与表格数据行），返回带页码与字符偏移量的真实标题列表。"""
    headings: List[Dict[str, Any]] = []
    page_no = 1
    offset = 0
    for raw_line in full_text.splitlines(keepends=True):
        form_feeds = raw_line.count("\f")
        clean = re.sub(r"\s+", " ", raw_line.replace("\f", "")).strip()
        if (
            clean
            and len(clean) <= 100
            and not _TOC_DOT_LINE_RE.search(clean)
            and _HEADING_RE.match(clean)
            and not _DATA_ROW_TYPE_RE.search(clean)
        ):
            headings.append(
                {
                    "text": clean,
                    "page": page_no,
                    "offset": offset,
                    "level": _heading_level(clean),
                }
            )
        offset += len(raw_line)
        page_no += form_feeds
    return headings


def default_pdf_strategy(full_text: str, headings: Optional[List[Dict[str, Any]]] = None) -> str:
    """根据文档指纹与标题密度选择默认确定性 PDF 切块策略。"""
    profiles = detect_pdf_profiles(full_text)
    if any(p in profiles for p in ("SAFE_CHAPTER6", "SAFE_FAL_2025", "SAFE_RULE_DIFF")):
        return "SAFE_SPEC_PARSER"
    found = headings if headings is not None else extract_pdf_headings(full_text)
    if len(found) >= 2:
        return "HEADING_SECTIONS"
    return "PAGE_WINDOWS"


def _is_toc_page(page_text: str) -> bool:
    lines = [ln.strip() for ln in page_text.splitlines() if ln.strip()]
    if not lines:
        return True
    dot_lines = sum(1 for ln in lines if _TOC_DOT_LINE_RE.search(ln))
    return dot_lines >= 4 and dot_lines / max(1, len(lines)) >= 0.35


def _infer_section_role(title: str, body: str) -> str:
    compact_title = re.sub(r"\s+", "", title)
    if any(hint in compact_title for hint in _IGNORE_TITLE_HINTS):
        return "IGNORE"
    if any(hint in title for hint in _CODE_SET_TITLE_HINTS):
        return "CODE_SET"
    if any(hint in title for hint in _RULE_TITLE_HINTS):
        return "RULE_EVIDENCE"
    if any(kw in body for kw in ("字段名", "数据类型", "字符型", "数值型", "VARCHAR", "DECIMAL", "NUMBER", "校验")):
        return "TABLE_METADATA"
    return "NARRATIVE"


def _make_table_code(prefix: str, title: str, ordinal: int) -> str:
    ascii_tokens = re.findall(r"[A-Za-z][A-Za-z0-9_]{1,24}", title)
    if ascii_tokens:
        raw = f"{prefix}_{ascii_tokens[0].upper()}"
    else:
        num_match = re.match(r"^\s*(\d+(?:\.\d+){0,3})", title)
        if num_match:
            raw = f"{prefix}_{num_match.group(1).replace('.', '_')}"
        else:
            digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:8].upper()
            raw = f"{prefix}_{ordinal + 1:02d}_{digest}"
    return re.sub(r"[^A-Z0-9_]", "_", raw.upper())[:80]


def _resolve_generic_type(
    field_cn: str,
    raw_kind: str,
    raw_len: Optional[str],
    raw_dec: Optional[str],
    tail: str,
) -> Tuple[str, int, Optional[int]]:
    upper_kind = raw_kind.upper()
    # 支持金融行业常见简写：C..30 / C18 / N..20,2 / N18.2
    short_c = re.fullmatch(r"C\.{0,2}(\d+)", upper_kind)
    if short_c:
        return "VARCHAR", int(short_c.group(1)), None
    short_n = re.fullmatch(r"N\.{0,2}(\d+)(?:[.,](\d+))?", upper_kind)
    if short_n:
        return "NUMBER", int(short_n.group(1)), int(short_n.group(2)) if short_n.group(2) else None
    if (
        upper_kind in {"NUMBER", "DECIMAL", "NUMERIC", "INT", "INTEGER"}
        or "数值" in raw_kind
        or "整数" in raw_kind
        or "数字" in raw_kind
    ):
        length = int(raw_len) if raw_len else (10 if "INT" in upper_kind or "整数" in raw_kind else 22)
        decimals = int(raw_dec) if raw_dec else (0 if "INT" in upper_kind or "整数" in raw_kind else None)
        return "NUMBER", length, decimals
    if upper_kind in {"DATE", "TIMESTAMP"} or "日期" in raw_kind or "时间" in raw_kind:
        length = int(raw_len) if raw_len else (8 if upper_kind == "DATE" or raw_kind == "日期型" else 19)
        return "VARCHAR", length, None
    if any(ch in raw_kind for ch in ("字符", "数值", "日期", "定长", "变长")):
        _, data_type, length, decimals, _, _ = _parse_type_from_entry(
            f"{field_cn} {raw_kind}{('，' + raw_len) if raw_len else ''}{('.' + raw_dec) if raw_dec else ''} {tail}",
            field_cn,
        )
        return data_type, length, decimals
    return "VARCHAR", int(raw_len) if raw_len else 128, None


def _scan_generic_fields(candidate_text: str, page: str) -> List[FieldSpec]:
    matches_by_pos: List[Tuple[int, str, str, str, Optional[str], Optional[str], str]] = []
    for match in _GENERIC_DICT_ROW_EN_FIRST_RE.finditer(candidate_text):
        matches_by_pos.append(
            (match.start(), match.group(1), match.group(2), match.group(3), match.group(4), match.group(5), match.group(6) or "")
        )
    for match in _GENERIC_DICT_ROW_CN_FIRST_RE.finditer(candidate_text):
        matches_by_pos.append(
            (match.start(), match.group(2), match.group(1), match.group(3), match.group(4), match.group(5), match.group(6) or "")
        )
    matches_by_pos.sort(key=lambda item: item[0])

    fields: List[FieldSpec] = []
    seen = set()
    for _, raw_en, raw_cn, raw_kind, raw_len, raw_dec, raw_tail in matches_by_pos:
        field_en = raw_en.strip().upper()
        field_cn = raw_cn.strip()
        if field_en in seen or field_en in _TYPE_KEYWORDS_UPPER:
            continue
        seen.add(field_en)
        data_type, length, decimals = _resolve_generic_type(field_cn, raw_kind.strip(), raw_len, raw_dec, raw_tail.strip())
        fields.append(
            FieldSpec(
                name=field_en,
                content=field_cn or field_en,
                data_type=data_type,
                length=max(1, length),
                decimal_places=decimals,
                check_text=raw_tail.strip()[:500],
                evidence_page=page,
            )
        )
    return fields


def _try_parse_generic_fields(body: str, page: str) -> List[FieldSpec]:
    """不依赖特定监管机构的通用字段表预解析：同时支持英文在前与中文在前（如 EAST 5.0/1104）数据字典。"""
    # 优先在原始分行文本上提取（避免外管局专用 reflow 误拼接中文在前或带序号的数据行）
    direct_fields = _scan_generic_fields(body, page)
    if len(direct_fields) >= 2:
        return direct_fields
    reflowed = reflow_dictionary_rows(body)
    if reflowed and len(reflowed) >= 20:
        reflowed_fields = _scan_generic_fields(reflowed, page)
        if len(reflowed_fields) >= 2:
            return reflowed_fields
    return []


def _subsplit_large_text(text: str, max_chars: int = MAX_GENERIC_CHUNK_CHARS) -> List[str]:
    cleaned = text.strip()
    if not cleaned:
        return []
    if len(cleaned) <= max_chars:
        return [cleaned]
    parts: List[str] = []
    paragraphs = re.split(r"\n{2,}", cleaned)
    buf: List[str] = []
    buf_len = 0
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:
            if buf:
                parts.append("\n\n".join(buf))
                buf, buf_len = [], 0
            # 超长单段按换行切分
            lines = para.splitlines()
            line_buf: List[str] = []
            line_len = 0
            for ln in lines:
                if line_len + len(ln) + 1 > max_chars and line_buf:
                    parts.append("\n".join(line_buf))
                    line_buf, line_len = [], 0
                line_buf.append(ln)
                line_len += len(ln) + 1
            if line_buf:
                parts.append("\n".join(line_buf))
            continue
        if buf_len + len(para) + 2 > max_chars and buf:
            parts.append("\n\n".join(buf))
            buf, buf_len = [], 0
        buf.append(para)
        buf_len += len(para) + 2
    if buf:
        parts.append("\n\n".join(buf))
    return parts


def _build_section_block(
    *,
    ordinal: int,
    title: str,
    body: str,
    page: int,
    role: str,
    path: Path,
    digest: str,
    strategy_tag: str,
) -> Dict[str, Any]:
    raw_content = f"{title}\n{body}".strip() if title and not body.startswith(title) else body.strip()
    page_str = str(max(1, page))
    if role == "CODE_SET":
        code = _make_table_code("CS", title, ordinal)
        pairs = extract_code_pairs_from_section(body, page_str, title)
        code_spec = {
            "code": code,
            "name": title,
            "section": title[:64],
            "page": page_str,
            "source_type": "DOCUMENT_TABLE",
            "definition": f"通用 PDF 章节切块提取的码表：{title}",
            "values": pairs,
            "eligible": len(pairs) > 0,
        }
        return {
            "block_type": "CODE_SET_CANDIDATE",
            "raw_content": raw_content,
            "source_locator": {"section": title[:64], "code_set_code": code, "page": page_str, "strategy": strategy_tag},
            "content_json": {
                "table_code": code,
                "table_name": title,
                "section": title[:64],
                "eligible": len(pairs) > 0,
                "candidate_role": "CODE_SET",
                "reason": (
                    f"通用 PDF 码表章节（预提取 {len(pairs)} 个码值项）"
                    if pairs
                    else "通用 PDF 码表章节，待 AI 辅助提取码值项"
                ),
                "field_count": len(pairs),
                "code_set_spec": code_spec if pairs else None,
            },
        }

    table_code = _make_table_code("DOC", title, ordinal)
    parsed_fields = _try_parse_generic_fields(body, page_str) if role == "TABLE_METADATA" else []
    spec_dump = None
    inline_code_sets: List[Dict[str, Any]] = []
    if parsed_fields:
        spec = SpecDocument(
            version="GENERIC_PDF",
            table_code=table_code,
            table_name=title,
            source_name=path.name,
            source_sha256=digest,
            section=title[:64],
            fields=parsed_fields,
            terms_text="",
            dictionary_text=raw_content,
        )
        spec_dump = spec.model_dump()
        for f in parsed_fields:
            enums = extract_inline_enum_values(f.check_text)
            if enums:
                inline_code_sets.append(
                    {
                        "code": re.sub(r"[^A-Z0-9_]", "_", f"{table_code}_{f.name}_ENUM".upper())[:90],
                        "name": f"{f.content}（{f.name} 内联枚举）",
                        "definition": f"提取自 {title} 字段 {f.name} 的内联枚举码值",
                        "source_type": "INLINE_ENUM",
                        "field_en": f.name,
                        "field_cn": f.content,
                        "table_code": table_code,
                        "page": page_str,
                        "values": enums,
                    }
                )

    block_type = {
        "TABLE_METADATA": "TABLE_CANDIDATE",
        "RULE_EVIDENCE": "RULE_EVIDENCE",
        "NARRATIVE": "NARRATIVE",
    }.get(role, "TABLE_CANDIDATE")

    return {
        "block_type": block_type,
        "raw_content": raw_content,
        "source_locator": {"section": title[:64], "table_code": table_code, "page": page_str, "strategy": strategy_tag},
        "content_json": {
            "table_code": table_code,
            "table_name": title,
            "section": title[:64],
            "eligible": bool(spec_dump),
            "candidate_role": role,
            "field_count": len(parsed_fields),
            "reason": (
                f"通用 PDF 章节切块已预识别 {len(parsed_fields)} 个字段"
                if parsed_fields
                else f"通用 PDF 章节切块（角色={role}），交由下游 AI 结构确认与提取"
            ),
            "spec": spec_dump,
            "inline_code_sets": inline_code_sets,
        },
    }


def _match_heading(headings: List[Dict[str, Any]], target: str, *, after_idx: int = -1) -> Optional[int]:
    norm_target = re.sub(r"\s+", "", str(target or "")).lower()
    if not norm_target:
        return None
    for idx in range(after_idx + 1, len(headings)):
        norm_text = re.sub(r"\s+", "", headings[idx]["text"]).lower()
        if norm_text == norm_target or norm_target in norm_text:
            return idx
    return None


def extract_pdf_generic_sections(
    full_text: str,
    path: Path,
    digest: str,
    *,
    chunking_plan: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """通用二级 PDF 解析器（HEADING_SECTIONS）：按 AI ① 区域蓝图或自动识别的章节标题切块。"""
    headings = extract_pdf_headings(full_text)
    if not headings:
        return []

    # 1) 构建每条标题对应的角色覆盖与忽略集（来自 AI ① 的 section_regions 蓝图）
    regions = (chunking_plan or {}).get("section_regions") if isinstance(chunking_plan, dict) else None
    role_by_heading_idx: Dict[int, str] = {}
    if isinstance(regions, list):
        for region in regions:
            if not isinstance(region, dict):
                continue
            role = str(region.get("role") or "").upper()
            if role not in {"TABLE_METADATA", "CODE_SET", "RULE_EVIDENCE", "NARRATIVE", "IGNORE"}:
                continue
            start_idx = _match_heading(headings, region.get("start_heading", ""))
            if start_idx is None:
                continue
            end_heading = region.get("end_heading")
            end_idx = _match_heading(headings, end_heading, after_idx=start_idx) if end_heading else None
            if end_idx is None:
                # 未指定 end_heading 时：向下覆盖该标题的所有更深子节，遇到同级或更高级标题自动止步
                start_level = headings[start_idx].get("level", 4)
                end_idx = start_idx + 1
                while end_idx < len(headings) and headings[end_idx].get("level", 4) > start_level:
                    end_idx += 1
            for h_idx in range(start_idx, min(end_idx, len(headings))):
                role_by_heading_idx[h_idx] = role

    blocks: List[Dict[str, Any]] = []
    for idx, item in enumerate(headings):
        start_offset = item["offset"]
        end_offset = headings[idx + 1]["offset"] if idx + 1 < len(headings) else len(full_text)
        section_raw = full_text[start_offset:end_offset].strip()
        # 剔除纯目录页与极短无正文空标题
        if not section_raw or _is_toc_page(section_raw):
            continue
        lines = section_raw.splitlines()
        body_only = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        if len(body_only) < 15:
            continue

        title = item["text"]
        role = role_by_heading_idx.get(idx) or _infer_section_role(title, body_only)
        if role == "IGNORE":
            continue

        for sub_idx, part in enumerate(_subsplit_large_text(body_only, MAX_GENERIC_CHUNK_CHARS)):
            sub_title = title if sub_idx == 0 else f"{title}（续 {sub_idx + 1}）"
            blocks.append(
                _build_section_block(
                    ordinal=len(blocks),
                    title=sub_title,
                    body=part,
                    page=item["page"],
                    role=role,
                    path=path,
                    digest=digest,
                    strategy_tag="HEADING_SECTIONS",
                )
            )
    return blocks


def extract_pdf_page_windows(
    full_text: str,
    path: Path,
    digest: str,
    *,
    pages_per_chunk: int = 3,
    chunking_plan: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """通用三级 PDF 兜底解析器（PAGE_WINDOWS）：无规范章节编号时按页窗切片，绝不拒收。"""
    pages = full_text.split("\f")
    blocks: List[Dict[str, Any]] = []
    buf_texts: List[str] = []
    buf_start_page = 1
    buf_end_page = 1
    buf_chars = 0

    def _flush() -> None:
        nonlocal buf_texts, buf_chars
        if not buf_texts:
            return
        combined = "\n\n".join(buf_texts).strip()
        if combined:
            title = f"第 {buf_start_page}-{buf_end_page} 页" if buf_start_page != buf_end_page else f"第 {buf_start_page} 页"
            role = _infer_section_role(title, combined)
            if role == "IGNORE":
                role = "NARRATIVE"
            blocks.append(
                _build_section_block(
                    ordinal=len(blocks),
                    title=title,
                    body=combined,
                    page=buf_start_page,
                    role=role,
                    path=path,
                    digest=digest,
                    strategy_tag="PAGE_WINDOWS",
                )
            )
        buf_texts = []
        buf_chars = 0

    for page_idx, raw_page in enumerate(pages, start=1):
        cleaned = raw_page.strip()
        if not cleaned or _is_toc_page(cleaned):
            continue
        if buf_texts and (
            (page_idx - buf_start_page) >= pages_per_chunk or buf_chars + len(cleaned) > MAX_GENERIC_CHUNK_CHARS
        ):
            _flush()
        if not buf_texts:
            buf_start_page = page_idx
        buf_end_page = page_idx
        buf_texts.append(cleaned)
        buf_chars += len(cleaned)
    _flush()
    return blocks
