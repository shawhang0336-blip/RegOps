import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.infra.db_tables import DocumentArchitectureModel, RawBlockModel, SourceDocumentModel
from app.infra.config import Settings
from app.infra.db import SessionLocal
from app.ingestion.parsers.extractors import extract_document, survey_document
from app.infra.llm import RealDesignLlm
from app.infra.llm.guard import call_llm_validated, current_llm_bus, submit_with_context
from app.policy.regops_bus import default_bus


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
            result, trace = call_llm_validated(
                llm,
                "inspect_document_structure",
                "在初步切块前检查整份文件的目录、章节、表格或工作表结构。判断文档类型、包含内容和适合的切块方式；只输出结构结论，不提取字段，不得虚构。",
                {"file_name": document.file_name, "survey": survey},
                timeout=60,
                backoff=(1.0,),
                bus=current_llm_bus() or default_bus(),
                transient_attempts=2,
                validation_attempts=2,
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

        active_bus = current_llm_bus() or default_bus()

        def plan_group(item):
            section, group = item
            if llm is None:
                return _default_group_plan(group, llm_error), {"error": f"{type(llm_error).__name__}: {llm_error}"}, "DEFAULT"
            if not _group_needs_ai(group, scope):
                return _default_group_plan(group), {"skipped": "结构化块无需重新决策"}, "DEFAULT"
            candidates = [_candidate(block) for block in group]
            payload = {
                "file_name": document.file_name,
                "file_type": document.file_type,
                "document_overview": overview,
                "section": section,
                "candidate_blocks": candidates,
            }
            def _validate_structure_args(args):
                validated = _validate_plan(args, group)
                _validate_structured_guard(validated, group)
                return validated

            try:
                args, trace = call_llm_validated(
                    llm,
                    "assign_document_structure",
                    (
                        "你在决定这份监管文档如何切成可解析的单元。按下面的清单逐项检查候选块，"
                        "发现问题才动手，没发现问题就原样返回。\n"
                        "1. 跨页断裂：某块结尾明显没写完（最后一行缺列、句子中断），或下一块开头是同一张表的重复表头"
                        "（字段/内容/类型（长度）/校验）→ 用多个 source_block_ids 合并成一块。\n"
                        "2. 内容混杂：一个块里同时有指标解释段落和字段表、或同时装了多张不同的表 → "
                        "用 split_markers 在分界文本处切开（marker 必须是原文里逐字出现的片段）。\n"
                        "3. 角色判断错误：块内容其实是码表（成对的值-名称、没有类型列）→ role=CODE_SET；"
                        "是校验规则说明或纯叙述 → NARRATIVE；是封面/目录/页眉/修订记录 → IGNORE。\n"
                        "4. 标题不准：parsed_spec 或首行已经给出表号表名，但 title 是空的或写错 → 用原文的准确标题。\n"
                        "硬约束：已解析出字段结构的块（parsed_spec）只能保留或改角色，不得拆分；"
                        "不得虚构任何块内容；必须覆盖本章节全部候选块。\n"
                        "重要：如果没有发现上述任一问题，就把每个块原样返回（role/title 保持不变），"
                        "不要为了\"看起来更整齐\"而合并、拆分或改名。"
                    ),
                    payload,
                    _validate_structure_args,
                    timeout=STRUCTURE_PLAN_TIMEOUT,
                    backoff=(1.0,),
                    bus=active_bus,
                    transient_attempts=2,
                    validation_attempts=2,
                )
                return args, trace, "AI"
            except Exception as exc:  # noqa: BLE001
                return _default_group_plan(group, exc), {"error": f"{type(exc).__name__}: {exc}"}, "DEFAULT"

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [submit_with_context(executor, plan_group, item) for item in groups.items()]
            results = [f.result() for f in futures]

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


def _split_segments(raw_content: str, args: Dict[str, Any]) -> List[Dict[str, Any]]:
    segments = args.get("segments", [])
    if not segments:
        raise ValueError("No semantic segments returned")
    results = []
    cursor = 0
    markers = set()
    for segment in segments:
        marker = segment["start_marker"].strip()
        if not marker:
            raise ValueError("Empty segment marker")
        if marker in markers:
            raise ValueError("Duplicate segment marker")
        markers.add(marker)
        start = raw_content.find(marker, cursor)
        if start < 0:
            raise ValueError(f"Segment marker not found: {marker}")
        if start < cursor:
            raise ValueError("Segment markers are out of order")
        if start > cursor and not results:
            prefix = raw_content[:start].strip()
            if prefix:
                results.append({"segment_type": "NARRATIVE", "title": "块前说明", "content": prefix, "requires_human": False, "warnings": []})
        results.append({**segment, "content": raw_content[start:], "_start": start})
        cursor = start
    final = []
    for index, segment in enumerate(results):
        start = segment.pop("_start")
        end = results[index + 1]["_start"] if index + 1 < len(results) else len(raw_content)
        content = raw_content[start:end].strip()
        if content:
            final.append({**segment, "content": content})
    return final


def analyze_block_segments(block_id: str, settings: Settings, llm=None) -> List[str]:
    """对保留下来的混装大块做块内细分（术语 / XML / 数据字典 / 码值 / 主键），拆出子块并保留父块证据链。"""
    with SessionLocal() as session:
        parent = session.get(RawBlockModel, block_id)
        if not parent or parent.content_json.get("superseded"):
            raise ValueError("Active parent block not found")
        if parent.content_json.get("segmented"):
            return parent.content_json["segment_ids"]
        llm = llm or RealDesignLlm(settings)
        try:
            args, trace = llm.assign(
                "assign_block_segments",
                "分析保留块内部结构，按术语、XML、数据字典、码值、主键和说明给出 start_marker/end_marker，供程序切块。标记需要人工确认的部分，不得虚构字段。",
                {"source_locator": parent.source_locator, "content": parent.content_json, "raw_content": parent.raw_content},
            )
            segments = _split_segments(parent.raw_content, args)
        except Exception as exc:
            trace = {"error": f"{type(exc).__name__}: {exc}"}
            segments = [
                {
                    "segment_type": "NARRATIVE",
                    "title": "块内结构需人工确认",
                    "content": parent.raw_content,
                    "requires_human": True,
                    "warnings": [trace["error"]],
                }
            ]
        parent_spec = parent.content_json.get("spec")
        segment_ids = []
        for index, segment in enumerate(segments):
            raw = segment["content"]
            segment_id = str(uuid.uuid4())
            segment_ids.append(segment_id)
            session.add(
                RawBlockModel(
                    id=segment_id,
                    document_id=parent.document_id,
                    ordinal=parent.ordinal * 1000 + index,
                    block_type=segment["segment_type"],
                    raw_content=raw,
                    source_locator={**parent.source_locator, "parent_block_id": parent.id, "segment_index": index, "title": segment["title"]},
                    content_json={
                        "parent_block_id": parent.id,
                        "parent_spec": parent_spec,
                        "segment_type": segment["segment_type"],
                        "column_mapping": parent.content_json.get("column_mapping", {}),
                        "human_required": segment["requires_human"],
                        "structure_warnings": segment["warnings"],
                        "superseded": segment["segment_type"] == "IGNORE",
                    },
                    raw_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                )
            )
        parent.content_json = {**parent.content_json, "segmented": True, "segment_ids": segment_ids, "superseded": True}
        parent_document = session.get(SourceDocumentModel, parent.document_id)
        parent_document.block_count = len(segments)
        architecture = DocumentArchitectureModel(
            id=str(uuid.uuid4()),
            document_id=parent.document_id,
            status="BLOCK_SEGMENTED",
            plan_json={"parent_block_id": parent.id, "segments": [{"id": segment_id, "type": segment["segment_type"], "title": segment["title"]} for segment_id, segment in zip(segment_ids, segments)]},
            warnings_json=[warning for segment in segments for warning in segment["warnings"]],
            trace_json=trace,
        )
        session.add(architecture)
        session.commit()
        return segment_ids
