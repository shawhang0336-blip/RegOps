"""元数据提取服务与资产层统一入口（Metadata Service）。

职责边界：
- 确定性优先 + 疑难字段小批次 AI 辅助提取：`extract_metadata_for_block`；
- 草稿落库与历史修订留痕：`_persist_draft`；
- 同时聚合导出 `metadata_views.py` 与 `metadata_mutations.py` 接口，供 API 与下游服务统一调用。
"""

import hashlib
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalog.tables.domain import (
    MetadataFieldDraft,
    MetadataTableDraft,
    require_valid_metadata,
    validate_metadata_draft,
)
from app.catalog.tables.mutations import (
    IMPORT_HEADER_ALIASES,
    TYPE_ALIASES,
    apply_field_type_import,
    normalize_type_name,
    publish_table,
    remove_table,
    update_field,
)
from app.catalog.tables.views import (
    CONTEXT_AFTER,
    CONTEXT_BEFORE,
    CONTEXT_MAX_CHARS,
    TYPE_BASIS_INFERRED,
    TYPE_BASIS_MANUAL,
    TYPE_BASIS_SOURCE,
    TYPE_DECLARATION_HINTS,
    TYPE_RELATED_PREFIXES,
    _excerpt_for_fields,
    _field_declares_length,
    _field_view,
    _is_length_related,
    _is_rule_assembly_issue,
    _is_type_related,
    _source_declares_type,
    enforce_issue_evidence,
    metadata_to_spec,
    table_detail,
    table_needs_review,
    table_summary,
    type_basis_for,
)
from app.infra.llm import RealDesignLlm
from app.infra.llm.guard import call_llm_validated, submit_with_context
from app.kernel.run import BudgetExceededError, RunCancelledError
from app.infra.db_tables import (
    FieldCodeReferenceModel,
    FieldMetadataModel,
    MetadataRevisionModel,
    MetadataValidationModel,
    RawBlockModel,
    TableMetadataModel,
)
from app.infra.config import Settings
from app.infra.db import SessionLocal
from app.ingestion.parsers.pdf_parser import (
    _parse_type_from_entry,
    extract_code_reference_from_text,
    extract_inline_enum_values,
    lookup_term_definition,
)

METADATA_BATCH_SIZE = 3  # 疑难字段小批次：单次 AI 调用少字段，超时损失可控

_BLOCK_LOCKS_GUARD = threading.Lock()
_BLOCK_LOCKS: Dict[str, threading.RLock] = {}
_MAX_BLOCK_LOCKS = 2048


def _get_block_extract_lock(block_id: str) -> threading.RLock:
    with _BLOCK_LOCKS_GUARD:
        lock = _BLOCK_LOCKS.get(block_id)
        if lock is not None:
            return lock
        if len(_BLOCK_LOCKS) >= _MAX_BLOCK_LOCKS:
            for key in list(_BLOCK_LOCKS.keys()):
                candidate = _BLOCK_LOCKS[key]
                if candidate.acquire(blocking=False):
                    candidate.release()
                    _BLOCK_LOCKS.pop(key, None)
                    if len(_BLOCK_LOCKS) < _MAX_BLOCK_LOCKS:
                        break
        lock = threading.RLock()
        _BLOCK_LOCKS[block_id] = lock
        return lock


def _persist_draft(session: Session, block: RawBlockModel, draft: MetadataTableDraft, trace: Dict[str, Any]) -> TableMetadataModel:
    existing = (
        session.query(TableMetadataModel)
        .filter(TableMetadataModel.block_id == block.id)
        .order_by(TableMetadataModel.current_version.desc())
        .first()
    )
    if existing:
        current_fields = (
            session.query(FieldMetadataModel)
            .filter(FieldMetadataModel.table_id == existing.id)
            .order_by(FieldMetadataModel.sort_order)
            .all()
        )
        field_keys = (
            "field_cn", "field_en", "field_definition", "check_text_raw", "data_type",
            "length", "decimal_places", "required", "code_reference_text", "confidence", "evidence_json",
        )
        current_values = [{key: getattr(field, key) for key in field_keys} for field in current_fields]
        incoming_values = [field.model_dump() for field in draft.fields]
        if (
            existing.table_code == draft.table_code
            and existing.table_name == draft.table_name
            and existing.table_definition == draft.table_definition
            and current_values == incoming_values
        ):
            return existing

    preserve_existing = False
    if existing:
        preserve_existing = existing.status == "PUBLISHED" or bool(
            session.query(MetadataRevisionModel)
            .filter(
                MetadataRevisionModel.table_id == existing.id,
                MetadataRevisionModel.revision_kind.in_(["HUMAN_EDIT", "HUMAN_ADD", "HUMAN_DELETE"]),
            )
            .first()
        ) or bool(
            session.query(FieldCodeReferenceModel)
            .join(FieldMetadataModel, FieldCodeReferenceModel.field_id == FieldMetadataModel.id)
            .filter(FieldMetadataModel.table_id == existing.id)
            .first()
        )

    safe_code = (draft.table_code or "").strip()[:96]
    safe_name = (draft.table_name or "").strip()[:255]
    if existing and not preserve_existing:
        for field in session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == existing.id).all():
            session.delete(field)
        session.flush()
        table = existing
        table.table_code = safe_code
        table.table_name = safe_name
        table.table_definition = draft.table_definition
        table.current_version += 1
    else:
        next_ver = (existing.current_version + 1) if existing else 1
        table = TableMetadataModel(
            id=str(uuid.uuid4()),
            document_id=block.document_id,
            block_id=block.id,
            table_code=safe_code,
            table_name=safe_name,
            table_definition=draft.table_definition,
            status="DRAFT",
            current_version=next_ver,
            # 新版本行沿用原表在原文中的位置，否则列表里会掉到最前面
            source_ordinal=(
                existing.source_ordinal
                if existing and existing.source_ordinal is not None
                else block.ordinal
            ),
        )
        try:
            with session.begin_nested():
                session.add(table)
                session.flush()
        except IntegrityError:
            concurrent = (
                session.query(TableMetadataModel)
                .filter(TableMetadataModel.block_id == block.id)
                .order_by(TableMetadataModel.current_version.desc())
                .first()
            )
            if not concurrent:
                raise
            return concurrent

    for index, field in enumerate(draft.fields):
        values = field.model_dump()
        values["field_cn"] = str(values.get("field_cn") or "").strip()[:255]
        values["field_en"] = str(values.get("field_en") or "").strip()[:128]
        if not values["evidence_json"]:
            values["evidence_json"] = block.source_locator
        session.add(
            FieldMetadataModel(
                id=str(uuid.uuid4()),
                table_id=table.id,
                sort_order=index,
                ai_value_json=values,
                type_basis=type_basis_for(block.raw_content, values["field_en"]),
                **values,
            )
        )
    session.add(
        MetadataRevisionModel(
            id=str(uuid.uuid4()),
            table_id=table.id,
            field_id=None,
            revision_kind="AI_EXTRACTION",
            old_value_json=None,
            new_value_json={"draft": draft.model_dump(), "trace": trace},
            editor="AI",
            reason="Batch metadata assignment",
        )
    )
    session.flush()
    return table


_TABULAR_COL_ALIASES = {
    "field_cn": ("字段中文名", "中文名", "数据项名称", "数据项中文名", "字段名称", "指标名称", "中文名称", "内容"),
    "field_en": ("字段英文名", "英文名", "字段名", "字段代码", "数据项代码", "英文代码", "field_en", "name"),
    "data_type": ("数据类型", "类型（长度）", "类型(长度)", "字段类型", "类型", "格式", "data_type"),
    "length": ("数据长度", "字段长度", "长度", "length"),
    "decimal_places": ("小数位", "小数位数", "精度", "decimal_places"),
    "required": ("是否必填", "必填", "是否可空", "可空", "required"),
    "check_text": ("字段校验", "校验要求", "校验规则", "校验", "业务口径", "说明", "填报说明", "备注", "check_text"),
}


def _map_tabular_row(row: List[str], headers: List[str]) -> Dict[str, str]:
    """按 Word/Excel 表头别名动态映射各列，避免 4+/5+ 列规范丢列或把序号当字段名。"""
    norm_headers = [re.sub(r"\s+", "", str(h or "")).lower() for h in headers]
    col_idx: Dict[str, int] = {}
    for role, aliases in _TABULAR_COL_ALIASES.items():
        for idx, h in enumerate(norm_headers):
            if idx in col_idx.values():
                continue
            if any(h == a.lower() for a in aliases):
                col_idx[role] = idx
                break
        if role not in col_idx:
            for idx, h in enumerate(norm_headers):
                if idx in col_idx.values():
                    continue
                if any(a.lower() in h for a in aliases):
                    col_idx[role] = idx
                    break

    def _get(role: str, fallback_idx: int = -1) -> str:
        idx = col_idx.get(role, fallback_idx)
        return row[idx] if 0 <= idx < len(row) else ""

    # 无表头命中时保留原 3 列位置回退（[field_cn, field_en, check_text]）
    if "field_cn" not in col_idx and "field_en" not in col_idx:
        return {
            "field_cn": row[0] if len(row) > 0 else "",
            "field_en": row[1] if len(row) > 1 else "",
            "data_type_raw": "",
            "length_raw": "",
            "dec_raw": "",
            "required_raw": "",
            "check_text": row[2] if len(row) > 2 else "",
        }
    return {
        "field_cn": _get("field_cn"),
        "field_en": _get("field_en"),
        "data_type_raw": _get("data_type"),
        "length_raw": _get("length"),
        "dec_raw": _get("decimal_places"),
        "required_raw": _get("required"),
        "check_text": _get("check_text"),
    }


def _parse_tabular_type(
    type_raw: str,
    length_raw: str,
    dec_raw: str,
    check_text: str,
    field_cn: str,
):
    """兼容 SQL/EAST 类型（VARCHAR(40)/DECIMAL(20,2)/C..30/DATE）与外管局中文类型。"""
    source = (type_raw or "").strip()
    if source:
        upper = source.upper()
        m_sql = re.match(r"^(VARCHAR2?|CHAR|NUMBER|DECIMAL|NUMERIC|INT|INTEGER)\s*(?:[(（]\s*(\d+)(?:\s*[,，.]\s*(\d+))?\s*[)）])?$", upper)
        if m_sql:
            kind, g_len, g_dec = m_sql.group(1), m_sql.group(2), m_sql.group(3)
            is_num = kind in {"NUMBER", "DECIMAL", "NUMERIC", "INT", "INTEGER"}
            data_type = "NUMBER" if is_num else "VARCHAR"
            length = int(g_len or length_raw or (10 if "INT" in kind else (22 if is_num else 128)))
            dec = int(g_dec or dec_raw or 0) if is_num else 0
            return data_type, max(1, length), max(0, dec), []
        m_c = re.fullmatch(r"C\.{0,2}(\d+)", upper)
        if m_c:
            return "VARCHAR", int(m_c.group(1)), 0, []
        m_n = re.fullmatch(r"N\.{0,2}(\d+)(?:[.,](\d+))?", upper)
        if m_n:
            return "NUMBER", int(m_n.group(1)), int(m_n.group(2) or 0), []
        if upper in {"DATE", "TIMESTAMP"}:
            return "VARCHAR", 8 if upper == "DATE" else 19, 0, []
    combined = f"{source}{('，' + length_raw) if length_raw else ''}{('.' + dec_raw) if dec_raw else ''} {check_text}".strip()
    _, data_type, length, parsed_dec, _, type_warnings = _parse_type_from_entry(combined, field_cn)
    return data_type, max(1, int(length or 128)), max(0, int(parsed_dec or 0)), type_warnings


def _field_needs_ai(raw_field: Dict[str, Any]) -> bool:
    """判断字段是否需要 AI 补全：要素齐全的结构化字段走确定性提取即可。

    疑难信号：缺英文名/中文名、类型缺失或无法识别、行解析带类型告警。
    """
    if "name" in raw_field:
        if not str(raw_field.get("name") or "").strip():
            return True
        if not str(raw_field.get("content") or "").strip():
            return True
        if str(raw_field.get("data_type", "")).upper() not in ("VARCHAR", "NUMBER"):
            return True
        if not raw_field.get("length"):
            return True
        return False
    if "row" in raw_field:
        row = [str(cell or "").strip() for cell in raw_field["row"]]
        headers = [str(h or "").strip() for h in raw_field.get("headers", [])]
        mapped = _map_tabular_row(row, headers)
        field_cn = mapped["field_cn"]
        field_en = mapped["field_en"]
        # 英文名缺失时确定性路径只能编造哈希名，交给 AI 从上下文推断
        if not field_en or not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", field_en):
            return True
        data_type, _length, _dec, type_warnings = _parse_tabular_type(
            mapped["data_type_raw"],
            mapped["length_raw"],
            mapped["dec_raw"],
            mapped["check_text"],
            field_cn,
        )
        if type_warnings or not data_type:
            return True
        return False
    return True


def _deterministic_field_draft(
    raw_field: Dict[str, Any],
    terms_text: str,
    locator: Dict[str, Any],
    fallback_reason: str = "",
) -> MetadataFieldDraft:
    # Support both structured spec field dicts and DOCX/XLSX table rows
    required_col_hint = False
    if "name" in raw_field:
        field_en = str(raw_field["name"]).strip()
        field_cn = str(raw_field.get("content") or field_en).strip()
        check_text = str(raw_field.get("check_text") or "").strip()
        data_type = "NUMBER" if str(raw_field.get("data_type", "VARCHAR")).upper() == "NUMBER" else "VARCHAR"
        length = int(raw_field.get("length") or 128)
        dec = int(raw_field.get("decimal_places") or 0)
        page = raw_field.get("evidence_page") or locator.get("page", "")
        type_warnings = []
    elif "row" in raw_field:
        row = [str(c or "").strip() for c in raw_field["row"]]
        headers = [str(h or "").strip() for h in raw_field.get("headers", [])]
        mapped = _map_tabular_row(row, headers)
        field_cn = mapped["field_cn"] or mapped["field_en"] or "字段"
        raw_en = mapped["field_en"]
        field_en = (
            raw_en
            if raw_en and re.match(r"^[A-Za-z][A-Za-z0-9_]*$", raw_en)
            else f"FIELD_{hashlib.sha1(field_cn.encode('utf-8')).hexdigest()[:8].upper()}"
        )
        check_text = mapped["check_text"]
        req_text = mapped["required_raw"]
        if req_text:
            required_col_hint = req_text in {"是", "必填", "Y", "YES", "TRUE", "1", "非空", "否(不可空)"}
        data_type, length, dec, type_warnings = _parse_tabular_type(
            mapped["data_type_raw"],
            mapped["length_raw"],
            mapped["dec_raw"],
            check_text,
            field_cn,
        )
        page = locator.get("table_index", locator.get("sheet", ""))
    else:
        raise ValueError("Cannot deterministically map unstructured raw_field without name or row")

    # Find term definition in terms_text if available (strictly scoped to this field)
    field_def = field_cn
    term_definition = lookup_term_definition(terms_text, field_cn)
    if term_definition:
        field_def = term_definition[:500]

    required = (
        required_col_hint
        or bool(raw_field.get("required_basis"))
        or ("必填" in check_text and "非必填" not in check_text and "选填" not in check_text)
    )
    code_ref = extract_code_reference_from_text(check_text) or extract_code_reference_from_text(term_definition)
    if not code_ref:
        inline_enums = extract_inline_enum_values(check_text)
        if inline_enums:
            code_ref = f"{field_cn}（{field_en} 内联枚举）"

    evidence = {"page": page, "source_locator": locator, "raw_field": raw_field}
    if fallback_reason:
        evidence["fallback_reason"] = fallback_reason
    if "row" in raw_field and type_warnings:
        evidence["parse_warnings"] = type_warnings

    return MetadataFieldDraft(
        field_cn=field_cn,
        field_en=field_en,
        field_definition=field_def,
        check_text_raw=check_text,
        data_type=data_type,
        length=max(1, length, dec),
        decimal_places=max(0, dec),
        required=required,
        code_reference_text=code_ref,
        confidence=0.5 if fallback_reason else (0.65 if "row" in raw_field and type_warnings else 0.95),
        evidence_json=evidence,
    )


def _apply_source_definitions(
    fields: List[MetadataFieldDraft],
    chunk: List[Dict[str, Any]],
    terms_text: str,
) -> None:
    """术语解释表里有的字段，定义一律取原文，不用模型转述。

    模型拿到的是带折行的原始文本，它按自己的理解把两列拼回去，很容易把上一行的
    名称残片写进定义里（"…不产生新的人 存款编号 民币结构性存款编号…"）。术语表已
    经在解析阶段重排成"名称\\t定义"，这里直接覆盖，让定义始终可溯源。
    """
    if not terms_text:
        return
    label_by_en = {
        str(item.get("name") or "").strip().upper(): str(item.get("content") or "").strip()
        for item in chunk
    }
    for field in fields:
        spec_cn = label_by_en.get((field.field_en or "").strip().upper(), "")
        definition = lookup_term_definition(terms_text, spec_cn) or lookup_term_definition(terms_text, field.field_cn)
        if not definition:
            continue
        if spec_cn and len(spec_cn) > len(field.field_cn or ""):
            field.field_cn = spec_cn
        field.field_definition = definition[:500]


def extract_metadata_for_block(block_id: str, settings: Settings, auto_link: bool = True) -> str:
    with _get_block_extract_lock(block_id):
        return _extract_metadata_for_block_locked(block_id, settings, auto_link=auto_link)


def _extract_metadata_for_block_locked(block_id: str, settings: Settings, auto_link: bool = True) -> str:
    with SessionLocal() as session:
        block = session.get(RawBlockModel, block_id)
        if not block:
            raise ValueError("Raw block not found")
        source_spec = block.content_json.get("spec") or block.content_json.get("parent_spec") or {}
        terms_text = source_spec.get("terms_text", "")
        raw_fields = source_spec.get("fields")
        if raw_fields is None:
            rows = block.content_json.get("rows", [])
            raw_fields = [{"row": row, "headers": block.content_json.get("headers", [])} for row in rows[1:] if any(row)]
        if not raw_fields:
            raw_fields = [{"raw_text": block.raw_content}]
        fallback_code = source_spec.get("table_code") or block.content_json.get("table_code") or f"TABLE_{block.id.replace('-', '')[:12].upper()}"
        fallback_name = source_spec.get("table_name") or block.content_json.get("table_name") or block.source_locator.get("sheet") or f"原始表格 {block.ordinal + 1}"

        try:
            llm = RealDesignLlm(settings)
        except Exception:
            llm = None

        # 确定性提取先行：spec_parser 已结构化、字段要素齐全的字段毫秒级落库，
        # 只有缺类型/缺英文名/有解析告警的疑难字段才小批次调 AI，避免整批 120s 白等超时。
        indexed_fields = list(enumerate(raw_fields))
        clean_items = [item for item in indexed_fields if not _field_needs_ai(item[1])]
        suspect_items = [item for item in indexed_fields if _field_needs_ai(item[1])]
        clean_drafts = {
            index: _deterministic_field_draft(raw_field, terms_text, block.source_locator)
            for index, raw_field in clean_items
        }

        # 疑难字段小批次调 AI，失败的批次退回确定性提取
        chunks = [suspect_items[i : i + METADATA_BATCH_SIZE] for i in range(0, len(suspect_items), METADATA_BATCH_SIZE)]

        def bind_chunk(chunk_items):
            chunk = [raw_field for _, raw_field in chunk_items]
            is_unstructured = len(chunk) == 1 and "raw_text" in chunk[0]
            if llm is not None:
                payload = {
                    "source_locator": block.source_locator,
                    "table": {"table_code": fallback_code, "table_name": fallback_name},
                    "raw_fields": chunk,
                    "raw_context": (
                        (block.raw_content or "")[:CONTEXT_MAX_CHARS]
                        if is_unstructured
                        else _excerpt_for_fields(
                            block.raw_content,
                            [item.get("name") or item.get("field_en") or "" for item in chunk],
                        )
                    ),
                }
                def _validate_meta_batch(args):
                    candidate = {
                        "table_code": fallback_code,
                        "table_name": fallback_name,
                        "table_definition": args.get("table_definition", ""),
                        "fields": args.get("fields", []),
                    }
                    draft = MetadataTableDraft.model_validate(candidate)
                    if is_unstructured:
                        if not draft.fields:
                            raise ValueError("Unstructured block LLM response returned zero fields")
                    elif len(draft.fields) != len(chunk):
                        raise ValueError(
                            f"Field count mismatch in batch LLM response: expected {len(chunk)}, got {len(draft.fields)}"
                        )
                    require_valid_metadata(draft)
                    return draft

                instruction = (
                    "从输入非结构化原文块中提取该表的全部业务字段定义（字段英文名、中文名、类型、长度、小数位、是否必填、校验原文、码值引用等）。"
                    "不得遗漏原文中的字段，也不得虚构原文未出现的字段；定义与长度仅在有明确依据时填写。"
                    if is_unstructured
                    else f"批量装配输入的 {len(chunk)} 个字段。字段英文名、中文名、类型和校验原文为最低必备信息；定义、长度、码值引用等仅在有明确依据时填写，其他项可省略，不得虚构。"
                )

                try:
                    draft, trace = call_llm_validated(
                        llm,
                        "assign_metadata",
                        instruction,
                        payload,
                        _validate_meta_batch,
                        timeout=120,
                        backoff=(1.0,),
                        transient_attempts=2,
                        validation_attempts=2,
                    )
                    _apply_source_definitions(draft.fields, chunk, terms_text)
                    return draft.fields, draft.table_definition, trace
                except (BudgetExceededError, RunCancelledError):
                    raise
                except Exception as exc:
                    err_msg = f"{type(exc).__name__}: {exc}"
            else:
                err_msg = ""

            # Deterministic fast-path / fallback mapping
            mapped_fields = [
                _deterministic_field_draft(rf, terms_text, block.source_locator, fallback_reason=err_msg)
                for rf in chunk
            ]
            table_def = f"{fallback_name}（共 {len(raw_fields)} 个字段，结构化提取）"
            return mapped_fields, table_def, {
                "strategy": "DETERMINISTIC_BATCH_MAPPING",
                "field_count": len(mapped_fields),
                **({"fallback_from": err_msg} if err_msg else {}),
            }

        if chunks:
            with ThreadPoolExecutor(max_workers=max(1, min(2, len(chunks)))) as executor:
                futures = [submit_with_context(executor, bind_chunk, chunk_items) for chunk_items in chunks]
                chunk_results = []
                for future in futures:
                    try:
                        chunk_results.append(future.result())
                    except (BudgetExceededError, RunCancelledError):
                        for f in futures:
                            f.cancel()
                        raise
        else:
            chunk_results = []

        # 按原始字段顺序合并：确定性结果 + AI/兜底结果（支持非结构化 raw_text 展开为多字段）
        fields_by_index: Dict[int, List[MetadataFieldDraft]] = {
            index: [draft_field] for index, draft_field in clean_drafts.items()
        }
        table_definition = ""
        batch_traces = []
        if clean_drafts:
            batch_traces.append({"strategy": "DETERMINISTIC_FIRST", "field_count": len(clean_drafts)})
        for chunk_items, (fields_part, t_def, tr) in zip(chunks, chunk_results):
            if len(chunk_items) == 1 and "raw_text" in chunk_items[0][1]:
                fields_by_index[chunk_items[0][0]] = list(fields_part)
            else:
                for (index, _), draft_field in zip(chunk_items, fields_part):
                    fields_by_index[index] = [draft_field]
            if t_def and not table_definition:
                table_definition = t_def
            batch_traces.append(tr)
        all_fields: List[MetadataFieldDraft] = [
            draft_field
            for index in range(len(raw_fields))
            for draft_field in fields_by_index.get(index, [])
        ]

        # 只有真调过 AI 赋值的批次才值得再做一次 AI 复核；全确定性提取走交叉核验即可
        ai_assignment_used = any(not str(trace.get("strategy", "")).startswith("DETERMINISTIC") for trace in batch_traces)
        assignment_fallback = bool(suspect_items) and not ai_assignment_used
        draft = MetadataTableDraft(
            table_code=fallback_code,
            table_name=fallback_name,
            table_definition=table_definition or f"{fallback_name} 结构化元数据表",
            fields=all_fields,
        )
        require_valid_metadata(draft)
        trace = {
            "provider": settings.llm_provider if llm else "DETERMINISTIC",
            "model": settings.llm_model if llm else "rule-engine",
            "batch_count": len(chunk_results),
            "batches": batch_traces,
            "arguments": draft.model_dump(),
        }
        table = _persist_draft(session, block, draft, trace)
        program_issues = validate_metadata_draft(draft)
        if block.content_json.get("human_required"):
            program_issues.append(
                {
                    "severity": "WARNING",
                    "code": "SOURCE_PARSE_REVIEW_REQUIRED",
                    "field_en": "",
                    "message": "；".join(block.content_json.get("parse_warnings") or ["来源块需要人工确认"]),
                }
            )
        session.add(
            MetadataValidationModel(
                id=str(uuid.uuid4()),
                table_id=table.id,
                validator_kind="PROGRAM",
                round=1,
                status="FAILED" if any(item["severity"] == "ERROR" for item in program_issues) else "PASSED",
                issues_json=program_issues,
            )
        )

        # Differential Review Stage (using review_metadata tool or deterministic cross-check)
        issues: List[Dict[str, Any]] = []
        if llm is not None and ai_assignment_used:
            try:
                args, review_trace = call_llm_validated(
                    llm,
                    "review_metadata",
                    (
                        "独立复核整表字段元数据，只依据 raw_context 判断，仅输出存在遗漏、类型长度错配、"
                        "无依据必填或虚构码值的问题。硬性要求："
                        "①每条问题必须给出 evidence，逐字摘录 raw_context 中支持该判断的原文；"
                        "②raw_context 没有声明数据类型/长度时，不得判定类型或长度错配为 ERROR，"
                        "只能报 WARNING 并把 evidence 填 NO_SOURCE；"
                        "③不要用业务常识（如\"金额项应为数值型\"）替代原文声明，此类推断不得报 ERROR；"
                        "④原文用①②③列举取值时，按序号 1..N 编码是允许的，不要判为编造码值；"
                        "⑤本条只审字段元数据本身，不要评价校验规则/条件算子是否已装配（那是后续阶段）；"
                        "⑥没有问题就返回空数组。"
                    ),
                    {
                        "source_locator": block.source_locator,
                        "table": {"table_code": draft.table_code, "table_name": draft.table_name},
                        "fields": [f.model_dump() for f in draft.fields],
                        "raw_context": _excerpt_for_fields(
                            block.raw_content, [f.field_en for f in draft.fields]
                        ),
                    },
                    timeout=60,
                    backoff=(1.0,),
                    transient_attempts=2,
                    validation_attempts=2,
                )
                issues = args.get("issues", [])
            except (BudgetExceededError, RunCancelledError):
                raise
            except Exception as exc:
                review_trace = {"error": f"{type(exc).__name__}: {exc}"}
                issues = [
                    {
                        "severity": "WARNING",
                        "code": "AI_REVIEW_FALLBACK",
                        "field_en": "",
                        "message": f"在线 AI 复核跳过，已使用确定性规则交叉核验：{review_trace['error']}",
                    }
                ]
        elif assignment_fallback:
            review_trace = {"strategy": "DETERMINISTIC_CROSS_CHECK", "skipped_reason": "AI_ASSIGNMENT_UNAVAILABLE"}
            issues = [{
                "severity": "WARNING",
                "code": "AI_REVIEW_UNAVAILABLE",
                "field_en": "",
                "message": "AI 字段赋值不可用，已使用程序提取并交叉检查；无需再等待一次 AI 复核。",
            }]
        else:
            review_trace = {"strategy": "DETERMINISTIC_CROSS_CHECK", "checked_fields": len(draft.fields)}

        issues = enforce_issue_evidence(issues, block.raw_content)
        session.add(
            MetadataValidationModel(
                id=str(uuid.uuid4()),
                table_id=table.id,
                validator_kind="AI_REVIEW",
                round=1,
                status="FAILED" if any(item.get("severity") == "ERROR" for item in issues) else "PASSED",
                issues_json=issues,
            )
        )
        session.add(
            MetadataRevisionModel(
                id=str(uuid.uuid4()),
                table_id=table.id,
                field_id=None,
                revision_kind="AI_REVIEW",
                old_value_json=None,
                new_value_json={"issues": issues, "trace": review_trace},
                editor="AI_REVIEWER",
                reason="Independent batch metadata review",
            )
        )
        # 只有确实违反原文的 ERROR 才让整表进待复核；提示类问题保留在详情里，不再整表标红
        table.status = (
            "NEEDS_REVIEW"
            if table_needs_review(program_issues + issues)
            else "AI_SELF_VALIDATED"
        )
        if auto_link:
            from app.catalog.code_sets.linking import ensure_table_references

            ensure_table_references(session, table)
        session.commit()
        return table.id
