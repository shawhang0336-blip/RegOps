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
    # "国际收支交易编码表"与"涉外收支交易分类与代码"是同一张 6 位交易代码表的两种叫法
    "国际收支交易": "交易",
    "国际收支交易编码": "交易",
    "涉外收支交易": "交易",
    "涉外收支交易分类": "交易",
    "涉外收支交易分类与代码": "交易",
    "对方与本机构的关系": "对方与本机构被代理居民机构委托人的关系",
    "对方与本机构被代理居民机构委托人的关系": "对方与本机构被代理居民机构委托人的关系",
    "外债的债务类型": "债务类型",
}


CIRCLED_NUM = "①②③④⑤⑥⑦⑧⑨⑩"
CIRCLED_INDEX = {char: str(index + 1) for index, char in enumerate(CIRCLED_NUM)}

# 内联枚举里的噪声片段：默认值说明、填写要求等。原文常写成
# "Y-是；N-否；默认N"，旧的整段解析会因为这个片段而全盘放弃。
ENUM_NOISE_RE = re.compile(r"^(?:默认|默认为|不填|留空|可选|必填|填写|请填|说明|注[：:]|见[：:])")
_ENUM_TOKENS = r"A-Za-z0-9" + CIRCLED_NUM
_ENUM_FORWARD_RE = re.compile(rf"^([{_ENUM_TOKENS}]+)\s*[\-—–－:：、]\s*(.+)$")
_ENUM_GLUED_RE = re.compile(rf"^([{_ENUM_TOKENS}]{{1,6}})([^\x00-\x7F].*)$")
_ENUM_REVERSE_RE = re.compile(rf"^(.+?)\s*[\-—–－:：、]\s*([{_ENUM_TOKENS}]+)$")


def _split_enum_fragments(text: str) -> List[str]:
    """按圆圈序号或中英文分隔符切成候选片段，丢弃序号之前的说明文字。"""
    raw = (text or "").strip()
    if not raw:
        return []
    positions = [index for index, char in enumerate(raw) if char in CIRCLED_INDEX]
    if len(positions) >= 2:
        # "按九个子部门码值填列：①政府；②央行…" → 从第一个序号开始切
        body = raw[positions[0]:]
        return [part.strip() for part in re.split(rf"(?=[{CIRCLED_NUM}])", body) if part.strip()]
    return [part.strip() for part in re.split(r"[；;，,、\n]", raw) if part.strip()]


def _parse_enum_pair(part: str) -> Optional[Tuple[str, str]]:
    """把单个片段解析成 (码值, 名称)；不是"码-值"结构就返回 None。"""
    text = (part or "").strip().strip("。.;；,，、:： ")
    if not text:
        return None
    match = _ENUM_FORWARD_RE.match(text) or _ENUM_GLUED_RE.match(text)
    if match:
        code, label = match.group(1).strip(), match.group(2).strip()
    else:
        match = _ENUM_REVERSE_RE.match(text)
        if not match:
            return None
        label, code = match.group(1).strip(), match.group(2).strip()
    label = label.strip("。.;；,，、 ")
    if not code or not label or code == label:
        return None
    if len(code) > 8 or len(label) > 80:
        return None
    if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", label):
        return None
    return CIRCLED_INDEX.get(code, code)[:20], label[:80]


_ENUM_LIST_RE = re.compile(r"\[([^\[\]]{4,2000})\]")


def _parse_enum_list(text: str) -> List[Dict[str, str]]:
    """解析 AI 复核给出的 ENUM：["上市普通股","非上市普通股（但可交易）"] 这种形式。"""
    raw = (text or "").strip()
    if not raw:
        return []
    if not re.search(r"\bENUM\b", raw, re.IGNORECASE) and not raw.startswith("["):
        return []
    match = _ENUM_LIST_RE.search(raw)
    if not match:
        return []
    labels = re.findall(r"[\"“]([^\"”]{1,80})[\"”]", match.group(1))
    if len(labels) < 2:
        return []
    return [
        {"value": str(index + 1), "label_cn": label.strip()[:80]}
        for index, label in enumerate(labels)
        if label.strip()
    ]


def parse_inline_enum(text: str) -> List[Dict[str, str]]:
    """把 "Y-是；N-否" / "①政府；②央行" / "是-Y，否-N" 这类内联枚举解析成码值对；解析失败返回空。

    逐片段解析：个别片段（如"默认N""填写N"）解析不出来只跳过它，不再整段放弃；
    至少要有 2 个有效码值才认为是枚举，避免把普通句子误判成码表。
    """
    listed = _parse_enum_list(text)
    if listed:
        return listed
    values: List[Dict[str, str]] = []
    seen = set()
    for part in _split_enum_fragments(text):
        if ENUM_NOISE_RE.match(part):
            continue
        pair = _parse_enum_pair(part)
        if pair is None and re.search(r"[：:]", part):
            # "交易方式枚举：1-线下" —— 去掉说明前缀再试一次
            pair = _parse_enum_pair(re.split(r"[：:]", part)[-1])
        if pair is None:
            continue
        code, label = pair
        if code in seen:
            continue
        seen.add(code)
        values.append({"value": code, "label_cn": label})
    if len(values) < 2:
        return []
    return values[:200]


def normalize_code_set_name(raw_name: str) -> str:
    if not raw_name:
        return ""
    text = raw_name.strip()
    # 先去掉括号及内容（如"（3位字母代码）"），避免其污染核心名
    text = re.sub(r"[（(][^）)]*[）)]", "", text)
    text = re.sub(
        r"^(?:参?见|参照|本制度|数据采集规范最新版本中的相关代码表或本制度|数据采集规范(?:最新版本)?(?:中|中的)?的?)+",
        "",
        text,
    )
    # 去掉"附录"残留（括号在上一步已移除）
    text = re.sub(r"(?:五、附录|附[录表])", "", text)
    # "…代码表填列"这类尾巴：先摘掉"填列/填报"，否则后面的"代码表"剥离会被挡住
    text = re.sub(r"(?:填写|填报|填列|填制|填用)$", "", text)
    text = re.sub(r"(?:代码表|分类标准表|分类标准|代码|名录|表|填写|填)+$", "", text)
    # 剥离后缀后可能留下连接词（"涉外收支交易分类与代码" → "…分类与"）
    text = re.sub(r"[与和及或等的]+$", "", text)
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
    for value in session.query(CodeValueModel).filter(CodeValueModel.code_set_id.in_(code_set_ids)).all():
        block_id = (value.source_locator or {}).get("block_id")
        if block_id in block_ordinal:
            located.add(value.code_set_id)
            candidate = block_ordinal[block_id] * 1000
            if candidate < ordinals.get(value.code_set_id, candidate + 1):
                ordinals[value.code_set_id] = candidate
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
    if code_set and enum_values and not _set_has_values(session, code_set, sets_with_values):
        # 命中的是占位码表，但引用原文本身就把码值列出来了（如"1-股票；2-债券…"），
        # 直接补进这张表，避免又建一张重复的"（内联枚举）"表。
        _fill_code_values(session, code_set, enum_values, text)
        enum_values = []
        if sets_with_values is not None:
            sets_with_values.add(code_set.id)
        session.flush()
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
            candidate_norms=candidate_norms,
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

    # 记下码表在原文里的位置，左侧列表才能按出现顺序排（×1000 给同块内留出插入空间）
    if row.source_ordinal is None and locator.get("block_id"):
        source_block = session.get(RawBlockModel, locator["block_id"])
        if source_block is not None:
            row.source_ordinal = source_block.ordinal * 1000
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
    on_progress: Optional[Any] = None,
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

        for completed, (bid, future) in enumerate(meta_futures.items(), start=1):
            try:
                table_id = future.result()
                if table_id:
                    table_ids.append(table_id)
            except Exception as exc:
                errors.append({"block_id": bid, "track": "metadata", "error": f"{type(exc).__name__}: {exc}"})
            if callable(on_progress):
                try:
                    on_progress(completed, len(meta_futures), len(table_ids))
                except Exception:
                    pass

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
