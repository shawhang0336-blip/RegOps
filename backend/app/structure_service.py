import hashlib
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from .asset_models import DocumentArchitectureModel, RawBlockModel, SourceDocumentModel
from .config import Settings
from .db import SessionLocal
from .extractors import extract_document, survey_document
from .generic_ai import RealDesignLlm


def _candidate(block: RawBlockModel) -> Dict[str, Any]:
    return {
        "id": block.id,
        "block_type": block.block_type,
        "source_locator": block.source_locator,
        "raw_preview": block.raw_content[:300],
        "structure": {
            key: value
            for key, value in block.content_json.items()
            if key in {"table_code", "table_name", "field_count", "headers", "rows", "spec", "code_set_spec", "candidate_role", "reason", "eligible"}
        },
    }


def _validate_plan(args: Dict[str, Any], blocks: List[RawBlockModel]) -> Dict[str, Any]:
    known = {block.id for block in blocks}
    used = set()
    for item in args.get("blocks", []):
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


def _apply_plan(session: Session, document: SourceDocumentModel, blocks: List[RawBlockModel], args: Dict[str, Any]) -> None:
    by_id = {block.id: block for block in blocks}
    next_ordinal = max((block.ordinal for block in blocks), default=0) + 1
    for item in args["blocks"]:
        source_ids = item["source_block_ids"]
        if len(source_ids) == 1 and not item["split_markers"]:
            block = by_id[source_ids[0]]
            block.block_type = item["role"]
            block.content_json = {
                **block.content_json,
                "column_mapping": item["column_mapping"],
                "human_required": item["human_required"],
                "structure_warnings": item["warnings"],
                "structure_ai_assigned": True,
                "superseded": item["role"] == "IGNORE",
            }
            continue
        for source_id in source_ids:
            by_id[source_id].content_json = {**by_id[source_id].content_json, "superseded": True}
        if item["split_markers"]:
            remaining = by_id[source_ids[0]].raw_content
            raw_parts = []
            for marker in item["split_markers"]:
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
                    block_type=item["role"],
                    raw_content=part,
                    source_locator={"title": item["title"], "source_block_ids": source_ids},
                    content_json={
                        "source_block_ids": source_ids,
                        "source_blocks": [by_id[source_id].content_json for source_id in source_ids],
                        "column_mapping": item["column_mapping"],
                        "human_required": item["human_required"],
                        "structure_warnings": item["warnings"],
                        "structure_ai_assigned": True,
                        "superseded": item["role"] == "IGNORE",
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
        if document.status == "OVERVIEW_FAILED":
            raise ValueError("Document structure inspection failed; initial chunking is blocked")
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


def analyze_document_structure(document_id: str, settings: Settings) -> str:
    with SessionLocal() as session:
        document = session.get(SourceDocumentModel, document_id)
        if not document:
            raise ValueError("Document not found")
        blocks = [
            block
            for block in session.query(RawBlockModel).filter(RawBlockModel.document_id == document.id).order_by(RawBlockModel.ordinal).all()
            if not block.content_json.get("superseded")
        ]
        if not blocks:
            raise ValueError("Document has no raw blocks")
        structured = [
            block
            for block in blocks
            if (block.content_json.get("eligible") and (block.content_json.get("spec") or block.content_json.get("code_set_spec")))
            or (block.content_json.get("candidate_role") == "CODE_SET" and block.content_json.get("code_set_spec"))
        ]
        ambiguous = [block for block in blocks if block not in structured]
        architecture_id = ""
        if structured:
            args = {"blocks": [_default_plan(block) for block in structured], "global_warnings": []}
            _apply_plan(session, document, structured, args)
            architecture = DocumentArchitectureModel(
                id=str(uuid.uuid4()),
                document_id=document.id,
                status="DETERMINISTIC_STRUCTURE",
                plan_json=args,
                warnings_json=[],
                trace_json={"strategy": "structured table and code_set blocks require no LLM architecture call"},
            )
            session.add(architecture)
            session.commit()
            architecture_id = architecture.id
        if ambiguous:
            groups = {}
            for block in ambiguous:
                section = str(block.source_locator.get("section", "UNKNOWN"))
                groups.setdefault(section, []).append(block)
            try:
                llm = RealDesignLlm(settings)
            except Exception as llm_init_exc:
                llm = None
                init_error = llm_init_exc

            def analyze_group(item):
                section, group = item
                if llm is None:
                    return {
                        "blocks": [_fallback_plan(block, init_error) for block in group],
                        "global_warnings": [f"{section}: {type(init_error).__name__}: {init_error}"],
                    }, {"error": f"{type(init_error).__name__}: {init_error}"}, None
                candidates = [_candidate(block) for block in group]
                try:
                    args, trace = llm.assign(
                        "assign_document_structure",
                        "先看这一章节原始块的整体结构，再决定保留为 TABLE_METADATA 或 CODE_SET、忽略、合并、拆分及列映射。每个章节最多一次调用，不得虚构字段。",
                        {"file_name": document.file_name, "file_type": document.file_type, "candidate_blocks": candidates},
                        timeout=25,
                    )
                    return args, trace, _validate_plan(args, group)
                except Exception as exc:
                    return {
                        "blocks": [_fallback_plan(block, exc) for block in group],
                        "global_warnings": [f"{section}: {type(exc).__name__}: {exc}"],
                    }, {"error": f"{type(exc).__name__}: {exc}"}, None

            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(analyze_group, groups.items()))
            for section, (args, trace, _) in zip(groups.keys(), results):
                _apply_plan(session, document, groups[section], args)
                architecture = DocumentArchitectureModel(
                    id=str(uuid.uuid4()),
                    document_id=document.id,
                    status="AI_STRUCTURE_REVIEWED" if "error" not in trace else "AI_STRUCTURE_NEEDS_REVIEW",
                    plan_json=args,
                    warnings_json=args.get("global_warnings", []),
                    trace_json=trace,
                )
                session.add(architecture)
                session.commit()
                architecture_id = architecture.id
        return architecture_id
