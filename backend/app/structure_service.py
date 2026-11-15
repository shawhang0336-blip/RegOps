import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .asset_models import DocumentArchitectureModel, RawBlockModel, SourceDocumentModel
from .config import Settings
from .db import SessionLocal
from .extractors import extract_document, survey_document
from .generic_ai import RealDesignLlm


def _spec_digest(spec: Dict[str, Any]) -> Dict[str, Any]:
    """把 spec 压成摘要：整份 spec 有几十个字段，塞进 prompt 会直接撑爆上下文。"""
    if not spec:
        return {}
    fields = spec.get("fields") or []
    return {
        "table_code": spec.get("table_code"),
        "table_name": spec.get("table_name"),
        "field_count": len(fields),
        "sample_fields": [f.get("name") for f in fields[:8]],
        "sample_types": [f"{f.get('data_type')}({f.get('length')})" for f in fields[:5]],
    }


def _candidate(block: RawBlockModel) -> Dict[str, Any]:
    structure = {
        key: value
        for key, value in block.content_json.items()
        if key in {"table_code", "table_name", "field_count", "headers", "rows", "candidate_role", "reason", "eligible"}
    }
    spec = block.content_json.get("spec") or block.content_json.get("parent_spec") or {}
    if spec:
        structure["parsed_spec"] = _spec_digest(spec)
    code_spec = block.content_json.get("code_set_spec") or {}
    if code_spec:
        structure["parsed_code_set"] = {
            "code": code_spec.get("code"),
            "name": code_spec.get("name"),
            "value_count": len(code_spec.get("values") or []),
        }
    return {
        "id": block.id,
        "block_type": block.block_type,
        "source_locator": block.source_locator,
        "raw_preview": block.raw_content[:400],
        "structure": structure,
    }


def _is_structured(block: RawBlockModel) -> bool:
    content = block.content_json or {}
    return bool(
        (content.get("eligible") and (content.get("spec") or content.get("code_set_spec")))
        or (content.get("candidate_role") == "CODE_SET" and content.get("code_set_spec"))
        or content.get("parent_spec")
    )


def _validate_structured_guard(args: Dict[str, Any], blocks: List[RawBlockModel]) -> None:
    """已经解析出结构的块不能被 AI 随意合并/拆分/改角色。

    合并多个已解析块会丢掉其中一份字段结构，拆分则要重新解析文本，
    两者都会把"已经确定的字段"退回"待推测"，所以这里直接拒绝，
    由调用方回退到默认计划。
    """
    structured = {block.id for block in blocks if _is_structured(block)}
    if not structured:
        return
    for item in args.get("blocks", []):
        ids = set(item.get("source_block_ids", []))
        hits = ids & structured
        if not hits:
            continue
        if len(ids) > 1 and len(hits) > 1:
            raise ValueError("AI 计划试图合并多个已解析结构的块，会丢失字段结构")
        if item.get("split_markers"):
            raise ValueError("AI 计划试图切分已解析结构的块")
        if item.get("role") not in {"TABLE_METADATA", "CODE_SET"}:
            raise ValueError(f"AI 计划把已解析结构的块标成了 {item.get('role')}")


def _validate_plan(args: Dict[str, Any], blocks: List[RawBlockModel]) -> Dict[str, Any]:
    known = {block.id for block in blocks}
    used = set()
    for item in args.get("blocks", []):
        # 模型的输出不一定填满 schema，先把缺省值补齐，避免后面按 key 取值时崩掉
        item.setdefault("split_markers", [])
        item.setdefault("warnings", [])
        item.setdefault("column_mapping", {})
        item.setdefault("human_required", False)
        item.setdefault("role", "NARRATIVE")
        item.setdefault("title", "未命名章节")
        item.setdefault("source_block_ids", [])
        ids = set(item.get("source_block_ids", []))
        if not ids or not ids.issubset(known):
            raise ValueError("Structure plan references an unknown source block")
        if used.intersection(ids):
            raise ValueError("Structure plan assigns a source block more than once")
        used.update(ids)
        if item.get("split_markers") and len(ids) != 1:
            raise ValueError("Split markers require exactly one source block")
    if used != known:
        raise ValueError("Structure plan does not cover every source block")
    return args


def _default_plan(block: RawBlockModel) -> Dict[str, Any]:
    is_code_set = (
        block.block_type in {"CODE_SET", "CODE_SET_CANDIDATE"}
        or block.content_json.get("candidate_role") == "CODE_SET"
        or bool(block.content_json.get("code_set_spec"))
    )
    if is_code_set:
        return {
            "source_block_ids": [block.id],
            "role": "CODE_SET",
            "title": block.content_json.get("table_name") or block.source_locator.get("section", "结构化码值表"),
            "column_mapping": block.content_json.get("column_mapping")
            or {
                "value": "value",
                "label_cn": "label_cn",
                "label_en": "label_en",
                "definition": "definition",
            },
            "human_required": False,
            "warnings": [],
            "split_markers": [],
        }
    if block.block_type in {"RULE_EVIDENCE", "REVISION_DIFF"}:
        return {
            "source_block_ids": [block.id],
            "role": block.block_type,
            "title": block.content_json.get("table_name") or block.source_locator.get("section", "规则证据"),
            "column_mapping": {},
            "human_required": False,
            "warnings": ["该块仅作为规则或版本差异证据，不得单独生成完整建表 DDL"],
            "split_markers": [],
        }
    return {
        "source_block_ids": [block.id],
        "role": "TABLE_METADATA",
        "title": block.source_locator.get("section", "结构化字段表"),
        "column_mapping": block.content_json.get("column_mapping")
        or {
            "name": "field_en",
            "content": "field_cn",
            "check_text": "check_text_raw",
            "data_type": "data_type",
            "length": "length",
        },
        "human_required": bool(block.content_json.get("human_required")),
        "warnings": list(block.content_json.get("parse_warnings") or []),
        "split_markers": [],
    }


def _fallback_plan(block: RawBlockModel, error: Exception) -> Dict[str, Any]:
    return {
        "source_block_ids": [block.id],
        "role": "NARRATIVE",
        "title": "需要人工确认结构的原始块",
        "column_mapping": {},
        "human_required": True,
        "warnings": [f"AI 架构预检失败：{type(error).__name__}: {error}"],
        "split_markers": [],
    }


def _inherit_spec(by_id: Dict[str, RawBlockModel], source_ids: List[str]) -> Dict[str, Any]:
    """合并块时保留结构化解析结果。

    合并两个块会把各自的 spec 埋进 source_blocks 里，下游只读 spec/parent_spec，
    所以这里把唯一的那份提升上来——否则"AI 判断这两块是同一张表"就会把
    已经解析好的字段结构一起丢掉。
    """
    specs = [
        by_id[sid].content_json.get("spec")
        for sid in source_ids
        if by_id.get(sid) and by_id[sid].content_json.get("spec")
    ]
    if len(specs) == 1:
        merged = dict(specs[0])
        merged["merged_from"] = source_ids
        return merged
    return {}


def _apply_plan(session: Session, document: SourceDocumentModel, blocks: List[RawBlockModel], args: Dict[str, Any]) -> None:
    by_id = {block.id: block for block in blocks}
    next_ordinal = max((block.ordinal for block in blocks), default=0) + 1
    for item in args["blocks"]:
        source_ids = list(item.get("source_block_ids") or [])
        role = item.get("role") or "NARRATIVE"
        title = item.get("title") or "未命名章节"
        column_mapping = item.get("column_mapping") or {}
        human_required = bool(item.get("human_required"))
        warnings = item.get("warnings") or []
        split_markers = item.get("split_markers") or []
        if not source_ids:
            continue
        if len(source_ids) == 1 and not split_markers:
            block = by_id[source_ids[0]]
            block.block_type = role
            block.content_json = {
                **block.content_json,
                "column_mapping": column_mapping,
                "human_required": human_required,
                "structure_warnings": warnings,
                "structure_ai_assigned": True,
                "superseded": role == "IGNORE",
            }
            continue
        for source_id in source_ids:
            by_id[source_id].content_json = {**by_id[source_id].content_json, "superseded": True}
        if split_markers:
            remaining = by_id[source_ids[0]].raw_content
            raw_parts = []
            for marker in split_markers:
                before, separator, after = remaining.partition(marker)
                if before.strip():
                    raw_parts.append(before.strip())
                if not separator:
                    raise ValueError(f"Split marker not found: {marker}")
                remaining = after
            if remaining.strip():
                raw_parts.append(remaining.strip())
        else:
            raw_parts = ["\n\n--- ORIGINAL BLOCK BOUNDARY ---\n\n".join(by_id[source_id].raw_content for source_id in source_ids)]
        for part in raw_parts:
            session.add(
                RawBlockModel(
                    id=str(uuid.uuid4()),
                    document_id=document.id,
                    ordinal=next_ordinal,
                    block_type=role,
                    raw_content=part,
                    source_locator={"title": title, "source_block_ids": source_ids},
                    content_json={
                        "source_block_ids": source_ids,
                        "source_blocks": [by_id[source_id].content_json for source_id in source_ids],
                        "parent_spec": _inherit_spec(by_id, source_ids),
                        "column_mapping": column_mapping,
                        "human_required": human_required,
                        "structure_warnings": warnings,
                        "structure_ai_assigned": True,
                        "superseded": role == "IGNORE",
                    },
                    raw_hash=hashlib.sha256(part.encode("utf-8")).hexdigest(),
                )
            )
            next_ordinal += 1
    session.flush()
    document.status = "STRUCTURE_REVIEWED"
    document.block_count = sum(
        1 for block in session.query(RawBlockModel).filter(RawBlockModel.document_id == document.id).all()
        if not block.content_json.get("superseded")
    )


def latest_document_architecture(session, document_id: str, statuses=None):
    query = session.query(DocumentArchitectureModel.id, DocumentArchitectureModel.created_at).filter(
        DocumentArchitectureModel.document_id == document_id
    )
    if statuses:
        query = query.filter(DocumentArchitectureModel.status.in_(statuses))
    rows = query.all()
    if not rows:
        return None
    latest_id = max(rows, key=lambda row: (row.created_at, row.id)).id
    return session.get(DocumentArchitectureModel, latest_id)


def inspect_document_overview(document_id: str, settings: Settings, force: bool = False) -> str:
    with SessionLocal() as session:
        document = session.get(SourceDocumentModel, document_id)
        if not document:
            raise ValueError("Document not found")
        latest = latest_document_architecture(
            session,
            document_id,
            statuses={"OVERVIEW_REVIEWED", "OVERVIEW_NEEDS_REVIEW", "OVERVIEW_FAILED"},
        )
        if latest and not force:
            return latest.id

        survey = {}
        try:
            source_path = Path(settings.blob_dir) / "uploads" / f"{document.sha256}.{document.file_type}"
            survey = survey_document(source_path, document.file_type)
            llm = RealDesignLlm(settings)
            result, trace = llm.assign(
                "inspect_document_structure",
                "在初步切块前检查整份文件的目录、章节、表格或工作表结构。判断文档类型、包含内容和适合的切块方式；只输出结构结论，不提取字段，不得虚构。",
                {"file_name": document.file_name, "survey": survey},
                timeout=60,
            )
            warnings = list(result.get("warnings") or [])
            expected_strategy = survey["available_chunk_strategy"]
            if result.get("chunking_strategy") not in {expected_strategy, "MANUAL_REVIEW"}:
                warnings.append("AI 建议的切块方式与文件类型不一致，程序将使用文件类型对应的安全切块方式")
                result["requires_human"] = True
            result["chunking_strategy"] = expected_strategy
            result["warnings"] = warnings
            status = "OVERVIEW_NEEDS_REVIEW" if result.get("requires_human") else "OVERVIEW_REVIEWED"
        except Exception as exc:
            result = {}
            trace = {"error": f"{type(exc).__name__}: {exc}"}
            status = "OVERVIEW_FAILED"
            warnings = [f"整份文件结构检查未完成：{type(exc).__name__}: {exc}"]

        architecture = DocumentArchitectureModel(
            id=str(uuid.uuid4()),
            document_id=document.id,
            status=status,
            plan_json={"stage": "DOCUMENT_OVERVIEW", "survey": survey, "result": result},
            warnings_json=warnings,
            trace_json=trace,
        )
        session.add(architecture)
        document.status = status
        session.commit()
        return architecture.id


def reextract_document_blocks(document_id: str, settings: Settings) -> int:
    with SessionLocal() as session:
        document = session.get(SourceDocumentModel, document_id)
        retry_failed_overview = bool(document and document.status == "OVERVIEW_FAILED")
    inspect_document_overview(document_id, settings, force=retry_failed_overview)
    with SessionLocal() as session:
        document = session.get(SourceDocumentModel, document_id)
        if not document:
            raise ValueError("Document not found")
        # 体检失败不再阻断切块：切块按文件类型走确定性路径，不依赖体检结论
        source_path = Path(settings.blob_dir) / "uploads" / f"{document.sha256}.{document.file_type}"
        if not source_path.is_file():
            raise ValueError("Original document blob not found")
        _, new_blocks = extract_document(source_path, document.file_type)
        existing = session.query(RawBlockModel).filter(RawBlockModel.document_id == document.id).all()
        for block in existing:
            block.content_json = {**block.content_json, "superseded": True}
        next_ordinal = max((block.ordinal for block in existing), default=-1) + 1
        for block in new_blocks:
            block["ordinal"] = next_ordinal
            next_ordinal += 1
            session.add(RawBlockModel(id=str(uuid.uuid4()), document_id=document.id, **block))
        document.status = "UPLOADED"
        document.block_count = len(new_blocks)
        session.commit()
        return len(new_blocks)


STRUCTURE_PLAN_TIMEOUT = 40


def _overview_context(session: Session, document_id: str) -> Dict[str, Any]:
    """把文档级结构体检的结论带进切块阶段，AI 才知道自己在处理什么文档。"""
    latest = latest_document_architecture(
        session,
        document_id,
        statuses={"OVERVIEW_REVIEWED", "OVERVIEW_NEEDS_REVIEW", "OVERVIEW_FAILED"},
    )
    if not latest:
        return {}
    plan = latest.plan_json or {}
    result = plan.get("result") or {}
    survey = plan.get("survey") or {}
    return {
        "document_kind": result.get("document_kind"),
        "summary": result.get("summary"),
        "expected_contents": result.get("expected_contents"),
        "chunking_strategy": result.get("chunking_strategy"),
        "page_count": survey.get("page_count"),
        "headings": (survey.get("headings") or [])[:40],
    }


def _default_group_plan(group: List[RawBlockModel], error: Optional[Exception] = None) -> Dict[str, Any]:
    plans = []
    for block in group:
        if _is_structured(block):
            plans.append(_default_plan(block))
        else:
            plans.append(_fallback_plan(block, error or RuntimeError("结构决策不可用")))
    return {"blocks": plans, "global_warnings": []}


def _group_needs_ai(group: List[RawBlockModel], scope: str) -> bool:
    """全量模式让 AI 审每个块；自适模式只把"有决策空间"的组交给 AI。"""
    if scope == "all":
        return True
    if len(group) > 1:
        return True  # 多块同章节：可能是跨页断开的同一张表，也可能是混杂内容
    if any(not _is_structured(block) for block in group):
        return True  # 没解析出结构的块，需要 AI 判断它到底是什么
    # 注意：不要用 structure_warnings 作为触发条件。_apply_plan 每次都会回写这个字段，
    # 一旦写了就会让下次重跑再次进来，变成自我触发的无限调用。
    return False


def analyze_document_structure(document_id: str, settings: Settings) -> str:
    scope = str(getattr(settings, "structure_ai_scope", "adaptive") or "adaptive")
    with SessionLocal() as session:
        document = session.get(SourceDocumentModel, document_id)
        if not document:
            raise ValueError("Document not found")
        blocks = [
            block
            for block in session.query(RawBlockModel)
            .filter(RawBlockModel.document_id == document.id)
            .order_by(RawBlockModel.ordinal)
            .all()
            if not block.content_json.get("superseded")
        ]
        if not blocks:
            raise ValueError("Document has no raw blocks")

        overview = _overview_context(session, document.id)
        groups: Dict[str, List[RawBlockModel]] = {}
        for block in blocks:
            section = str(
                block.source_locator.get("section")
                or block.content_json.get("table_code")
                or f"#{block.ordinal}"
            )
            groups.setdefault(section, []).append(block)

        try:
            llm = RealDesignLlm(settings)
            llm_error: Optional[Exception] = None
        except Exception as exc:  # noqa: BLE001
            llm = None
            llm_error = exc

        def plan_group(item):
            section, group = item
            if llm is None:
                return _default_group_plan(group, llm_error), {"error": f"{type(llm_error).__name__}: {llm_error}"}, "DEFAULT"
            if not _group_needs_ai(group, scope):
                return _default_group_plan(group), {"skipped": "结构化块无需重新决策"}, "DEFAULT"
            candidates = [_candidate(block) for block in group]
            try:
                args, trace = llm.assign(
                    "assign_document_structure",
                    (
                        "你在决定这份监管文档如何切成可解析的单元。根据章节原文和已解析结构，"
                        "判断每个块的 role（TABLE_METADATA/CODE_SET/NARRATIVE/IGNORE），"
                        "跨页断开的同一张表用多个 source_block_ids 合并，混杂多种内容的块用 split_markers 拆分，"
                        "并给出准确的 title 和 column_mapping。"
                        "已经解析出字段结构的块（parsed_spec）只允许保留或改角色，不得拆分。"
                        "不得虚构块内容，必须覆盖本章节的全部候选块。"
                    ),
                    {
                        "file_name": document.file_name,
                        "file_type": document.file_type,
                        "document_overview": overview,
                        "section": section,
                        "candidate_blocks": candidates,
                    },
                    timeout=STRUCTURE_PLAN_TIMEOUT,
                )
                _validate_plan(args, group)
                _validate_structured_guard(args, group)
                return args, trace, "AI"
            except Exception as exc:  # noqa: BLE001
                return _default_group_plan(group, exc), {"error": f"{type(exc).__name__}: {exc}"}, "DEFAULT"

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(plan_group, groups.items()))

        architecture_id = ""
        for (section, group), (args, trace, mode) in zip(groups.items(), results):
            _apply_plan(session, document, group, args)
            architecture = DocumentArchitectureModel(
                id=str(uuid.uuid4()),
                document_id=document.id,
                status="AI_STRUCTURE_REVIEWED" if mode == "AI" else "DETERMINISTIC_STRUCTURE",
                plan_json=args,
                warnings_json=args.get("global_warnings", []),
                trace_json={**trace, "mode": mode, "section": section, "scope": scope},
            )
            session.add(architecture)
            session.commit()
            architecture_id = architecture.id
        return architecture_id
