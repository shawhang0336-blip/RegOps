import hashlib
import uuid
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from app.infra.db_tables import (
    DocumentArchitectureModel,
    FieldMetadataModel,
    MetadataRevisionModel,
    MetadataValidationModel,
    RawBlockModel,
    SourceDocumentModel,
    TableMetadataModel,
)
from app.infra.config import Settings
from app.infra.db import SessionLocal
from app.infra.llm import RealDesignLlm


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


def merge_metadata_tables(session: Session, parent_id: str, table_ids: List[str]) -> str:
    tables = session.query(TableMetadataModel).filter(TableMetadataModel.id.in_(table_ids)).all()
    if not tables:
        raise ValueError("No metadata tables to merge")
    priority = {"NARRATIVE": 0, "TERMINOLOGY": 1, "XML_FORMAT": 2, "CODE_VALUES": 2, "PRIMARY_KEY": 2, "DATA_DICTIONARY": 3}
    blocks = {block.id: block for block in session.query(RawBlockModel).filter(RawBlockModel.id.in_([table.block_id for table in tables])).all()}
    tables.sort(key=lambda table: priority.get(blocks[table.block_id].block_type, 0))
    canonical = tables[-1]
    merged = {}
    for table in tables:
        block = blocks[table.block_id]
        fields = session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == table.id).order_by(FieldMetadataModel.sort_order).all()
        for field in fields:
            values = {key: getattr(field, key) for key in [
                "field_cn", "field_en", "field_definition", "check_text_raw", "data_type", "length", "decimal_places", "required", "code_reference_text", "confidence", "ai_value_json"
            ]}
            values["evidence_json"] = {
                **field.evidence_json,
                "segments": (field.evidence_json.get("segments") or []) + [{"block_id": block.id, "segment_type": block.block_type}],
            }
            merged[field.field_en.upper()] = values
    for field in session.query(FieldMetadataModel).filter(FieldMetadataModel.table_id == canonical.id).all():
        session.delete(field)
    session.flush()
    for index, values in enumerate(merged.values()):
        session.add(FieldMetadataModel(id=str(uuid.uuid4()), table_id=canonical.id, sort_order=index, **values))
    for table in tables:
        if table.id != canonical.id:
            table.status = "MERGED"
            session.query(MetadataValidationModel).filter(MetadataValidationModel.table_id == table.id).update({"table_id": canonical.id})
            session.query(MetadataRevisionModel).filter(MetadataRevisionModel.table_id == table.id).update({"table_id": canonical.id})
    canonical.block_id = parent_id
    canonical.status = "NEEDS_REVIEW" if any(table.status == "NEEDS_REVIEW" for table in tables) else "AI_SELF_VALIDATED"
    session.flush()
    return canonical.id
