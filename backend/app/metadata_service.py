import hashlib
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .asset_models import (
    FieldCodeReferenceModel,
    FieldMetadataModel,
    MetadataRevisionModel,
    MetadataValidationModel,
    RawBlockModel,
    SourceDocumentModel,
    TableMetadataModel,
)
from .config import Settings
from .db import SessionLocal
from .domain import FieldSpec, SpecDocument
from .generic_ai import RealDesignLlm
from .metadata_domain import MetadataFieldDraft, MetadataTableDraft, require_valid_metadata, validate_metadata_draft
from .spec_parser import (
    _parse_type_from_entry,
    extract_code_reference_from_text,
    extract_inline_enum_values,
    lookup_term_definition,
)

METADATA_BATCH_SIZE = 10

# 来源原文里出现这些字样，才说明这份文档真的声明了数据类型/长度
TYPE_DECLARATION_HINTS = (
    "类型（长度）",
    "字符型",
    "数值型",
    "整数型",
    "日期型",
    "定长",
    "变长",
    "数据格式",
)

# 与类型/长度相关的问题码前缀；原文没有类型声明时，这类 ERROR 只能算推断
TYPE_RELATED_PREFIXES = (
    "TYPE_",
    "TYPE",
    "LENGTH",
    "VALUE_TYPE",
    "PRECISION",
    "DECIMAL",
    "UNVERIFIED_LENGTH",
    "UNDOCUMENTED_LENGTH",
)


CONTEXT_BEFORE = 300
CONTEXT_AFTER = 2200
CONTEXT_MAX_CHARS = 14000


def _excerpt_for_fields(raw: str, field_names: List[str], *, max_chars: int = CONTEXT_MAX_CHARS) -> str:
    """按字段在原文中的位置截上下文，而不是固定取前 N 个字。

    一个块两万多字时，"取前 6000 字"会让后半段的字段在复核时完全看不到自己的原文，
    模型只能凭字段名推断——"evidence 在原文里找不到"的误降级就是这么来的。
    """
    text = raw or ""
    if len(text) <= max_chars:
        return text
    spans = []
    for name in field_names:
        name = str(name or "").strip()
        if len(name) < 2:
            continue
        for match in re.finditer(re.escape(name), text):
            start = max(0, match.start() - CONTEXT_BEFORE)
            end = min(len(text), match.start() + CONTEXT_AFTER)
            spans.append((start, end))
    if not spans:
        return text[:max_chars]
    spans.sort()
    merged: List[tuple] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    parts: List[str] = []
    used = 0
    for start, end in merged:
        chunk = text[start:end]
        if used + len(chunk) > max_chars:
            remain = max_chars - used
            if remain < 500:
                break
            chunk = chunk[:remain]
        parts.append(chunk)
        used += len(chunk)
        if used >= max_chars:
            break
    return "\n…\n".join(parts)


def _source_declares_type(raw_context: str) -> bool:
    text = raw_context or ""
    return any(hint in text for hint in TYPE_DECLARATION_HINTS)


# "校验规则有没有装配好"是后续阶段的职责，阶段 1 的字段复核不该用它阻断整张表
RULE_ASSEMBLY_KEYWORDS = (
    "OPERATOR",
    "COND_REQUIRED",
    "MISSING_CONDITIONAL_REQUIRED",
    "MISSING_CONDITIONAL",
    "FOREIGN_KEY",
    "RULE_NOT_ASSEMBLED",
)


def _is_rule_assembly_issue(code: str) -> bool:
    upper = (code or "").upper()
    return any(keyword in upper for keyword in RULE_ASSEMBLY_KEYWORDS)


def _field_declares_length(raw_context: str, field_en: str) -> Optional[bool]:
    """判断原文对该字段是否给出了明确的长度。

    True = 写了"数值型，13.8"这类完整声明；False = 只写了"数值型"没给长度；
    None = 原文里找不到该字段的类型声明。
    """
    if not field_en:
        return None
    type_pattern = r"(定长字符型|变长字符型|字符型|数值型|日期型|整数型|长整数型|时间型)"
    lines = (raw_context or "").split("\n")
    found_type = False
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith(field_en):
            continue
        if re.search(type_pattern + r"\s*[，,]\s*\d+", stripped):
            return True
        if re.search(type_pattern, stripped):
            found_type = True
            # 长度可能被折到下一行，但下一行必须是纯续行（不是新字段）才算
            nxt = lines[index + 1] if index + 1 < len(lines) else ""
            if nxt and not re.match(r"^\s*[A-Z][A-Z0-9_]{2,}\s", nxt) and re.search(
                r"^\s*[，,]?\s*\d{1,3}(\.\d+)?", nxt
            ):
                return True
    return False if found_type else None


def _is_length_related(code: str) -> bool:
    upper = (code or "").upper()
    return any(key in upper for key in ("LENGTH", "PRECISION", "DECIMAL"))


def _is_type_related(code: str) -> bool:
    upper = (code or "").upper()
    return any(upper.startswith(prefix) for prefix in TYPE_RELATED_PREFIXES)


def enforce_issue_evidence(
    issues: List[Dict[str, Any]],
    raw_context: str,
    *,
    downgrade_unverified: bool = True,
) -> List[Dict[str, Any]]:
    """把"没有原文依据的 ERROR"降级成提示。

    复核模型很容易把业务常识当成原文要求（例如依据"金额项"推断 NUMBER(22,2)）。
    来源文档如果没有声明过类型/长度，这类判断就不该阻断整张表；同理，模型给出的
    evidence 片段如果在原文里找不到，也不该算数。
    """
    declares = _source_declares_type(raw_context)
    compact_source = re.sub(r"\s+", "", raw_context or "")
    result: List[Dict[str, Any]] = []
    for issue in issues:
        item = dict(issue)
        code = str(item.get("code") or "AI_REVIEW")
        severity = str(item.get("severity") or "WARNING").upper()
        message = str(item.get("message") or "")
        evidence = str(item.get("evidence") or "").strip()
        reason = ""
        if severity == "ERROR" and downgrade_unverified:
            if not declares and _is_type_related(code):
                reason = "来源原文未声明数据类型或长度，该判断属推断"
            elif _is_length_related(code) and _field_declares_length(raw_context, str(item.get("field_en") or "")) is False:
                reason = "来源原文只写了类型、未给出长度，长度判断属推断"
            elif _is_rule_assembly_issue(code):
                reason = "校验规则装配属后续阶段，不作为字段元数据的阻断项"
            elif evidence and evidence.upper() not in {"NO_SOURCE", "无", "N/A"}:
                compact_evidence = re.sub(r"\s+", "", evidence)
                if compact_evidence and compact_evidence not in compact_source:
                    reason = "所引原文片段未在来源中找到"
        if reason:
            severity = "WARNING"
            code = f"UNVERIFIED_{code}"[:64]
            message = f"{message}（{reason}，已降级为提示）"
        item["severity"] = severity
        item["code"] = code
        item["message"] = message
        result.append(item)
    return result


def table_needs_review(issues: List[Dict[str, Any]]) -> bool:
    """只有 ERROR 才让整张表进入待复核；WARNING 是提示，保留在详情里但不阻断。"""
    return any(str(item.get("severity") or "").upper() == "ERROR" for item in issues)


TYPE_BASIS_SOURCE = "SOURCE"
TYPE_BASIS_INFERRED = "INFERRED"
TYPE_BASIS_MANUAL = "MANUAL"


def type_basis_for(raw_context: str, field_en: str) -> str:
    """字段类型是原文写的，还是我们推断的。

    SOURCE = 原文给了类型声明（哪怕没给长度）；INFERRED = 原文没提，类型是兜底猜的。
    两者的可信度差一个量级，界面上必须能分开看。
    """
    return TYPE_BASIS_INFERRED if _field_declares_length(raw_context, field_en) is None else TYPE_BASIS_SOURCE


def _field_view(field: FieldMetadataModel) -> Dict[str, Any]:
    return {
        "id": field.id,
        "field_cn": field.field_cn,
        "field_en": field.field_en,
        "field_definition": field.field_definition,
        "check_text_raw": field.check_text_raw,
        "data_type": field.data_type,
        "length": field.length,
        "decimal_places": field.decimal_places,
        "required": field.required,
        "code_reference_text": field.code_reference_text,
        "confidence": field.confidence,
        "type_basis": (field.type_basis or "UNKNOWN"),
        "evidence_json": field.evidence_json,
        "sort_order": field.sort_order,
        "ai_value_json": field.ai_value_json,
    }


def table_summary(row: TableMetadataModel) -> Dict[str, Any]:
    return {
        "id": row.id,
        "document_id": row.document_id,
        "block_id": row.block_id,
        "table_code": row.table_code,
        "table_name": row.table_name,
        "table_definition": row.table_definition,
        "status": row.status,
        "current_version": row.current_version,
        "published_version": row.published_version,
        "updated_at": row.updated_at.isoformat(),
    }


def table_detail(session: Session, table: TableMetadataModel) -> Dict[str, Any]:
    fields = (
        session.query(FieldMetadataModel)
        .filter(FieldMetadataModel.table_id == table.id)
        .order_by(FieldMetadataModel.sort_order)
        .all()
    )
    validations = (
        session.query(MetadataValidationModel)
        .filter(MetadataValidationModel.table_id == table.id)
        .order_by(MetadataValidationModel.created_at)
        .all()
    )
    revisions = (
        session.query(MetadataRevisionModel)
        .filter(MetadataRevisionModel.table_id == table.id)
        .order_by(MetadataRevisionModel.created_at.desc())
        .limit(30)
        .all()
    )
    refs = {
        ref.field_id: ref.code_set_id
        for ref in session.query(FieldCodeReferenceModel)
        .filter(FieldCodeReferenceModel.field_id.in_([field.id for field in fields]))
        .all()
    }
    return {
        **table_summary(table),
        "fields": [{**_field_view(field), "code_set_id": refs.get(field.id)} for field in fields],
        "validations": [
            {
                "validator_kind": item.validator_kind,
                "round": item.round,
                "status": item.status,
                "issues": item.issues_json,
                "created_at": item.created_at.isoformat(),
            }
            for item in validations
        ],
        "revisions": [
            {
                "revision_kind": item.revision_kind,
                "field_id": item.field_id,
                "old_value": item.old_value_json,
                "new_value": item.new_value_json,
                "editor": item.editor,
                "reason": item.reason,
                "created_at": item.created_at.isoformat(),
            }
            for item in revisions
        ],
    }


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

    if existing and not preserve_existing:
        for field in session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == existing.id).all():
            session.delete(field)
        session.flush()
        table = existing
        table.table_code = draft.table_code
        table.table_name = draft.table_name
        table.table_definition = draft.table_definition
        table.current_version += 1
    else:
        next_ver = (existing.current_version + 1) if existing else 1
        table = TableMetadataModel(
            id=str(uuid.uuid4()),
            document_id=block.document_id,
            block_id=block.id,
            table_code=draft.table_code,
            table_name=draft.table_name,
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
        session.add(table)
        session.flush()

    for index, field in enumerate(draft.fields):
        values = field.model_dump()
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


def _deterministic_field_draft(
    raw_field: Dict[str, Any],
    terms_text: str,
    locator: Dict[str, Any],
    fallback_reason: str = "",
) -> MetadataFieldDraft:
    # Support both structured spec field dicts and DOCX/XLSX table rows
    if "name" in raw_field:
        field_en = str(raw_field["name"]).strip()
        field_cn = str(raw_field.get("content") or field_en).strip()
        check_text = str(raw_field.get("check_text") or "").strip()
        data_type = "NUMBER" if str(raw_field.get("data_type", "VARCHAR")).upper() == "NUMBER" else "VARCHAR"
        length = int(raw_field.get("length") or 128)
        dec = int(raw_field.get("decimal_places") or 0)
        page = raw_field.get("evidence_page") or locator.get("page", "")
    elif "row" in raw_field:
        row = [str(c or "").strip() for c in raw_field["row"]]
        headers = [str(h or "").strip() for h in raw_field.get("headers", [])]
        field_cn = row[0] if len(row) > 0 else "字段"
        field_en = row[1] if len(row) > 1 and re.match(r"^[A-Za-z][A-Za-z0-9_]*$", row[1]) else f"FIELD_{hashlib.sha1(field_cn.encode('utf-8')).hexdigest()[:8].upper()}"
        check_text = row[2] if len(row) > 2 else ""
        _, data_type, length, parsed_dec, _, type_warnings = _parse_type_from_entry(check_text, field_cn)
        dec = parsed_dec or 0
        page = locator.get("table_index", locator.get("sheet", ""))
    else:
        raise ValueError("Cannot deterministically map unstructured raw_field without name or row")

    # Find term definition in terms_text if available
    field_def = field_cn
    term_definition = lookup_term_definition(terms_text, field_cn)
    if term_definition:
        field_def = term_definition[:500]
    elif terms_text and field_cn:
        idx = terms_text.find(field_cn)
        if idx >= 0:
            snippet = re.sub(r"\s+", " ", terms_text[idx : idx + 240]).strip()
            field_def = snippet[:180]

    required = bool(raw_field.get("required_basis")) or ("必填" in check_text and "非必填" not in check_text and "选填" not in check_text)
    code_ref = extract_code_reference_from_text(check_text) or extract_code_reference_from_text(field_def)
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
        fallback_name = source_spec.get("table_name") or block.content_json.get("table_name") or f"原始表格 {block.ordinal + 1}"

        try:
            llm = RealDesignLlm(settings)
        except Exception:
            llm = None

        # Chunk raw_fields into batches (up to 10 fields per LLM call instead of 1 call per field)
        chunks = [raw_fields[i : i + METADATA_BATCH_SIZE] for i in range(0, len(raw_fields), METADATA_BATCH_SIZE)]

        def bind_chunk(chunk: List[Dict[str, Any]]):
            if llm is not None:
                payload = {
                    "source_locator": block.source_locator,
                    "table": {"table_code": fallback_code, "table_name": fallback_name},
                    "raw_fields": chunk,
                    "raw_context": _excerpt_for_fields(
                        block.raw_content,
                        [item.get("name") or item.get("field_en") or "" for item in chunk],
                    ),
                }
                try:
                    args, trace = llm.assign(
                        "assign_metadata",
                        f"批量装配输入的 {len(chunk)} 个字段。字段英文名、中文名、类型和校验原文为最低必备信息；定义、长度、码值引用等仅在有明确依据时填写，其他项可省略，不得虚构。",
                        payload,
                        timeout=120,
                    )
                    candidate = {
                        "table_code": fallback_code,
                        "table_name": fallback_name,
                        "table_definition": args.get("table_definition", ""),
                        "fields": args.get("fields", []),
                    }
                    draft = MetadataTableDraft.model_validate(candidate)
                    if len(draft.fields) == len(chunk):
                        require_valid_metadata(draft)
                        _apply_source_definitions(draft.fields, chunk, terms_text)
                        return draft.fields, draft.table_definition, trace
                except Exception as exc:
                    err_msg = f"{type(exc).__name__}: {exc}"
                else:
                    err_msg = "Field count mismatch in batch LLM response"
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

        with ThreadPoolExecutor(max_workers=min(2, len(chunks))) as executor:
            chunk_results = list(executor.map(bind_chunk, chunks))

        all_fields: List[MetadataFieldDraft] = []
        table_definition = ""
        batch_traces = []
        for fields_part, t_def, tr in chunk_results:
            all_fields.extend(fields_part)
            if t_def and not table_definition:
                table_definition = t_def
            batch_traces.append(tr)

        assignment_fallback = bool(batch_traces) and all(trace.get("fallback_from") for trace in batch_traces)
        draft = MetadataTableDraft(
            table_code=fallback_code,
            table_name=fallback_name,
            table_definition=table_definition or f"{fallback_name} 结构化元数据表",
            fields=all_fields,
        )
        require_valid_metadata(draft)
        trace = {
            "provider": "GLM" if llm else "DETERMINISTIC",
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
        if llm is not None and not assignment_fallback:
            try:
                args, review_trace = llm.assign(
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
                )
                issues = args.get("issues", [])
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
            from .code_service import ensure_table_references

            ensure_table_references(session, table)
        session.commit()
        return table.id


def update_field(
    session: Session,
    table: TableMetadataModel,
    field: FieldMetadataModel,
    changes: Dict[str, Any],
    expected_version: int,
    editor: str,
    reason: str,
) -> None:
    if table.current_version != expected_version:
        raise ValueError("VERSION_CONFLICT")
    if table.status == "PUBLISHED":
        raise ValueError("PUBLISHED_TABLE_IS_IMMUTABLE")
    allowed = {
        "field_cn",
        "field_en",
        "field_definition",
        "check_text_raw",
        "data_type",
        "length",
        "decimal_places",
        "required",
        "code_reference_text",
        "confidence",
        "type_basis",
    }
    if any(key not in allowed for key in changes):
        raise ValueError("INVALID_FIELD_CHANGE")
    if "type_basis" in changes and str(changes["type_basis"]).upper() not in {
        TYPE_BASIS_SOURCE,
        TYPE_BASIS_INFERRED,
        TYPE_BASIS_MANUAL,
    }:
        raise ValueError("INVALID_TYPE_BASIS")
    old = _field_view(field)
    for key, value in changes.items():
        setattr(field, key, value)
    # 人工改过类型/长度，就不再是"原文声明"或"AI 推断"，标成人工口径
    if {"data_type", "length", "decimal_places"} & set(changes) and "type_basis" not in changes:
        field.type_basis = TYPE_BASIS_MANUAL
    table.current_version += 1
    table.status = "DRAFT"
    session.add(
        MetadataRevisionModel(
            id=str(uuid.uuid4()),
            table_id=table.id,
            field_id=field.id,
            revision_kind="HUMAN_EDIT",
            old_value_json=old,
            new_value_json=_field_view(field),
            editor=editor,
            reason=reason,
        )
    )
    session.flush()


def remove_table(session: Session, table: TableMetadataModel) -> Dict[str, Any]:
    """删掉一张报表：字段、字段上的码值引用、历史修订一并清掉。

    历史 run 不受影响——run 存的是 spec 快照（content_json），不依赖这一行。
    码表不跟着删，它可能还被别的表引用着。
    """
    field_ids = [
        row[0]
        for row in session.query(FieldMetadataModel.id).filter(FieldMetadataModel.table_id == table.id).all()
    ]
    if field_ids:
        session.query(FieldCodeReferenceModel).filter(
            FieldCodeReferenceModel.field_id.in_(field_ids)
        ).delete(synchronize_session=False)
    session.query(MetadataRevisionModel).filter(
        MetadataRevisionModel.table_id == table.id
    ).delete(synchronize_session=False)
    session.query(FieldMetadataModel).filter(
        FieldMetadataModel.table_id == table.id
    ).delete(synchronize_session=False)
    code = table.table_code
    table_id = table.id
    session.delete(table)
    session.flush()
    return {"deleted": table_id, "table_code": code, "field_count": len(field_ids)}


def publish_table(session: Session, table: TableMetadataModel, approver: str, expected_version: int) -> Dict[str, Any]:
    """定稿一张表；顺带把它引用、且检查通过的码表一起发布。

    返回自动发布的码表清单，调用方可以据此告诉用户「替你发了什么」。
    """
    if table.current_version != expected_version:
        raise ValueError("VERSION_CONFLICT")
    fields = session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == table.id).order_by(FieldMetadataModel.sort_order).all()
    draft = MetadataTableDraft(
        table_code=table.table_code,
        table_name=table.table_name,
        table_definition=table.table_definition,
        fields=[
            {
                key: value
                for key, value in _field_view(field).items()
                if key not in {"id", "sort_order", "ai_value_json", "type_basis"}
            }
            for field in fields
        ],
    )
    require_valid_metadata(draft)
    from .code_service import ensure_table_references
    from .readiness import evaluate_table

    ensure_table_references(session, table)
    session.flush()

    # 表发布是阶段 2 的唯一入口，这里必须回答"能不能落成 DDL"；规则/码值的问题
    # 不阻断发布，但要写进发布快照，避免阶段 2 才炸。
    readiness = evaluate_table(session, table)
    if not readiness["ddl"]["ready"]:
        first = readiness["ddl"]["blockers"][0]
        raise ValueError(f"NOT_READY_FOR_DDL：{first['message']}")

    # 顺手把它引用的、检查通过的码表一起发布。
    # 码表「发布」原本是独立的人工动作，但没人会为了 165 张码表挨个点，
    # 不点的话阶段 2 的 load_published_code_sets 又直接抛错。检查不通过的
    # （空码表、码值超长）留给人补数据，那才是真需要动手的地方。
    from .code_set_readiness import auto_publish_referenced_code_sets

    auto = auto_publish_referenced_code_sets(session, table)
    if auto["published"]:
        readiness = evaluate_table(session, table)

    table.status = "PUBLISHED"
    table.published_version = table.current_version
    session.add(
        MetadataRevisionModel(
            id=str(uuid.uuid4()),
            table_id=table.id,
            field_id=None,
            revision_kind="PUBLISHED",
            old_value_json=None,
            new_value_json={
                "version": table.current_version,
                "readiness": {
                    "rule_materials_ready": readiness["rule_materials"]["ready"],
                    "code_sets_ready": readiness["code_sets"]["ready"],
                    "rule_material_blockers": [item["code"] for item in readiness["rule_materials"]["blockers"]],
                    "code_set_blockers": [item["code"] for item in readiness["code_sets"]["blockers"]],
                },
                "auto_published_code_sets": [item["code"] for item in auto["published"]],
                "code_sets_needing_work": auto["skipped"],
            },
            editor=approver,
            reason="Human published immutable metadata",
        )
    )
    session.flush()
    return auto


def metadata_to_spec(session: Session, table: TableMetadataModel) -> SpecDocument:
    if table.status != "PUBLISHED" or table.published_version != table.current_version:
        raise ValueError("TABLE_NOT_PUBLISHED")
    document = session.get(SourceDocumentModel, table.document_id)
    block = session.get(RawBlockModel, table.block_id)
    fields = session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == table.id).order_by(FieldMetadataModel.sort_order).all()
    return SpecDocument(
        version=document.spec_version,
        table_code=table.table_code,
        table_name=table.table_name,
        source_name=document.file_name,
        source_sha256=document.sha256,
        section=str(block.source_locator.get("section", table.table_code)),
        fields=[
            FieldSpec(
                name=field.field_en,
                content=field.field_cn,
                data_type=field.data_type,
                length=field.length,
                decimal_places=field.decimal_places,
                check_text=field.check_text_raw,
                code_reference=field.code_reference_text or "",
                evidence_page=str(field.evidence_json.get("page", field.evidence_json)),
            )
            for field in fields
        ],
        terms_text=block.raw_content,
        dictionary_text=block.raw_content,
    )


# ---------------------------------------------------------------------------
# 批量修正字段类型
#
# 用户拿到的规范里，类型常常整表缺失（例如统计制度只有"数值型"没给长度），
# 逐个字段点开改不现实。这里用一张表（CSV/XLSX）一次性给出口径，
# 导入的行一律标成 MANUAL——手工给的口径不该再显示成"原文声明"或"推断"。
# ---------------------------------------------------------------------------

TYPE_ALIASES = {
    "varchar": "VARCHAR",
    "字符型": "VARCHAR",
    "字符": "VARCHAR",
    "字符串": "VARCHAR",
    "文本": "VARCHAR",
    "string": "VARCHAR",
    "char": "VARCHAR",
    "number": "NUMBER",
    "numeric": "NUMBER",
    "decimal": "NUMBER",
    "数值型": "NUMBER",
    "数值": "NUMBER",
    "数字": "NUMBER",
    "整数型": "NUMBER",
}

IMPORT_HEADER_ALIASES = {
    "table_code": "table_code",
    "报表代码": "table_code",
    "表代码": "table_code",
    "表号": "table_code",
    "field_en": "field_en",
    "字段英文名": "field_en",
    "字段": "field_en",
    "英文名": "field_en",
    "字段名": "field_en",
    "data_type": "data_type",
    "目标类型": "data_type",
    "类型": "data_type",
    "数据类型": "data_type",
    "length": "length",
    "目标长度": "length",
    "长度": "length",
    "decimal_places": "decimal_places",
    "目标小数位": "decimal_places",
    "小数位": "decimal_places",
    "reason": "reason",
    "说明": "reason",
    "备注": "reason",
}


def normalize_type_name(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    return TYPE_ALIASES.get(raw.lower()) or TYPE_ALIASES.get(raw) or ""


def apply_field_type_import(
    session: Session,
    rows: List[List[str]],
    *,
    editor: str,
    reason: str = "批量导入类型修正",
    document_id: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """按表里的行修正字段类型，返回改动明细与未导入行。

    表头通过别名识别（报表代码/字段英文名/目标类型/目标长度/目标小数位/说明），
    只写需要改的列即可；找不到字段或类型写错的行整体跳过并报行号，不中断整批。
    """
    if not rows:
        raise ValueError("文件没有内容")
    header = [str(cell or "").strip() for cell in rows[0]]
    indexes = {IMPORT_HEADER_ALIASES[name]: index for index, name in enumerate(header) if name in IMPORT_HEADER_ALIASES}
    if "field_en" not in indexes:
        raise ValueError("缺少必需列：字段英文名（field_en）")
    if not {"data_type", "length", "decimal_places"} & set(indexes):
        raise ValueError("至少要有一列要改的内容：目标类型/长度/小数位")

    def cell(values: List[str], key: str) -> str:
        index = indexes.get(key)
        if index is None or index >= len(values):
            return ""
        return str(values[index] or "").strip()

    updated: List[Dict[str, Any]] = []
    unchanged: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    tables = {
        table.table_code.upper(): table
        for table in session.query(TableMetadataModel).all()
        if not document_id or table.document_id == document_id
    }
    versions: Dict[str, int] = {}
    for line_no, values in enumerate(rows[1:], start=2):
        if not any(str(item or "").strip() for item in values):
            continue
        field_en = cell(values, "field_en").upper()
        table_code = cell(values, "table_code").upper()
        if not field_en:
            errors.append({"line": line_no, "message": "缺少字段英文名"})
            continue
        raw_type = cell(values, "data_type")
        target_type = normalize_type_name(raw_type)
        if raw_type and not target_type:
            errors.append({"line": line_no, "message": f"无法识别的类型：{raw_type}"})
            continue
        try:
            target_length = int(cell(values, "length")) if cell(values, "length") else None
            target_dec = int(cell(values, "decimal_places")) if cell(values, "decimal_places") else None
        except ValueError:
            errors.append({"line": line_no, "message": "长度/小数位必须是整数"})
            continue

        candidates = [tables[table_code]] if table_code and table_code in tables else list(tables.values())
        field = None
        table = None
        for candidate in candidates:
            field = (
                session.query(FieldMetadataModel)
                .filter(FieldMetadataModel.table_id == candidate.id, FieldMetadataModel.field_en == field_en)
                .first()
            )
            if field:
                table = candidate
                break
        if not field or not table:
            errors.append(
                {
                    "line": line_no,
                    "message": f"找不到字段 {field_en}" + (f"（报表 {table_code}）" if table_code else ""),
                }
            )
            continue

        changes: Dict[str, Any] = {}
        if target_type and (field.data_type or "").upper() != target_type:
            changes["data_type"] = target_type
        if target_length is not None and int(field.length or 0) != target_length:
            changes["length"] = target_length
        if target_dec is not None and int(field.decimal_places or 0) != target_dec:
            changes["decimal_places"] = target_dec
        if not changes:
            unchanged.append({"line": line_no, "table_code": table.table_code, "field_en": field_en})
            continue
        if table.status == "PUBLISHED":
            errors.append({"line": line_no, "message": f"{table.table_code}.{field_en} 已发布，不可直接修改"})
            continue
        old = {
            "data_type": field.data_type,
            "length": field.length,
            "decimal_places": field.decimal_places,
            "type_basis": field.type_basis,
        }
        for key, value in changes.items():
            setattr(field, key, value)
        field.type_basis = TYPE_BASIS_MANUAL
        if not dry_run:
            versions.setdefault(table.id, table.current_version)
            table.current_version = versions[table.id] + 1
            versions[table.id] += 1
            table.status = "DRAFT"
            session.add(
                MetadataRevisionModel(
                    id=str(uuid.uuid4()),
                    table_id=table.id,
                    field_id=field.id,
                    revision_kind="HUMAN_EDIT",
                    old_value_json=old,
                    new_value_json={**old, **changes, "type_basis": TYPE_BASIS_MANUAL},
                    editor=editor,
                    reason=(cell(values, "reason") or reason)[:200],
                )
            )
        updated.append({"line": line_no, "table_code": table.table_code, "field_en": field_en, "changes": changes})
    if dry_run:
        session.rollback()
    else:
        session.commit()
    return {
        "updated": len(updated),
        "unchanged": len(unchanged),
        "errors": errors,
        "details": updated,
        "dry_run": dry_run,
    }
