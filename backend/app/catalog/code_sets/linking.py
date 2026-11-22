"""字段-码表引用自动匹配、序号分配与生成阻断检查（Code Set Linking）。

职责边界：
- `document_code_set_ids` / `document_code_set_ordinals`：计算文档作用域内码表集合与展示序号；
- `ensure_field_reference` / `ensure_table_references` / `auto_link_all_references`：
  将字段 `code_reference_text` 对齐到真实有值码表（或内联枚举自动建表），刷新 `RESOLVED/UNRESOLVED` 状态；
- `generation_blockers`：在阶段二生成前检查表内是否存在未解决的码表阻断项。
"""

import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.catalog.code_sets.parser import (
    is_rule_reference,
    normalize_code_set_name,
    parse_fixed_value,
    parse_inline_enum,
)
from app.infra.db_tables import (
    CodeSetModel,
    CodeValueModel,
    FieldCodeReferenceModel,
    FieldMetadataModel,
    RawBlockModel,
    TableMetadataModel,
)


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
        code_set_id
        for code_set_id, source_locator in session.query(
            CodeValueModel.code_set_id,
            CodeValueModel.source_locator,
        ).all()
        if (source_locator or {}).get("block_id") in block_ids
    }
    return referenced_ids | sourced_ids


def document_code_set_ordinals(session: Session, document_id: str, code_set_ids: set) -> Dict[str, int]:
    """按"在当前文件里出现的先后"给码表定位。

    优先用码表自身（码值来源块）的位置——附录里的码值表就排到附录那一块；
    内联枚举没有独立的原文块，退回它所在字段的报表位置。
    两者都定位不到的（例如只是被本文件引用、定义在别的文件里）返回 None，列表排最后。
    """
    ordinals: Dict[str, int] = {}
    if not code_set_ids:
        return ordinals
    block_ordinal = {
        row.id: row.ordinal
        for row in session.query(RawBlockModel)
        .filter(RawBlockModel.document_id == document_id)
        .all()
    }
    located: set = set()
    value_rows = (
        session.query(CodeValueModel.code_set_id, CodeValueModel.source_locator)
        .filter(CodeValueModel.code_set_id.in_(code_set_ids))
        .all()
    )
    for code_set_id, source_locator in value_rows:
        block_id = (source_locator or {}).get("block_id")
        if block_id in block_ordinal:
            located.add(code_set_id)
            candidate = block_ordinal[block_id] * 1000
            if candidate < ordinals.get(code_set_id, candidate + 1):
                ordinals[code_set_id] = candidate
    rows = (
        session.query(
            FieldCodeReferenceModel.code_set_id,
            TableMetadataModel.source_ordinal,
            FieldMetadataModel.sort_order,
        )
        .join(FieldMetadataModel, FieldCodeReferenceModel.field_id == FieldMetadataModel.id)
        .join(TableMetadataModel, FieldMetadataModel.table_id == TableMetadataModel.id)
        .filter(TableMetadataModel.document_id == document_id)
        .all()
    )
    for code_set_id, table_ordinal, field_order in rows:
        if table_ordinal is None or code_set_id in located:
            continue
        # +400 起是为了排在"该表自身的码值表"之后、下一张表之前
        candidate = table_ordinal * 1000 + 400 + int(field_order or 0)
        if candidate < ordinals.get(code_set_id, candidate + 1):
            ordinals[code_set_id] = candidate
    return ordinals


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


def _set_has_values(
    session: Session,
    code_set: CodeSetModel,
    sets_with_values: Optional[set] = None,
) -> bool:
    if sets_with_values is not None:
        return code_set.id in sets_with_values
    return (
        session.query(CodeValueModel)
        .filter(CodeValueModel.code_set_id == code_set.id, CodeValueModel.enabled.is_(True))
        .count()
        > 0
    )


def _reference_name_variants(text: str) -> List[str]:
    """把"境内主体类型代码表/境外主体类型代码表"拆成多个候选名，逐个去匹配真实码表。"""
    raw = (text or "").strip()
    if not raw:
        return []
    variants = [raw]
    for match in re.finditer(r"[（(]([^）)]{2,40})[）)]", raw):
        inner = match.group(1).strip()
        if inner and inner not in variants:
            variants.append(inner)
    for match in re.finditer(r"([A-Z][A-Z0-9_]{2,})\s*内联枚举", raw):
        # "（F0105 内联枚举）"指向另一字段的内联枚举表，按代码直接找
        code_name = f"{match.group(1)}_INLINE_ENUM"
        if code_name not in variants:
            variants.append(code_name)
    for pattern in (r"\s*/\s*", r"／", r"\s*、\s*", r"[；;。\n]", r"或"):
        for piece in re.split(pattern, raw):
            piece = piece.strip(" 　。.；;，,")
            if piece and piece not in variants:
                variants.append(piece)
    return variants


def _find_matching_code_set(
    session: Session,
    reference_text: str,
    by_name: Optional[Dict[str, CodeSetModel]] = None,
    by_code: Optional[Dict[str, CodeSetModel]] = None,
    by_norm: Optional[Dict[str, Optional[CodeSetModel]]] = None,
    sets_with_values: Optional[set] = None,
    candidate_norms: Optional[List[Tuple[str, CodeSetModel]]] = None,
) -> Optional[CodeSetModel]:
    """按"精确名 → 精确码 → 归一化名 → 核心名包含"的顺序找码表，并优先返回已有值的码表。

    系统会为找不到码表的字段建"占位码表"（名字就是引用原文、没有任何取值）。占位表
    会和真实码表抢同一个归一化名，所以这里把"名称与引用原文完全相同且无值"的候选降级，
    避免占位表把真正有 200 条码值的码表遮蔽掉。
    """
    text = (reference_text or "").strip()
    if not text:
        return None
    names = _reference_name_variants(text)

    scored: List[Tuple[Tuple[int, int], CodeSetModel]] = []
    seen_ids = set()

    def collect(rank: int, code_set: Optional[CodeSetModel]) -> None:
        if code_set is None or code_set.id in seen_ids:
            return
        seen_ids.add(code_set.id)
        has_values = _set_has_values(session, code_set, sets_with_values)
        effective_rank = rank
        if not has_values and code_set.source_type == "DOCUMENT_REFERENCE":
            # 由引用原文自建的占位表一律降级：只要库里有真正带取值的码表，就优先用真实的
            effective_rank += 10
        scored.append(((effective_rank, 0 if has_values else 1), code_set))

    for name in names:
        if by_name is not None:
            collect(0, by_name.get(name))
        if by_code is not None:
            collect(1, by_code.get(name.upper()))

    for name in names:
        if not by_norm:
            continue
        norm_name = normalize_code_set_name(name)
        if norm_name:
            collect(2, by_norm.get(norm_name))

    if not scored:
        # 非批量场景（未预置索引）：直接查库
        for name in names:
            collect(0, session.query(CodeSetModel).filter(CodeSetModel.name == name).first())
            collect(1, session.query(CodeSetModel).filter(CodeSetModel.code == name.upper()).first())

    norms = candidate_norms
    if norms is None:
        norms = [(normalize_code_set_name(cs.name), cs) for cs in session.query(CodeSetModel).all()]

    if not scored:
        for name in names:
            norm_name = normalize_code_set_name(name)
            if not norm_name:
                continue
            for norm_cs, code_set in norms:
                if norm_cs and norm_cs == norm_name:
                    collect(2, code_set)

    # 核心名包含匹配：一方包含另一方且长度接近，容忍"附录/参照/（3位字母代码）"等残留修饰。
    # 即使前面已有命中也要跑——占位表会以 rank 10 参与排序，真实码表 rank 5 可以反超。
    for name in names:
        norm_name = normalize_code_set_name(name)
        if len(norm_name) < 2:
            continue
        for norm_cs, code_set in norms:
            if len(norm_cs) < 2:
                continue
            shorter, longer = (
                (norm_name, norm_cs) if len(norm_name) <= len(norm_cs) else (norm_cs, norm_name)
            )
            if not (len(shorter) >= 2 and shorter in longer):
                continue
            # 引用本身是短缩写时不做"补全式"绑定（"国家" ≠ "国家和地区"，属语义推断而非匹配）
            if norm_name == shorter and longer.startswith(shorter) and len(longer) - len(shorter) >= 3:
                continue
            # 前缀包含允许更大的长度差（"境内主体类型" vs "境内主体类型代码表境外主体类型"）
            limit = 12 if longer.startswith(shorter) else 6
            if len(longer) - len(shorter) <= limit:
                collect(5, code_set)

    if not scored:
        return None
    scored.sort(key=lambda item: item[0])
    return scored[0][1]


def _compact_code_set_name(text: str) -> str:
    """把"投资工具类型代码：1-股票；2-债券…"压成"投资工具类型代码"这样的可读表名。"""
    raw = (text or "").strip()
    head = re.split(r"[：:；;、，,。]", raw)[0].strip()
    if len(head) < 4:
        return raw[:60]
    return head[:60]


def _fill_code_values(
    session: Session,
    code_set: CodeSetModel,
    enum_values: List[Dict[str, str]],
    source_text: str,
) -> int:
    """把解析出来的内联枚举补进已有码表（通常是由引用原文建的占位表）。"""
    existing = {
        row.value
        for row in session.query(CodeValueModel)
        .filter(CodeValueModel.code_set_id == code_set.id)
        .all()
    }
    order = len(existing)
    added = 0
    for item in enum_values:
        if item["value"] in existing:
            continue
        session.add(
            CodeValueModel(
                id=str(uuid.uuid4()),
                code_set_id=code_set.id,
                value=item["value"][:128],
                label_cn=item["label_cn"][:255],
                label_en="",
                definition="",
                sort_order=order,
                enabled=True,
                source_locator={},
            )
        )
        existing.add(item["value"])
        order += 1
        added += 1
    if not added:
        return 0
    if code_set.source_type == "DOCUMENT_REFERENCE":
        code_set.source_type = "INLINE_ENUM"
        code_set.name = _compact_code_set_name(source_text)[:255]
        if code_set.status == "DRAFT":
            code_set.status = "AI_SELF_VALIDATED"
    code_set.definition = (
        f"{(code_set.definition or '').strip()}\n由引用原文自动补齐 {added} 项码值：{source_text}"
    ).strip()[:1000]
    return added


def _stamp_field_ordinal(session: Session, code_set: CodeSetModel, field: FieldMetadataModel) -> None:
    """内联枚举码表的排序位置取自它所在字段的报表位置。"""
    if code_set.source_ordinal is not None:
        return
    table = session.get(TableMetadataModel, field.table_id) if field.table_id else None
    block = session.get(RawBlockModel, table.block_id) if table and table.block_id else None
    if block is not None:
        code_set.source_ordinal = block.ordinal * 1000 + (field.sort_order or 0) + 1


def _values_overlap(session: Session, code_set: CodeSetModel, enum_values: List[Dict[str, str]], sets_with_values: Optional[set]) -> bool:
    """匹配到的码表取值和枚举值有没有交集。

    只比 label，不比 value——内联枚举的 value 是「1、2、3」这种序号，
    和任何含数字代码的码表都会撞上（TAXFREECODE 撞上「货物贸易=1」），
    序号没有比对意义。
    """
    labels = {item["label_cn"] for item in enum_values}
    query = session.query(CodeValueModel.label_cn).filter(
        CodeValueModel.code_set_id == code_set.id, CodeValueModel.enabled.is_(True)
    )
    if sets_with_values is not None and code_set.id not in sets_with_values:
        return False
    return any(label and label in labels for (label,) in query.all())


def ensure_field_reference(
    session: Session,
    field: FieldMetadataModel,
    by_name: Optional[Dict[str, CodeSetModel]] = None,
    by_code: Optional[Dict[str, CodeSetModel]] = None,
    by_norm: Optional[Dict[str, Optional[CodeSetModel]]] = None,
    sets_with_values: Optional[set] = None,
    existing_ref_by_field: Optional[Dict[str, FieldCodeReferenceModel]] = None,
    candidate_norms: Optional[List[Tuple[str, CodeSetModel]]] = None,
) -> None:
    text = (field.code_reference_text or "").strip()
    if not text or text.upper() in {"无", "N/A", "NONE", "无引用"}:
        # 字段引用被人工清空时同步解绑旧引用，避免残留失效码表关联
        existing_empty = (
            existing_ref_by_field.pop(field.id, None)
            if existing_ref_by_field is not None
            else session.query(FieldCodeReferenceModel).filter(FieldCodeReferenceModel.field_id == field.id).first()
        )
        if existing_empty:
            session.delete(existing_empty)
            session.flush()
        return
    fixed = parse_fixed_value(text)
    if fixed:
        # 固定值不是码表引用：不建占位码表，也不该在「码值待补」里出现。
        # 值本身留在 code_reference_text 里，阶段 2 的 AI 依据校验原文生成 ENUM。
        session.flush()
        existing_fixed = session.query(FieldCodeReferenceModel).filter(
            FieldCodeReferenceModel.field_id == field.id
        ).first()
        if existing_fixed:
            session.delete(existing_fixed)
            session.flush()
        return
    if is_rule_reference(text):
        # 格式/编号规则不是码表引用，同理清掉。规则本体留在 check_text 里。
        session.flush()
        existing_rule = session.query(FieldCodeReferenceModel).filter(
            FieldCodeReferenceModel.field_id == field.id
        ).first()
        if existing_rule:
            session.delete(existing_rule)
            session.flush()
        return
    code_set = _find_matching_code_set(
        session,
        text,
        by_name=by_name,
        by_code=by_code,
        by_norm=by_norm,
        sets_with_values=sets_with_values,
        candidate_norms=candidate_norms,
    )
    enum_values = parse_inline_enum(text)
    if not enum_values:
        # 引用摘要里只有「（F0102 内联枚举）」这种标记，枚举值其实在校验原文里：
        # 「按以下两种情况填列：①是；②否」。只看摘要会漏抽，回退到原文再试一次。
        enum_values = parse_inline_enum(field.check_text_raw or "")
    if code_set and enum_values and not _set_has_values(session, code_set, sets_with_values):
        # 命中的是占位码表，但引用原文本身就把码值列出来了（如"1-股票；2-债券…"），
        # 直接补进这张表，避免又建一张重复的"（内联枚举）"表。
        _fill_code_values(session, code_set, enum_values, text)
        enum_values = []
        if sets_with_values is not None:
            sets_with_values.add(code_set.id)
        session.flush()
    elif code_set and enum_values and not _values_overlap(session, code_set, enum_values, sets_with_values):
        # 匹配到的是已有值的码表，但取值和枚举值没有交集——按名字误撞上的，
        # 退回改用字段自己的内联枚举表，不关联。
        code_set = None
    if not code_set:
        if enum_values:
            code = re.sub(r"[^A-Z0-9_]", "_", f"{field.field_en}_INLINE_ENUM".upper())[:96]
            code_set = by_code.get(code) if by_code is not None else session.query(CodeSetModel).filter(CodeSetModel.code == code).first()
            if not code_set:
                code_set = CodeSetModel(
                    id=str(uuid.uuid4()),
                    code=code,
                    name=f"{field.field_cn or field.field_en}（内联枚举）"[:255],
                    definition=f"字段 {field.field_en} 的内联枚举：{text}"[:1000],
                    source_type="INLINE_ENUM",
                    status="AI_SELF_VALIDATED",
                    current_version=1,
                )
                session.add(code_set)
                session.flush()
                _stamp_field_ordinal(session, code_set, field)
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
            elif not _set_has_values(session, code_set, sets_with_values):
                # 之前建过同名的内联枚举表但没写进取值，这里补上
                _fill_code_values(session, code_set, enum_values, text)
                if sets_with_values is not None:
                    sets_with_values.add(code_set.id)
                session.flush()
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
                _stamp_field_ordinal(session, code_set, field)
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
        # 已经标过「外部规范」的引用，这次没匹配到本地码表就保持原样——
        # 不能一重跑对齐就降级回 UNRESOLVED，否则「需要补」和「本地没有」反复横跳。
        # 能匹配到（target=RESOLVED）才照常更新。
        if existing.status == "EXTERNAL" and target_status == "UNRESOLVED":
            return
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
    # 已有引用若已指向"有值码表"，按原引用原文锚定，保证重跑幂等（表名被压缩后仍能命中同一张表）
    by_id = {cs.id: cs for cs in all_sets}
    for ref in existing_ref_by_field.values():
        anchor = by_id.get(ref.code_set_id)
        if anchor is not None and ref.reference_text and anchor.id in sets_with_values:
            by_name.setdefault(ref.reference_text, anchor)
    candidate_norms = [(normalize_code_set_name(cs.name), cs) for cs in all_sets]
    for field in session.query(FieldMetadataModel).all():
        text = (field.code_reference_text or "").strip()
        if (not text or text.upper() in {"无", "N/A", "NONE", "无引用"}) and field.id not in existing_ref_by_field:
            continue
        ensure_field_reference(
            session,
            field,
            by_name=by_name,
            by_code=by_code,
            by_norm=by_norm,
            sets_with_values=sets_with_values,
            existing_ref_by_field=existing_ref_by_field,
            candidate_norms=candidate_norms,
        )

    for ref in session.query(FieldCodeReferenceModel).all():
        ref.status = "RESOLVED" if ref.code_set_id in sets_with_values else "UNRESOLVED"

    session.flush()
    return sum(1 for r in session.query(FieldCodeReferenceModel).all() if r.status == "RESOLVED")


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
