import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .asset_models import (
    CodeSetModel,
    CodeSetValidationModel,
    CodeValueModel,
    FieldCodeReferenceModel,
    FieldMetadataModel,
    RawBlockModel,
    TableMetadataModel,
)
from .catalog import _extract_code_pairs_from_section
from .config import Settings
from .db import SessionLocal
from .generic_ai import RealDesignLlm


class CodeValueDraft(BaseModel):
    value: str = Field(min_length=1)
    label_cn: str = Field(min_length=1)
    label_en: str = ""
    definition: str = ""


class CodeSetDraft(BaseModel):
    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    definition: str = ""
    source_type: str = "DOCUMENT_TABLE"
    values: List[CodeValueDraft] = Field(default_factory=list)


CODE_NAME_SYNONYMS = {
    "国别": "国家和地区",
    "国别和地区": "国家和地区",
    "国家地区": "国家和地区",
    "国家和地区": "国家和地区",
    "行业属性": "行业类别",
    "行业": "行业类别",
    "所属行业": "行业类别",
    "行业类别": "行业类别",
    "国民经济行业": "行业类别",
    "行政区划": "中国大陆行政区划",
    "中国大陆行政区划": "中国大陆行政区划",
    "地区": "中国大陆行政区划",
    "交易编码": "交易代码",
    "交易代码": "交易代码",
    "对方与本机构的关系": "对方与本机构被代理居民机构委托人的关系",
    "对方与本机构被代理居民机构委托人的关系": "对方与本机构被代理居民机构委托人的关系",
    "外债的债务类型": "债务类型",
}


CIRCLED_NUM = "①②③④⑤⑥⑦⑧⑨⑩"


def parse_inline_enum(text: str) -> List[Dict[str, str]]:
    """把 "Y-是；N-否" / "①政府；②央行" / "是-Y，否-N" 这类内联枚举解析成码值对；解析失败返回空。"""
    parts = [p.strip() for p in re.split(r"[；;，,]", text or "") if p.strip()]
    if len(parts) < 2:
        return []
    values = []
    for part in parts:
        match = re.match(r"^([A-Za-z0-9①②③④⑤⑥⑦⑧⑨⑩]+)\s*[\-—:：、]?\s*(.+)$", part)
        if match:
            code, label = match.group(1).strip(), match.group(2).strip()
        else:
            reverse = re.match(r"^(.+?)\s*[\-—:：、]\s*([A-Za-z0-9①②③④⑤⑥⑦⑧⑨⑩]+)$", part)
            if not reverse:
                return []
            label, code = reverse.group(1).strip(), reverse.group(2).strip()
        if code in CIRCLED_NUM:
            code = str(CIRCLED_NUM.index(code) + 1)
        values.append({"value": code[:20], "label_cn": label[:80]})
    codes = [item["value"] for item in values]
    if len(set(codes)) != len(codes):
        return []
    return values


def normalize_code_set_name(raw_name: str) -> str:
    if not raw_name:
        return ""
    text = raw_name.strip()
    # 先去掉括号及内容（如"（3位字母代码）"），避免其污染核心名
    text = re.sub(r"[（(][^）)]*[）)]", "", text)
    text = re.sub(r"^(?:参?见|参照|本制度|数据采集规范最新版本中的相关代码表或本制度)+", "", text)
    # 去掉"附录"残留（括号在上一步已移除）
    text = re.sub(r"(?:五、附录|附[录表])", "", text)
    text = re.sub(r"(?:代码表|分类标准表|分类标准|代码|名录|表|填写|填)+$", "", text)
    text = re.sub(r"[\s/（）()—\-·:：，,。]", "", text)
    return CODE_NAME_SYNONYMS.get(text, text)


def validate_code_set_draft(draft: CodeSetDraft) -> List[Dict[str, Any]]:
    issues = []
    seen = set()
    for item in draft.values:
        if item.value in seen:
            issues.append({"severity": "WARNING", "code": "DUPLICATE_CODE_VALUE", "value": item.value})
        seen.add(item.value)
        if not item.label_cn.strip():
            issues.append({"severity": "ERROR", "code": "EMPTY_LABEL", "value": item.value})
    if not draft.values:
        issues.append({"severity": "WARNING", "code": "EMPTY_CODE_SET", "value": ""})
    return issues


def code_set_summary(session: Session, row: CodeSetModel) -> Dict[str, Any]:
    value_count = session.query(CodeValueModel).filter(CodeValueModel.code_set_id == row.id).count()
    enabled_count = session.query(CodeValueModel).filter(CodeValueModel.code_set_id == row.id, CodeValueModel.enabled.is_(True)).count()
    unresolved = session.query(FieldCodeReferenceModel).filter(
        FieldCodeReferenceModel.code_set_id == row.id,
        FieldCodeReferenceModel.status == "UNRESOLVED",
    ).count()
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "definition": row.definition,
        "source_type": row.source_type,
        "status": row.status,
        "current_version": row.current_version,
        "published_version": row.published_version,
        "value_count": value_count,
        "enabled_value_count": enabled_count,
        "unresolved_reference_count": unresolved,
    }


def document_code_set_ids(session: Session, document_id: str) -> set:
    block_ids = {
        row.id for row in session.query(RawBlockModel.id).filter(RawBlockModel.document_id == document_id).all()
    }
    referenced_ids = {
        row.code_set_id
        for row in session.query(FieldCodeReferenceModel)
        .join(FieldMetadataModel, FieldCodeReferenceModel.field_id == FieldMetadataModel.id)
        .join(TableMetadataModel, FieldMetadataModel.table_id == TableMetadataModel.id)
        .filter(TableMetadataModel.document_id == document_id)
        .all()
    }
    sourced_ids = {
        value.code_set_id
        for value in session.query(CodeValueModel).all()
        if value.source_locator.get("block_id") in block_ids
    }
    return referenced_ids | sourced_ids


def refresh_reference_status(session: Session, code_set_id: str) -> None:
    session.flush()
    has_values = session.query(CodeValueModel).filter(
        CodeValueModel.code_set_id == code_set_id,
        CodeValueModel.enabled.is_(True),
    ).count() > 0
    references = session.query(FieldCodeReferenceModel).filter(FieldCodeReferenceModel.code_set_id == code_set_id).all()
    for reference in references:
        reference.status = "RESOLVED" if has_values else "UNRESOLVED"
    session.flush()


def _find_matching_code_set(
    session: Session,
    reference_text: str,
    by_name: Optional[Dict[str, CodeSetModel]] = None,
    by_code: Optional[Dict[str, CodeSetModel]] = None,
    by_norm: Optional[Dict[str, Optional[CodeSetModel]]] = None,
) -> Optional[CodeSetModel]:
    if by_name is not None and reference_text in by_name:
        return by_name[reference_text]
    if by_code is not None and reference_text.upper() in by_code:
        return by_code[reference_text.upper()]
    norm_target = normalize_code_set_name(reference_text)
    if by_norm is not None and norm_target:
        return by_norm.get(norm_target)

    exact = session.query(CodeSetModel).filter(CodeSetModel.name == reference_text).first()
    if exact:
        return exact
    code_match = session.query(CodeSetModel).filter(CodeSetModel.code == reference_text.upper()).first()
    if code_match:
        return code_match
    if not norm_target:
        return None
    candidates = session.query(CodeSetModel).all()
    for cs in candidates:
        if normalize_code_set_name(cs.name) == norm_target:
            return cs
    # 核心名包含匹配：一方包含另一方且长度接近，容忍"附录/参照/（3位字母代码）"等残留修饰
    contains = []
    for cs in candidates:
        norm_cs = normalize_code_set_name(cs.name)
        if len(norm_cs) < 2:
            continue
        shorter, longer = (norm_target, norm_cs) if len(norm_target) <= len(norm_cs) else (norm_cs, norm_target)
        if len(shorter) >= 2 and shorter in longer and len(longer) - len(shorter) <= 6:
            contains.append(cs)
    if contains:
        with_values = [
            cs for cs in contains
            if session.query(CodeValueModel).filter(CodeValueModel.code_set_id == cs.id).count() > 0
        ]
        return (with_values or contains)[0]
    return None


def ensure_field_reference(
    session: Session,
    field: FieldMetadataModel,
    by_name: Optional[Dict[str, CodeSetModel]] = None,
    by_code: Optional[Dict[str, CodeSetModel]] = None,
    by_norm: Optional[Dict[str, Optional[CodeSetModel]]] = None,
    sets_with_values: Optional[set] = None,
    existing_ref_by_field: Optional[Dict[str, FieldCodeReferenceModel]] = None,
) -> None:
    text = (field.code_reference_text or "").strip()
    if not text or text.upper() in {"无", "N/A", "NONE", "无引用"}:
        return
    code_set = _find_matching_code_set(session, text, by_name=by_name, by_code=by_code, by_norm=by_norm)
    if not code_set:
        enum_values = parse_inline_enum(text)
        if enum_values:
            code = re.sub(r"[^A-Z0-9_]", "_", f"{field.field_en}_INLINE_ENUM".upper())[:96]
            code_set = by_code.get(code) if by_code is not None else session.query(CodeSetModel).filter(CodeSetModel.code == code).first()
            if not code_set:
                code_set = CodeSetModel(
                    id=str(uuid.uuid4()),
                    code=code,
                    name=text[:255],
                    definition=f"字段 {field.field_en} 的内联枚举",
                    source_type="INLINE_ENUM",
                    status="AI_SELF_VALIDATED",
                    current_version=1,
                )
                session.add(code_set)
                session.flush()
                for index, item in enumerate(enum_values):
                    session.add(
                        CodeValueModel(
                            id=str(uuid.uuid4()),
                            code_set_id=code_set.id,
                            value=item["value"],
                            label_cn=item["label_cn"],
                            label_en="",
                            definition="",
                            sort_order=index,
                            enabled=True,
                            source_locator={},
                        )
                    )
                if by_code is not None:
                    by_code[code] = code_set
                if sets_with_values is not None:
                    sets_with_values.add(code_set.id)
        else:
            code = re.sub(r"[^A-Z0-9_]", "_", f"{field.field_en}_CODESET".upper())
            code_set = by_code.get(code) if by_code is not None else session.query(CodeSetModel).filter(CodeSetModel.code == code).first()
            if not code_set:
                code_set = CodeSetModel(
                    id=str(uuid.uuid4()),
                    code=code[:96],
                    name=text[:255],
                    definition=f"由字段 {field.field_en} 的校验引用自动建立",
                    source_type="DOCUMENT_REFERENCE",
                    status="DRAFT",
                    current_version=1,
                )
                session.add(code_set)
                session.flush()
                if by_name is not None:
                    by_name[text] = code_set
                if by_code is not None:
                    by_code[code] = code_set
                if by_norm is not None:
                    nk = normalize_code_set_name(text)
                    if nk and nk not in by_norm:
                        by_norm[nk] = code_set

    has_val = (
        (code_set.id in sets_with_values)
        if sets_with_values is not None
        else (session.query(CodeValueModel).filter(CodeValueModel.code_set_id == code_set.id, CodeValueModel.enabled.is_(True)).count() > 0)
    )
    target_status = "RESOLVED" if has_val else "UNRESOLVED"

    existing = (
        existing_ref_by_field.get(field.id)
        if existing_ref_by_field is not None
        else session.query(FieldCodeReferenceModel).filter(FieldCodeReferenceModel.field_id == field.id).first()
    )
    if existing:
        existing.code_set_id = code_set.id
        existing.reference_text = text
        existing.status = target_status
    else:
        ref = FieldCodeReferenceModel(
            id=str(uuid.uuid4()),
            field_id=field.id,
            code_set_id=code_set.id,
            reference_text=text,
            status=target_status,
            condition_json={},
            confidence=field.confidence,
        )
        session.add(ref)
        if existing_ref_by_field is not None:
            existing_ref_by_field[field.id] = ref


def ensure_table_references(session: Session, table: TableMetadataModel) -> None:
    fields = session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == table.id).all()
    for field in fields:
        ensure_field_reference(session, field)


def auto_link_all_references(session: Session) -> int:
    """Fast O(F + C) batch alignment between all FieldMetadataModel and CodeSetModel rows."""
    session.flush()
    all_sets = session.query(CodeSetModel).all()
    sets_with_values = {
        v.code_set_id
        for v in session.query(CodeValueModel).filter(CodeValueModel.enabled.is_(True)).all()
    }
    by_name: Dict[str, CodeSetModel] = {}
    by_code: Dict[str, CodeSetModel] = {}
    by_norm: Dict[str, Optional[CodeSetModel]] = {}

    for cs in all_sets:
        by_name[cs.name] = cs
        by_code[cs.code.upper()] = cs
        nk = normalize_code_set_name(cs.name)
        if not nk:
            continue
        existing = by_norm.get(nk)
        if existing is None or existing.id == cs.id:
            by_norm[nk] = cs
        elif (cs.id in sets_with_values) and (existing.id not in sets_with_values):
            by_norm[nk] = cs  # 归一化名冲突时优先保留有值码表，避免占位表遮蔽

    existing_ref_by_field = {
        r.field_id: r for r in session.query(FieldCodeReferenceModel).all()
    }
    for field in session.query(FieldMetadataModel).all():
        text = (field.code_reference_text or "").strip()
        if not text or text.upper() in {"无", "N/A", "NONE", "无引用"}:
            continue
        ensure_field_reference(
            session,
            field,
            by_name=by_name,
            by_code=by_code,
            by_norm=by_norm,
            sets_with_values=sets_with_values,
            existing_ref_by_field=existing_ref_by_field,
        )

    for ref in session.query(FieldCodeReferenceModel).all():
        ref.status = "RESOLVED" if ref.code_set_id in sets_with_values else "UNRESOLVED"

    session.flush()
    return sum(1 for r in session.query(FieldCodeReferenceModel).all() if r.status == "RESOLVED")


def _clean_value_drafts(raw_values: List[Dict[str, Any]]) -> List[CodeValueDraft]:
    cleaned = []
    for v in raw_values:
        val = str(v.get("value") or "").strip()
        lbl = str(v.get("label_cn") or "").strip()
        if not val or not lbl:
            continue
        cleaned.append(
            CodeValueDraft(
                value=val,
                label_cn=lbl,
                label_en=str(v.get("label_en") or "").strip(),
                definition=str(v.get("definition") or "").strip(),
            )
        )
    return cleaned


def _upsert_code_set_draft(
    session: Session,
    draft: CodeSetDraft,
    locator: Dict[str, Any],
) -> Tuple[CodeSetModel, bool]:
    existing = (
        session.query(CodeSetModel)
        .filter(CodeSetModel.code == draft.code.upper()[:96])
        .order_by(CodeSetModel.current_version.desc())
        .first()
    )
    if not existing:
        existing = session.query(CodeSetModel).filter(CodeSetModel.name == draft.name[:255]).first()
    changed = False
    if existing:
        row = existing
        if draft.definition and (not row.definition or row.source_type == "DOCUMENT_REFERENCE") and row.definition != draft.definition:
            row.definition = draft.definition
            changed = True
        if row.source_type == "DOCUMENT_REFERENCE" and row.source_type != draft.source_type:
            row.source_type = draft.source_type
            changed = True
    else:
        has_errors = any(issue["severity"] == "ERROR" for issue in validate_code_set_draft(draft))
        row = CodeSetModel(
            id=str(uuid.uuid4()),
            code=draft.code.upper()[:96],
            name=draft.name[:255],
            definition=draft.definition,
            source_type=draft.source_type,
            status="NEEDS_REVIEW" if has_errors else ("AI_SELF_VALIDATED" if draft.values else "DRAFT"),
            current_version=1,
        )
        session.add(row)
        session.flush()

    existing_vals = {
        item.value: item
        for item in session.query(CodeValueModel).filter(CodeValueModel.code_set_id == row.id).all()
    }
    sort_order = len(existing_vals)
    for item in draft.values:
        code_val = item.value.strip()[:128]
        if not code_val:
            continue
        if code_val in existing_vals:
            cur = existing_vals[code_val]
            if item.label_cn and not cur.label_cn:
                cur.label_cn = item.label_cn[:255]
                changed = True
            if item.label_en and not cur.label_en:
                cur.label_en = item.label_en[:255]
                changed = True
            if item.definition and not cur.definition:
                cur.definition = item.definition
                changed = True
        else:
            new_val = CodeValueModel(
                id=str(uuid.uuid4()),
                code_set_id=row.id,
                value=code_val,
                label_cn=item.label_cn.strip()[:255],
                label_en=(item.label_en or "").strip()[:255],
                definition=(item.definition or "").strip(),
                sort_order=sort_order,
                enabled=True,
                source_locator=locator,
            )
            session.add(new_val)
            existing_vals[code_val] = new_val
            sort_order += 1
            changed = True

    if not existing:
        return row, True
    has_errors = any(issue["severity"] == "ERROR" for issue in validate_code_set_draft(draft))
    target_status = "NEEDS_REVIEW" if has_errors else ("AI_SELF_VALIDATED" if existing_vals else "DRAFT")
    if row.status != "PUBLISHED" and row.status != target_status:
        row.status = target_status
        changed = True
    if changed:
        row.current_version += 1
    return row, changed


def _record_code_set_validation(
    session: Session,
    row: CodeSetModel,
    draft: CodeSetDraft,
    block: RawBlockModel,
    settings: Settings,
    content_changed: bool,
    review_with_ai: bool = True,
) -> None:
    program_issues = validate_code_set_draft(draft)
    session.add(
        CodeSetValidationModel(
            id=str(uuid.uuid4()),
            code_set_id=row.id,
            validator_kind="PROGRAM",
            status="NEEDS_REVIEW" if program_issues else "PASSED",
            issues_json=program_issues,
            trace_json={"value_count": len(draft.values)},
        )
    )
    if row.status == "PUBLISHED":
        return
    if not review_with_ai:
        row.status = "NEEDS_REVIEW" if program_issues else "PROGRAM_VALIDATED"
        return

    llm = None
    try:
        llm = RealDesignLlm(settings)
    except Exception:
        pass

    existing_review = session.query(CodeSetValidationModel).filter(
        CodeSetValidationModel.code_set_id == row.id,
        CodeSetValidationModel.validator_kind == "AI_REVIEW",
    ).first()
    if llm is None:
        row.status = "NEEDS_REVIEW" if program_issues else "PROGRAM_VALIDATED"
        return
    if not content_changed and existing_review:
        return

    try:
        args, trace = llm.assign(
            "review_code_set",
            "独立复核码值表。仅输出码值遗漏、重复、含义错配、无证据码值或定义不一致的问题；无问题返回空数组。",
            {
                "source_locator": block.source_locator,
                "code_set": draft.model_dump(),
                "raw_context": block.raw_content[:6000],
            },
            timeout=35,
        )
        issues = args.get("issues", [])
    except Exception as exc:
        trace = {"error": f"{type(exc).__name__}: {exc}"}
        issues = [{
            "severity": "WARNING",
            "code": "AI_REVIEW_UNAVAILABLE",
            "value": "",
            "message": f"AI 复核不可用：{trace['error']}",
        }]
    session.add(
        CodeSetValidationModel(
            id=str(uuid.uuid4()),
            code_set_id=row.id,
            validator_kind="AI_REVIEW",
            status="NEEDS_REVIEW" if issues else "PASSED",
            issues_json=issues,
            trace_json=trace,
        )
    )
    row.status = "NEEDS_REVIEW" if program_issues or issues else "AI_SELF_VALIDATED"


def extract_code_sets_for_block(block_id: str, settings: Settings, auto_link: bool = True, review_with_ai: bool = True) -> List[str]:
    """Extract standalone CodeSets or inline field enum CodeSets from a RawBlockModel (Track B)."""
    with SessionLocal() as session:
        block = session.get(RawBlockModel, block_id)
        if not block:
            raise ValueError("Raw block not found")

        created_ids: List[str] = []
        content = block.content_json or {}

        # Case 1: Block has pre-extracted standalone code_set_spec (from PDF Chapter 9 / Appendix / DOCX / XLSX)
        cs_spec = content.get("code_set_spec")
        if cs_spec and cs_spec.get("values"):
            valid_vals = _clean_value_drafts(cs_spec["values"])
            if valid_vals:
                draft = CodeSetDraft(
                    code=cs_spec["code"],
                    name=cs_spec["name"],
                    definition=cs_spec.get("definition", ""),
                    source_type=cs_spec.get("source_type", "DOCUMENT_TABLE"),
                    values=valid_vals,
                )
                row, changed = _upsert_code_set_draft(session, draft, {**block.source_locator, "block_id": block.id})
                _record_code_set_validation(session, row, draft, block, settings, changed, review_with_ai=review_with_ai)
                created_ids.append(row.id)

        # Case 2: Block has inline_code_sets (from table fields in Chapter 6 or Statistical System Section 四)
        inline_list = content.get("inline_code_sets") or []
        for inline_cs in inline_list:
            valid_vals = _clean_value_drafts(inline_cs.get("values") or [])
            if not valid_vals:
                continue
            draft = CodeSetDraft(
                code=inline_cs["code"],
                name=inline_cs["name"],
                definition=inline_cs.get("definition", ""),
                source_type="INLINE_ENUM",
                values=valid_vals,
            )
            row, changed = _upsert_code_set_draft(
                session,
                draft,
                {
                    **block.source_locator,
                    "block_id": block.id,
                    "field_en": inline_cs.get("field_en", ""),
                    "page": inline_cs.get("page", ""),
                },
            )
            _record_code_set_validation(session, row, draft, block, settings, changed, review_with_ai=review_with_ai)
            created_ids.append(row.id)

        # Case 3: Unstructured block routed as CODE_SET without pre-extracted values -> regex + AI fallback
        if not created_ids and (block.block_type in {"CODE_SET", "CODE_SET_CANDIDATE"} or content.get("candidate_role") == "CODE_SET"):
            title = content.get("table_name") or block.source_locator.get("title") or block.source_locator.get("section") or f"码值表_{block.ordinal + 1}"
            code_id = content.get("table_code") or re.sub(r"[^A-Z0-9_]", "_", f"CS_{block.id[:8]}".upper())
            pairs = _extract_code_pairs_from_section(block.raw_content, str(block.source_locator.get("section", "")), title)
            valid_vals = _clean_value_drafts(pairs)
            if not valid_vals:
                try:
                    llm = RealDesignLlm(settings)
                    args, _ = llm.assign(
                        "assign_code_set",
                        "从输入块中提取码值表代码、中文名、定义及全部码值项（value、label_cn、label_en、definition），不得虚构。",
                        {"source_locator": block.source_locator, "raw_content": block.raw_content[:6000]},
                        timeout=35,
                    )
                    valid_vals = _clean_value_drafts(args.get("values", []))
                    draft = CodeSetDraft(
                        code=args.get("code") or code_id,
                        name=args.get("name") or title,
                        definition=args.get("definition", ""),
                        source_type="AI_EXTRACTED",
                        values=valid_vals,
                    )
                except Exception:
                    draft = CodeSetDraft(code=code_id, name=title, definition="待补充码值项", source_type="DOCUMENT_TABLE", values=[])
            else:
                draft = CodeSetDraft(
                    code=code_id,
                    name=title,
                    definition=f"从文档块 {title} 提取",
                    source_type="DOCUMENT_TABLE",
                    values=valid_vals,
                )
            if draft.values:
                row, changed = _upsert_code_set_draft(session, draft, {**block.source_locator, "block_id": block.id})
                _record_code_set_validation(session, row, draft, block, settings, changed, review_with_ai=review_with_ai)
                created_ids.append(row.id)

        if auto_link:
            auto_link_all_references(session)
        session.commit()
        return created_ids


def extract_parallel_for_blocks(
    block_ids: List[str],
    settings: Settings,
    targets: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run Track A (Table Metadata) and Track B (Code Sets) concurrently across selected blocks, then auto-link in one O(F+C) pass."""
    active_targets = set(targets or ["metadata", "code_sets"])
    with SessionLocal() as session:
        blocks = session.query(RawBlockModel).filter(RawBlockModel.id.in_(block_ids)).all()
        by_id = {b.id: b for b in blocks}
        metadata_block_ids: List[str] = []
        code_set_block_ids: List[str] = []
        for bid in block_ids:
            b = by_id.get(bid)
            if not b or b.content_json.get("superseded"):
                continue
            is_cs_only = (
                b.block_type in {"CODE_SET", "CODE_SET_CANDIDATE"}
                or b.content_json.get("candidate_role") == "CODE_SET"
            )
            is_rule_evidence = (
                b.block_type in {"RULE_EVIDENCE", "REVISION_DIFF"}
                or b.content_json.get("candidate_role") in {"RULE_EVIDENCE", "REVISION_DIFF"}
            )
            if "metadata" in active_targets and not is_cs_only and not is_rule_evidence and b.block_type != "IGNORE":
                metadata_block_ids.append(bid)
            if "code_sets" in active_targets:
                if is_cs_only or b.content_json.get("inline_code_sets") or b.content_json.get("code_set_spec"):
                    code_set_block_ids.append(bid)

    from .metadata_service import extract_metadata_for_block
    from .segment_service import analyze_block_segments, merge_metadata_tables

    def _run_metadata_one(bid: str) -> str:
        with SessionLocal() as s:
            blk = s.get(RawBlockModel, bid)
            has_structured = bool(
                blk.content_json.get("spec")
                or blk.content_json.get("parent_spec")
                or blk.content_json.get("rows")
            )
        if has_structured:
            return extract_metadata_for_block(bid, settings, auto_link=False)
        seg_ids = analyze_block_segments(bid, settings)
        seg_tables = [extract_metadata_for_block(sid, settings, auto_link=False) for sid in seg_ids]
        if len(seg_tables) > 1:
            with SessionLocal() as merge_session:
                return merge_metadata_tables(merge_session, bid, seg_tables)
        if seg_tables:
            return seg_tables[0]
        raise ValueError(f"No table metadata produced for block {bid}")

    def _run_code_set_one(bid: str) -> List[str]:
        return extract_code_sets_for_block(bid, settings, auto_link=False)

    table_ids: List[str] = []
    code_set_ids: List[str] = []
    errors: List[Dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(metadata_block_ids)))) as executor:
        meta_futures = {bid: executor.submit(_run_metadata_one, bid) for bid in metadata_block_ids}

        for bid in code_set_block_ids:
            try:
                for cid in _run_code_set_one(bid):
                    if cid not in code_set_ids:
                        code_set_ids.append(cid)
            except Exception as exc:
                errors.append({"block_id": bid, "track": "code_sets", "error": f"{type(exc).__name__}: {exc}"})

        for bid, future in meta_futures.items():
            try:
                table_id = future.result()
                if table_id:
                    table_ids.append(table_id)
            except Exception as exc:
                errors.append({"block_id": bid, "track": "metadata", "error": f"{type(exc).__name__}: {exc}"})

    with SessionLocal() as session:
        resolved_count = auto_link_all_references(session)
        session.commit()

    produced_count = len(table_ids) + len(code_set_ids)
    status = "PARALLEL_EXTRACTION_COMPLETE"
    if errors:
        status = "PARALLEL_EXTRACTION_PARTIAL" if produced_count else "PARALLEL_EXTRACTION_FAILED"
    return {
        "table_ids": table_ids,
        "code_set_ids": code_set_ids,
        "resolved_reference_count": resolved_count,
        "errors": errors,
        "status": status,
    }


def generation_blockers(session: Session, table: TableMetadataModel) -> List[Dict[str, Any]]:
    blockers = []
    fields = session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == table.id).all()
    for field in fields:
        references = session.query(FieldCodeReferenceModel).filter(FieldCodeReferenceModel.field_id == field.id).all()
        text = field.code_reference_text.strip()
        if text and text.upper() not in {"无", "N/A", "NONE", "无引用"} and not references:
            blockers.append({"field_en": field.field_en, "reason": "CODE_REFERENCE_NOT_BOUND"})
        for reference in references:
            if reference.status == "UNRESOLVED" and field.required:
                blockers.append({"field_en": field.field_en, "reason": "REQUIRED_CODE_SET_UNRESOLVED", "code_set_id": reference.code_set_id})
    return blockers
