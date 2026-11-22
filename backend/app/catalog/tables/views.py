"""元数据视图序列化、原文证据降级校验与 SpecDocument 转换（Metadata Views）。

职责边界：
- 原文声明判定与问题证据核查：`enforce_issue_evidence`、`table_needs_review`、`type_basis_for`；
- 工作台查询序列化：`_field_view`、`table_summary`、`table_detail`；
- 阶段二输入转换：`metadata_to_spec`（将已发布表元数据转为不可变 `SpecDocument`）。
"""

import re
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.infra.contracts import FieldSpec, SpecDocument
from app.infra.db_tables import (
    FieldCodeReferenceModel,
    FieldMetadataModel,
    MetadataRevisionModel,
    MetadataValidationModel,
    RawBlockModel,
    SourceDocumentModel,
    TableMetadataModel,
)


# 来源原文里出现这些字样，才说明这份文档真的声明了数据类型/长度（兼容外管局中文声明与 EAST/1104/SQL 标准声明）
TYPE_DECLARATION_HINTS = (
    "类型（长度）",
    "数据类型",
    "字符型",
    "数值型",
    "整数型",
    "日期型",
    "定长",
    "变长",
    "数据格式",
    "VARCHAR",
    "CHAR(",
    "NUMBER(",
    "DECIMAL(",
    "NUMERIC(",
    "C..",
    "N..",
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
    upper = text.upper()
    return any((hint in text) or (hint.isascii() and hint in upper) for hint in TYPE_DECLARATION_HINTS)


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
    """判断原文对该字段是否给出了明确的长度（支持外管局中文声明与 EAST/1104/Word/Excel 常见 SQL 类型声明）。

    True = 写了"数值型，13.8" / "VARCHAR(60)" / "DECIMAL(20,2)" / "C..30" 这类完整声明；
    False = 只写了"数值型" / "VARCHAR" / "NUMBER" 没给长度；
    None = 原文里找不到该字段的类型声明。
    """
    if not field_en:
        return None
    cn_type_pattern = r"(定长字符型|变长字符型|字符型|数值型|日期型|整数型|长整数型|时间型)"
    sql_type_with_len = (
        r"(?:\b(?:VARCHAR2?|CHAR|NUMBER|DECIMAL|NUMERIC)\s*[(（]\s*\d+(?:\s*[,，.]\s*\d+)?\s*[)）]"
        r"|\b[CN]\.{0,2}\d+(?:[.,]\d+)?\b"
        r"|\b(?:DATE|TIMESTAMP|INTEGER|INT)\b)"
    )
    sql_type_bare = r"\b(?:VARCHAR2?|CHAR|NUMBER|DECIMAL|NUMERIC)\b"
    field_token_re = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(field_en)}(?![A-Za-z0-9_])", re.IGNORECASE)
    lines = (raw_context or "").split("\n")
    found_type = False
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not (stripped.startswith(field_en) or field_token_re.search(stripped)):
            continue
        if re.search(cn_type_pattern + r"\s*[，,(（]\s*\d+", stripped):
            return True
        if re.search(sql_type_with_len, stripped, re.IGNORECASE):
            return True
        if re.search(cn_type_pattern, stripped):
            found_type = True
            # 长度可能被折到下一行，但下一行必须是纯续行（不是新字段）才算
            nxt = lines[index + 1] if index + 1 < len(lines) else ""
            if nxt and not re.match(r"^\s*[A-Z][A-Z0-9_]{2,}\s", nxt) and re.search(
                r"^\s*[，,]?\s*\d{1,3}(\.\d+)?", nxt
            ):
                return True
        elif re.search(sql_type_bare, stripped, re.IGNORECASE):
            found_type = True
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
    refs: Dict[str, str] = {}
    field_ids = [field.id for field in fields]
    for idx in range(0, len(field_ids), 500):
        chunk = field_ids[idx : idx + 500]
        for ref in (
            session.query(FieldCodeReferenceModel)
            .filter(FieldCodeReferenceModel.field_id.in_(chunk))
            .all()
        ):
            refs[ref.field_id] = ref.code_set_id
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


def metadata_to_spec(session: Session, table: TableMetadataModel) -> SpecDocument:
    if table.status != "PUBLISHED" or table.published_version != table.current_version:
        raise ValueError("TABLE_NOT_PUBLISHED")
    document = session.get(SourceDocumentModel, table.document_id)
    if not document:
        raise ValueError("SOURCE_DOCUMENT_NOT_FOUND")
    block = session.get(RawBlockModel, table.block_id)
    if not block:
        raise ValueError("RAW_BLOCK_NOT_FOUND")
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
