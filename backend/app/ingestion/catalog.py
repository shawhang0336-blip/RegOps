import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.infra.contracts import FieldSpec, SpecDocument
from app.ingestion.spec_parser import (
    SpecParseError,
    _between,
    _extract_pdf_text,
    _parse_fields,
    extract_code_reference_from_text,
    extract_inline_enum_values,
    reflow_spec_terms,
)

MARKERS = {"数据术语解释", "数据格式", "数据字典"}


def _table_code(section: str) -> str:
    return "SAFE_" + section.replace(".", "_")


def _heading_before(text: str, section: str, position: int):
    matches = list(re.finditer(rf"(?m)^\s*{re.escape(section)}\s+([^\n]+)$", text[:position]))
    if not matches:
        raise SpecParseError(f"Cannot find table heading for {section}")
    match = matches[-1]
    return match.start(), f"{section} {match.group(1).strip()}"


def _collect_inline_code_sets(spec: SpecDocument) -> List[Dict[str, Any]]:
    inline_sets: List[Dict[str, Any]] = []
    for field in spec.fields:
        combined_text = f"{field.check_text}\n{spec.terms_text}"
        # Prefer extracting from field's own check_text first
        enums = extract_inline_enum_values(field.check_text)
        if not enums and field.content:
            # Look up field's paragraph in terms_text
            idx = spec.terms_text.find(field.content)
            if idx >= 0:
                enums = extract_inline_enum_values(spec.terms_text[idx : idx + 450])
        if enums:
            code_set_code = re.sub(r"[^A-Z0-9_]", "_", f"{spec.table_code}_{field.name}_ENUM".upper())
            inline_sets.append(
                {
                    "code": code_set_code[:90],
                    "name": f"{field.content}（{field.name} 内联枚举）",
                    "definition": f"提取自 {spec.table_name} 字段 {field.name}（{field.content}）的内联枚举码值",
                    "source_type": "INLINE_ENUM",
                    "field_en": field.name,
                    "field_cn": field.content,
                    "table_code": spec.table_code,
                    "page": field.evidence_page,
                    "values": enums,
                }
            )
    return inline_sets


def _parse_chapter6_catalog(full_text: str, path: Path, digest: str) -> List[Dict[str, Any]]:
    chapter_start = full_text.rfind("第六章 数据接口格式")
    chapter_end = full_text.find("第七章", chapter_start)
    text = full_text[chapter_start : chapter_end if chapter_end >= 0 else len(full_text)]
    version_match = re.search(r"数据采集规范（V([0-9.]+)）", full_text[:5000])
    document_version = f"SAFE_{version_match.group(1)}" if version_match else "UNKNOWN"
    marker_pattern = re.compile(r"(?m)^\s*(6(?:\.\d+){1,4})\s+(数据术语解释|数据格式|数据字典)\s*$")
    markers = list(marker_pattern.finditer(text))
    dictionary_markers = [match for match in markers if match.group(2) == "数据字典"]
    entries: List[Dict[str, Any]] = []
    for index, dictionary_marker in enumerate(dictionary_markers):
        section = dictionary_marker.group(1).rsplit(".", 1)[0]
        title_start, table_name = _heading_before(text, section, dictionary_marker.start())
        next_title_start = len(text)
        if index + 1 < len(dictionary_markers):
            next_section = dictionary_markers[index + 1].group(1).rsplit(".", 1)[0]
            next_title_start, _ = _heading_before(text, next_section, dictionary_markers[index + 1].start())
        section_text = text[title_start:next_title_start]
        code = _table_code(section)
        page = max(1, full_text[: chapter_start + title_start].count("\f") + 1 - 12)
        base: Dict[str, Any] = {
            "table_code": code,
            "table_name": table_name,
            "section": section,
            "eligible": False,
            "reason": "",
            "spec": None,
        }
        parse_warnings: List[str] = []
        try:
            local_markers = list(marker_pattern.finditer(section_text))
            terms_marker = next((item for item in local_markers if item.group(2) == "数据术语解释"), None)
            format_marker = next((item for item in local_markers if item.group(2) == "数据格式"), None)
            if not format_marker:
                raise SpecParseError(f"Cannot find data format for {section}")
            terms_text = section_text[terms_marker.end():format_marker.start()] if terms_marker else ""
            format_text = section_text[format_marker.end():local_markers[-1].start()]
            dictionary_text = section_text[local_markers[-1].end():]
            terms_text = reflow_spec_terms(terms_text)
            fields = _parse_fields(
                format_text,
                dictionary_text,
                str(page),
                repair_layout=section == "6.1",
                parse_warnings=parse_warnings,
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
            inline_sets = _collect_inline_code_sets(spec)
            base.update(
                {
                    "eligible": True,
                    "reason": "已从数据格式和数据字典提取字段；术语解释为可选内容" + (f"；存在 {len(parse_warnings)} 项解析兜底，需人工确认" if parse_warnings else ""),
                    "field_count": len(fields),
                    "parse_warnings": parse_warnings,
                    "human_required": bool(parse_warnings),
                    "spec": spec.model_dump(),
                    "inline_code_sets": inline_sets,
                }
            )
        except (SpecParseError, ValueError) as exc:
            base["reason"] = f"暂不符合第一步结构化解析条件：{exc}"
        entries.append(base)
    return entries


def _parse_statistical_system_catalog(full_text: str, path: Path, digest: str) -> List[Dict[str, Any]]:
    """Parse SAFE Statistical System PDF (e.g. 汇发2025_22号_附件1_对外金融资产负债及交易统计制度.pdf)."""
    s3_start = full_text.rfind("三、调查表式")
    s4_start = full_text.rfind("四、主要指标解释")
    s5_start = full_text.rfind("五、附录")
    if s3_start < 0 or s4_start < 0:
        return []
    s3_text = full_text[s3_start:s4_start]
    s4_text = full_text[s4_start : s5_start if s5_start > s4_start else len(full_text)]

    # Extract all field definitions in Section 四: e.g. Z0101.申报主体名称：...
    indicator_pattern = re.compile(r"(?m)^\s*([A-Z]\d{3,4}[A-Z0-9_]*)\s*[.．]\s*([^：:\n]+)[：:](.*)$")
    ind_matches = list(indicator_pattern.finditer(s4_text))
    indicators_by_prefix: Dict[str, List[Tuple[str, str, str]]] = {}
    for idx, m in enumerate(ind_matches):
        f_code = m.group(1).strip()
        f_cn = m.group(2).strip()
        end_pos = ind_matches[idx + 1].start() if idx + 1 < len(ind_matches) else len(s4_text)
        body = (m.group(3) + "\n" + s4_text[m.end() : end_pos]).strip()
        # Keep only up to next table header or example diagram
        body = re.split(r"(?m)^\s*(?:[A-Z]\d{2}(?:-\d)?\s*表[：:]|（[一二三四五六七八九十]+）|[0-9]+\.\s*相关举例)", body)[0].strip()
        body_clean = re.sub(r"\s+", " ", body)
        prefix = f_code[:3]  # e.g. Z01, A01, E01
        indicators_by_prefix.setdefault(prefix, []).append((f_code, f_cn, body_clean))

    # Also scan survey forms in Section 三 to get exact table codes (e.g. Z01, A01-1, A01-2, E01-1..E01-4)
    form_pattern = re.compile(r"表\s*号[：:]\s*外统\s*([A-Z0-9-]+)\s*表")
    form_matches = list(form_pattern.finditer(s3_text))
    entries: List[Dict[str, Any]] = []
    seen_codes = set()
    for idx, m in enumerate(form_matches):
        raw_tcode = m.group(1).strip()
        if raw_tcode in seen_codes:
            continue
        seen_codes.add(raw_tcode)
        # Find table title before "表 号：外统 ..."
        window_before = s3_text[max(0, m.start() - 300) : m.start()]
        lines_before = [
            ln.strip()
            for ln in window_before.splitlines()
            if ln.strip()
            and not re.match(r"^(?:[0-9]+|（[一二三四五六七八九十]+）.*|三、调查表式|申报主体.*)$", ln.strip())
        ]
        t_title = lines_before[-1] if lines_before else f"{raw_tcode} 表"
        t_title = re.sub(r"\s+[0-9]+$", "", t_title).strip()
        next_form_start = form_matches[idx + 1].start() if idx + 1 < len(form_matches) else len(s3_text)
        form_slice = s3_text[m.start() : next_form_start]
        form_codes = set(re.findall(r"\b([A-Z]\d{3,4}[A-Z0-9_]*)\b", form_slice))

        prefix = raw_tcode.split("-")[0]
        candidate_inds = indicators_by_prefix.get(prefix, [])
        if form_codes:
            matched_inds = [item for item in candidate_inds if item[0] in form_codes]
            if not matched_inds:
                matched_inds = candidate_inds
        else:
            matched_inds = candidate_inds

        page = max(1, full_text[: s3_start + m.start()].count("\f") + 1)
        table_code = "FAL_2025_" + raw_tcode.replace("-", "_")
        fields: List[FieldSpec] = []
        for f_code, f_cn, f_desc in matched_inds:
            is_num = any(k in f_cn or k in f_desc for k in ("比例", "金额", "数量", "市价", "市值", "人数", "资产", "负债", "权益", "利润", "收入", "支出", "余额", "周期"))
            is_int = "人数" in f_cn
            fields.append(
                FieldSpec(
                    name=f_code,
                    content=f_cn,
                    data_type="NUMBER" if is_num else "VARCHAR",
                    length=10 if is_int else (22 if is_num else 128),
                    decimal_places=0 if is_int else (2 if is_num else None),
                    check_text=f_desc[:500],
                    evidence_page=str(page),
                )
            )
        if not fields:
            continue
        terms_text = "\n".join(f"{f.name} {f.content}：{f.check_text}" for f in fields)
        spec = SpecDocument(
            version="SAFE_FAL_2025",
            table_code=table_code,
            table_name=f"{raw_tcode} 表：{t_title}",
            source_name=path.name,
            source_sha256=digest,
            section=raw_tcode,
            fields=fields,
            terms_text=terms_text,
            dictionary_text=terms_text,
        )
        entries.append(
            {
                "table_code": table_code,
                "table_name": f"{raw_tcode} 表：{t_title}",
                "section": raw_tcode,
                "eligible": True,
                "reason": "从调查表式与主要指标解释联合提取字段定义及内联码值；类型长度需人工确认",
                "field_count": len(fields),
                "spec": spec.model_dump(),
                "human_required": True,
                "parse_warnings": ["字段类型与长度由指标名称及解释关键词推断，发布前必须逐项确认"],
                "inline_code_sets": _collect_inline_code_sets(spec),
            }
        )
    return entries


def _parse_rules_or_revision_catalog(full_text: str, path: Path, digest: str) -> List[Dict[str, Any]]:
    """Parse Verification Rules PDF (2024核查规则) or Revision Comparison PDF (修订对照表/修订说明) by target table."""
    version = "SAFE_FAL_2025_DIFF" if "修订对照表" in path.name or "2025" in path.name else "SAFE_FAL_2024_RULES"
    table_mentions = re.compile(r"\b([A-Z]\d{2}(?:-\d)?)\s*表")
    grouped: Dict[str, List[Tuple[str, str, int]]] = {}
    pages = full_text.split("\f")
    for page_idx, page_text in enumerate(pages, start=1):
        for para in re.split(r"\n\s*\n", page_text):
            para_clean = re.sub(r"\s+", " ", para).strip()
            if len(para_clean) < 12:
                continue
            tables_found = list(dict.fromkeys(table_mentions.findall(para_clean)))
            for tcode in tables_found:
                grouped.setdefault(tcode, []).append((para_clean, str(page_idx), len(para_clean)))

    entries: List[Dict[str, Any]] = []
    for tcode, items in grouped.items():
        # Extract referenced field codes like Z0102, A0108, Z0104CODE
        field_Map: Dict[str, Tuple[str, List[str], str]] = {}
        for para_clean, page_str, _ in items:
            # Match pattern like Z0102（证照类别） or Z0105.申报主体所属行业标识码 or A0108[期末（申报主体）表决权比例（%）]
            for fm in re.finditer(r"\b([A-Z]\d{3,4}[A-Z0-9_]*)\s*(?:[（(\[.]([^）)\]\n：:]{2,32})[）)\]]?)?", para_clean):
                f_en = fm.group(1).strip()
                f_cn = (fm.group(2) or f_en).strip(" .．：:")
                if not f_cn or len(f_cn) > 30:
                    f_cn = f_en
                prev = field_Map.get(f_en)
                if not prev:
                    field_Map[f_en] = (f_cn, [para_clean], page_str)
                else:
                    if para_clean not in prev[1]:
                        prev[1].append(para_clean)
        if not field_Map:
            continue
        fields: List[FieldSpec] = []
        for f_en, (f_cn, rules_list, page_str) in field_Map.items():
            joined_check = "；".join(rules_list)[:600]
            is_num = any(k in f_cn or k in joined_check for k in ("比例", "金额", "数量", "市价", "市值", "人数", "资产", "负债", "利润"))
            fields.append(
                FieldSpec(
                    name=f_en,
                    content=f_cn,
                    data_type="NUMBER" if is_num else "VARCHAR",
                    length=22 if is_num else 128,
                    decimal_places=2 if is_num else None,
                    check_text=joined_check,
                    evidence_page=page_str,
                )
            )
        table_code = f"{version}_{tcode.replace('-', '_')}"
        raw_joined = "\n".join(p for p, _, _ in items)
        spec = SpecDocument(
            version=version,
            table_code=table_code,
            table_name=f"{tcode} 表（核查与修订规则提取）",
            source_name=path.name,
            source_sha256=digest,
            section=tcode,
            fields=fields,
            terms_text=raw_joined,
            dictionary_text=raw_joined,
        )
        entries.append(
            {
                "table_code": table_code,
                "table_name": f"{tcode} 表（核查与修订规则提取）",
                "section": tcode,
                "eligible": True,
                "candidate_role": "REVISION_DIFF" if "修订" in path.name else "RULE_EVIDENCE",
                "reason": f"按表聚合提取 {len(fields)} 个涉及字段的核查规则与修订说明，仅作为规则证据，不作为完整建表结构",
                "field_count": len(fields),
                "spec": spec.model_dump(),
                "inline_code_sets": _collect_inline_code_sets(spec),
            }
        )
    return entries


def parse_document_catalog(pdf_path: str) -> List[Dict[str, Any]]:
    path = Path(pdf_path).resolve()
    if not path.is_file():
        raise SpecParseError(f"Source PDF not found: {path}")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    full_text = _extract_pdf_text(path)

    if "第六章 数据接口格式" in full_text:
        entries = _parse_chapter6_catalog(full_text, path, digest)
        if entries:
            return entries

    if "三、调查表式" in full_text and "四、主要指标解释" in full_text:
        entries = _parse_statistical_system_catalog(full_text, path, digest)
        if entries:
            return entries

    entries = _parse_rules_or_revision_catalog(full_text, path, digest)
    if entries:
        return entries

    raise SpecParseError("No table candidates were discovered in document")


def _extract_code_pairs_from_section(section_text: str, section_id: str, title: str) -> List[Dict[str, str]]:
    """Deterministically parse code-value rows from Chapter 9 or Appendix sections."""
    values: List[Dict[str, str]] = []
    seen = set()

    for raw_line in section_text.splitlines():
        line = raw_line.strip()
        if not line or "国家外汇管理局" in line or re.fullmatch(r"\d{1,3}", line):
            continue
        if any(h in line for h in ("代码", "名称", "类型", "表号", "中文全称", "行业类别", "序号", "商户介绍")) and len(line) < 45:
            continue

        # 1. Appendix 1 Industry Category: "01   农业   0101" or "6610  中央银行服务  6610"
        m_ind = re.match(r"^(\d{2,4})\s+([\u4e00-\u9fa5、，（）()/\s]{2,36}?)\s+(\d{4})$", line)
        if m_ind:
            orig_code, label, safe_code = m_ind.group(1), re.sub(r"\s+", "", m_ind.group(2)), m_ind.group(3)
            if safe_code not in seen:
                seen.add(safe_code)
                values.append({"value": safe_code, "label_cn": label, "label_en": orig_code, "definition": f"国标行业代码 {orig_code}"})
            continue

        # 2. Appendix 2 Country table: "CHN 中华人民共和国 中国 the People's Republic of China CHINA"
        m_cty = re.match(r"^([A-Z]{3})\s+([\u4e00-\u9fa5（）()、·]{2,20})\s+([\u4e00-\u9fa5（）()、·]{2,20})\s+([A-Za-z].*)$", line)
        if m_cty:
            code, full_cn, short_cn, en_part = m_cty.group(1), m_cty.group(2), m_cty.group(3), m_cty.group(4).strip()
            if code not in seen:
                seen.add(code)
                values.append({"value": code, "label_cn": short_cn, "label_en": en_part, "definition": full_cn})
            continue

        # 3. Appendix 3 Currency 2-column table: "ADP 安道尔比赛塔   EGP 埃及镑"
        m_ccy_pair = re.findall(r"([A-Z]{3})\s+([\u4e00-\u9fa5A-Za-z（）()]{2,18})", line)
        if len(m_ccy_pair) >= 1 and all(len(c) == 3 for c, _ in m_ccy_pair) and ("币种" in title or section_id in {"三", "9.2"}):
            for code, label in m_ccy_pair:
                if code not in seen:
                    seen.add(code)
                    values.append({"value": code, "label_cn": label.strip(), "label_en": code, "definition": ""})
            continue

        # 4. Hierarchical multi-column tables (e.g. 9.16 / 9.17 / 9.20.1 / 9.21.1): capture rightmost code + label pair
        m_hier = re.search(r"([\u4e00-\u9fa5（）()/、A-Za-z0-9]{2,28}?)\s+(\d{4,8})\s*$", line)
        if m_hier and not re.match(r"^\d", m_hier.group(1)):
            label, code = m_hier.group(1).strip(), m_hier.group(2).strip()
            if code not in seen:
                seen.add(code)
                values.append({"value": code, "label_cn": label, "label_en": "", "definition": ""})
            continue

        # 5. Standard "CODE   LABEL   [DESC]" line (e.g. "001 支付货款", "1000 经常项目-外汇结算账户", "Z01 Z01表单位基本情况信息")
        m_std = re.match(r"^([A-Z0-9][A-Z0-9_-]{0,9})\s+([\u4e00-\u9fa5A-Za-z0-9-\uff08\uff09()、/；;，,]{2,40})(?:\s+(.*))?$", line)
        if m_std:
            code, label, desc = m_std.group(1).strip(), m_std.group(2).strip(), (m_std.group(3) or "").strip()
            if code.isdigit() and len(code) <= 2 and desc and re.match(r"^[A-Z]{2,8}$", desc.split()[0]):
                # Appendix 6/7 numbered list (e.g. "1 African Development Bank ...")
                continue
            if code not in seen and not any(w in label for w in ("代码表", "国家外汇管理局", "类型代码")):
                seen.add(code)
                values.append({"value": code, "label_cn": label, "label_en": "", "definition": desc})

    return values


def parse_document_code_sets(pdf_path: str) -> List[Dict[str, Any]]:
    """Scan PDF for standalone code tables (Chapter 9, Section 1.4.3.1, Appendix 一~九) and inline field enums."""
    path = Path(pdf_path).resolve()
    if not path.is_file():
        return []
    full_text = _extract_pdf_text(path)
    code_sets: List[Dict[str, Any]] = []

    # 1. SAFE 1.3 Chapter 9 Code Sets
    ch9_start = full_text.rfind("第九章 代码表")
    if ch9_start >= 0:
        ch9_text = full_text[ch9_start:]
        headings = list(re.finditer(r"(?m)^\s*(9\.\d+(?:\.\d+)?)\s+([^\n]+)$", ch9_text))
        for idx, m in enumerate(headings):
            sec_id = m.group(1).strip()
            title = m.group(2).strip()
            next_start = headings[idx + 1].start() if idx + 1 < len(headings) else len(ch9_text)
            sec_body = ch9_text[m.end() : next_start].strip()
            if not sec_body:
                continue
            page = max(1, full_text[: ch9_start + m.start()].count("\f") + 1 - 12)
            values = _extract_code_pairs_from_section(sec_body, sec_id, title)
            if not values and "门户网发布" in sec_body:
                continue
            code_sets.append(
                {
                    "code": f"SAFE_CH9_{sec_id.replace('.', '_')}",
                    "name": title,
                    "section": sec_id,
                    "page": str(page),
                    "source_type": "DOCUMENT_TABLE",
                    "definition": f"《数据采集规范》第九章 {sec_id} {title}",
                    "raw_content": f"{sec_id} {title}\n{sec_body}",
                    "values": values,
                    "eligible": len(values) > 0,
                }
            )

    # 2. SAFE 2025 Statistical System Section 五 Appendices (一) ~ (九)
    app_start = full_text.rfind("五、附录")
    if app_start >= 0:
        app_text = full_text[app_start:]
        app_headings = list(re.finditer(r"(?m)^\s*（([一二三四五六七八九十]+)）\s*([^\n]+)$", app_text))
        cn_num_map = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
        for idx, m in enumerate(app_headings):
            cn_num = m.group(1)
            if cn_num not in cn_num_map:
                continue
            title = m.group(2).strip()
            next_start = app_headings[idx + 1].start() if idx + 1 < len(app_headings) else len(app_text)
            sec_body = app_text[m.end() : next_start].strip()
            page = max(1, full_text[: app_start + m.start()].count("\f") + 1)
            values = _extract_code_pairs_from_section(sec_body, cn_num, title)
            code_sets.append(
                {
                    "code": f"FAL_2025_APP_{cn_num_map[cn_num]}",
                    "name": title,
                    "section": f"附录（{cn_num}）",
                    "page": str(page),
                    "source_type": "DOCUMENT_APPENDIX",
                    "definition": f"《对外金融资产负债及交易统计制度》附录（{cn_num}）{title}",
                    "raw_content": f"附录（{cn_num}）{title}\n{sec_body[:12000]}",
                    "values": values,
                    "eligible": len(values) > 0,
                }
            )

    return code_sets
