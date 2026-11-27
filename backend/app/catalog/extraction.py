"""跨子域三轨并发抽取协同与跨文件规范融合（Layer 4: Catalog Extraction）。

职责边界：
- 主线程读取 `RawBlockModel` 快照并在必要时完成混装块切分（`analyze_block_segments`）；
- Worker 线程池通过内核 `execute_wave` / `Scheduler` 纯计算提取表元数据、码表草案与核查规则证据（**零 DB 访问**）；
- 主协调线程单写落库并执行包内/全局 O(F+C) 字段-码表自动对齐（`auto_link_all_references`）；
- `reconcile_package_assets` 完成同一规范包内 Word+Excel 多文件同表融合与跨文件冲突检测；
- 将协同调度置于 `catalog/` 顶层，彻底消除 `code_sets/` 反向依赖 `tables/` 的跨子域耦合。
"""

import hashlib
import re
import uuid
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from app.catalog.code_sets.linking import auto_link_all_references
from app.catalog.code_sets.service import (
    compute_code_sets_for_block_snapshot,
    persist_computed_code_sets,
    preload_existing_code_sets_state,
)
from app.catalog.tables.mutations import merge_metadata_tables
from app.catalog.tables.service import (
    compute_metadata_for_block_snapshot,
    persist_computed_metadata,
    snapshot_raw_block,
)
from app.infra.config import Settings
from app.infra.contracts import canonical_json
from app.infra.db import SessionLocal
from app.infra.db_tables import (
    FieldCodeReferenceModel,
    FieldMetadataModel,
    MetadataRevisionModel,
    MetadataValidationModel,
    RawBlockModel,
    SourceDocumentModel,
    TableMetadataModel,
    TableRuleEvidenceModel,
)
from app.infra.errors import ProviderAuthOrBillingError
from app.infra.llm.guard import current_cancellation, current_llm_bus
from app.ingestion.structure_service import analyze_block_segments
from app.kernel.run import BudgetExceededError, RunCancelledError
from app.kernel.scheduler import execute_wave

PRIORITY_STOP_ERRORS = (RunCancelledError, BudgetExceededError, ProviderAuthOrBillingError)
_NON_EXTRACTABLE_ROLES = frozenset({"CATALOG_INDEX", "MATRIX_REPORT", "REVISION_DIFF", "IGNORE", "NARRATIVE"})
_NON_EXTRACTABLE_BLOCK_TYPES = frozenset({"CATALOG_INDEX", "MATRIX_REPORT", "REVISION_DIFF", "IGNORE", "NARRATIVE"})


def extract_rule_evidences_from_block_snapshot(block_snap: Dict[str, Any]) -> List[Dict[str, Any]]:
    """纯计算提取结构化/文本型核查规则证据（Excel 检核表直接提取；PDF/Word 规则条款按序号+表号解析）。"""
    content = dict(block_snap.get("content_json") or {})
    locator = dict(block_snap.get("source_locator") or {})
    block_id = str(block_snap["id"])
    raw = str(block_snap.get("raw_content") or "")

    pre_rows = content.get("rule_rows")
    if isinstance(pre_rows, list) and pre_rows:
        out: List[Dict[str, Any]] = []
        for item in pre_rows:
            if not isinstance(item, dict):
                continue
            rtext = str(item.get("rule_text") or "").strip()
            rsql = str(item.get("reference_sql") or "").strip()
            if not rtext and not rsql:
                continue
            tcode = str(item.get("table_code") or content.get("table_code") or locator.get("table_code") or "GENERAL").strip().upper()[:90]
            rcode = str(item.get("rule_code") or "").strip()[:90]
            rhash = str(item.get("rule_hash") or "").strip() or hashlib.sha256(
                canonical_json({"table_code": tcode, "rule_code": rcode, "rule_text": rtext, "reference_sql": rsql}).encode("utf-8")
            ).hexdigest()
            out.append(
                {
                    "table_code": tcode,
                    "table_name": str(item.get("table_name") or content.get("table_name") or "")[:250],
                    "rule_code": rcode,
                    "rule_name": str(item.get("rule_name") or "")[:250],
                    "severity": "SOFT" if str(item.get("severity") or "").upper() == "SOFT" else "HARD",
                    "scope": "CROSS_TABLE" if str(item.get("scope") or "").upper() == "CROSS_TABLE" else "SINGLE_TABLE",
                    "target_fields": list(item.get("target_fields") or []),
                    "rule_text": rtext or rsql,
                    "reference_sql": rsql,
                    "source_locator": {**locator, **(item.get("source_locator") or {}), "block_id": block_id},
                    "rule_hash": rhash,
                }
            )
        return out

    # 纯文本 / SAFE 核查规则 PDF 块（例如《对外金融资产负债及交易统计核查规则_2024年版.pdf》）：按规则条目切分
    default_tcode = str(content.get("table_code") or locator.get("table_code") or "").strip().upper()
    default_tname = str(content.get("table_name") or locator.get("section") or "").strip()
    is_cross_section = any(k in f"{default_tname} {raw[:300]}" for k in ("跨表", "表间", "CROSS"))
    entries: List[Tuple[str, str]] = []
    cur_code = ""
    cur_lines: List[str] = []
    for line in raw.splitlines():
        clean = line.strip()
        if not clean:
            continue
        m_num = re.match(r"^(?:(\d{1,4})[、.\s]+)?([A-Z]\d{2}(?:-\d)?|[A-Z][A-Z0-9_]{2,})?\s*(.*)$", clean)
        if m_num and m_num.group(1) and len(clean) > 4:
            if cur_lines:
                entries.append((cur_code, " ".join(cur_lines)))
            cur_code = f"R{m_num.group(1)}"
            cur_lines = [clean]
        else:
            cur_lines.append(clean)
    if cur_lines:
        entries.append((cur_code, " ".join(cur_lines)))
    if not entries and raw.strip():
        entries = [("R1", raw.strip()[:2000])]

    out_items: List[Dict[str, Any]] = []
    for rcode, text in entries[:120]:
        if len(text) < 6:
            continue
        found_tables = re.findall(r"\b([A-Z]\d{2}(?:-\d)?|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b", text)
        tcode = (found_tables[0] if found_tables else default_tcode or "GENERAL")[:90]
        # 识别 SAFE 指标/字段编码（如 Z0105, A0102, 或含下划线的字段名）
        raw_fields = re.findall(r"\b([A-Z]\d{4}[A-Z0-9_]*|[A-Z]{2,}_[A-Z0-9_]+)\b", text)
        target_fields = list(dict.fromkeys(f for f in raw_fields if f != tcode))[:12]
        scope = "CROSS_TABLE" if (len(set(found_tables)) > 1 or is_cross_section or "跨表" in text or "表间" in text) else "SINGLE_TABLE"
        severity = "SOFT" if any(k in text for k in ("一般应", "可疑", "非系统校验", "疑问数据", "提示")) else "HARD"
        rhash = hashlib.sha256(
            canonical_json({"table_code": tcode, "rule_code": rcode, "rule_text": text[:1500]}).encode("utf-8")
        ).hexdigest()
        out_items.append(
            {
                "table_code": tcode,
                "table_name": default_tname[:250],
                "rule_code": rcode[:90],
                "rule_name": "",
                "severity": severity,
                "scope": scope,
                "target_fields": target_fields,
                "rule_text": text[:4000],
                "reference_sql": "",
                "source_locator": {**locator, "block_id": block_id},
                "rule_hash": rhash,
            }
        )
    return out_items


def persist_rule_evidences(
    session: Session,
    block: RawBlockModel,
    items: List[Dict[str, Any]],
    *,
    package_id: Optional[str] = None,
) -> List[str]:
    """幂等写入独立核查规则 / Excel 检核规则到 TableRuleEvidenceModel。"""
    if not items:
        return []
    resolved_pkg = package_id
    if resolved_pkg is None:
        resolved_pkg = (
            session.query(SourceDocumentModel.package_id)
            .filter(SourceDocumentModel.id == block.document_id)
            .scalar()
        )
    existing_by_hash = {
        row.rule_hash: row
        for row in session.query(TableRuleEvidenceModel)
        .filter(TableRuleEvidenceModel.document_id == block.document_id, TableRuleEvidenceModel.block_id == block.id)
        .all()
    }
    ids: List[str] = []
    for item in items:
        rhash = item["rule_hash"]
        cur = existing_by_hash.get(rhash)
        if cur is not None:
            if cur.package_id is None and resolved_pkg:
                cur.package_id = resolved_pkg
            ids.append(cur.id)
            continue
        row = TableRuleEvidenceModel(
            id=str(uuid.uuid4()),
            package_id=resolved_pkg,
            document_id=block.document_id,
            block_id=block.id,
            table_code=str(item.get("table_code") or "GENERAL")[:96],
            table_name=str(item.get("table_name") or "")[:255],
            rule_code=str(item.get("rule_code") or "")[:96],
            rule_name=str(item.get("rule_name") or "")[:255],
            severity=str(item.get("severity") or "HARD")[:16],
            scope=str(item.get("scope") or "SINGLE_TABLE")[:24],
            target_fields_json=list(item.get("target_fields") or []),
            rule_text=str(item.get("rule_text") or ""),
            reference_sql=str(item.get("reference_sql") or ""),
            source_locator=dict(item.get("source_locator") or {}),
            rule_hash=rhash,
        )
        session.add(row)
        existing_by_hash[rhash] = row
        ids.append(row.id)
    session.flush()
    return ids


def prepare_block_extraction_tasks(
    session: Session,
    block_ids: List[str],
    settings: Settings,
    *,
    targets: Optional[List[str]] = None,
    skip_keys: Optional[Set[Tuple[str, str]]] = None,
    cancellation: Any = None,
) -> Dict[str, Any]:
    """在主线程准备脱库块任务切片、分段子块并过滤可复用的断点检查点（支持 metadata / code_sets / rule_evidence 三轨）。"""
    active_targets = set(targets or ["metadata", "code_sets", "rule_evidence"])
    skip = skip_keys or set()
    blocks = session.query(RawBlockModel).filter(RawBlockModel.id.in_(block_ids)).all()
    by_id = {b.id: b for b in blocks}

    metadata_block_ids: List[str] = []
    code_set_block_ids: List[str] = []
    rule_block_ids: List[str] = []
    matrix_report_blocks = 0

    for bid in block_ids:
        b = by_id.get(bid)
        if not b or b.content_json.get("superseded"):
            continue
        crole = str(b.content_json.get("candidate_role") or "")
        if b.block_type == "MATRIX_REPORT" or crole == "MATRIX_REPORT":
            matrix_report_blocks += 1
        is_cs_only = b.block_type in {"CODE_SET", "CODE_SET_CANDIDATE"} or crole == "CODE_SET"
        is_rule_only = (
            b.block_type in {"RULE_EVIDENCE", "REVISION_DIFF"}
            or crole in {"RULE_EVIDENCE", "REVISION_DIFF"}
            or bool(b.content_json.get("rule_rows"))
        )
        is_non_meta = (
            is_cs_only
            or is_rule_only
            or b.block_type in _NON_EXTRACTABLE_BLOCK_TYPES
            or crole in _NON_EXTRACTABLE_ROLES
        )
        if "metadata" in active_targets and not is_non_meta:
            metadata_block_ids.append(bid)
        if "code_sets" in active_targets:
            if is_cs_only or b.content_json.get("inline_code_sets") or b.content_json.get("code_set_spec"):
                code_set_block_ids.append(bid)
        if "rule_evidence" in active_targets:
            if b.block_type == "RULE_EVIDENCE" or crole == "RULE_EVIDENCE" or bool(b.content_json.get("rule_rows")):
                rule_block_ids.append(bid)

    tasks: List[Dict[str, Any]] = []
    pre_errors: List[Dict[str, str]] = []
    skipped_meta_ids: List[str] = []
    skipped_cs_ids: List[str] = []
    ordered_meta_ids: List[str] = []
    ordered_cs_ids: List[str] = []
    ordered_rule_ids: List[str] = []
    skipped_rule_ids: List[str] = []

    for bid in metadata_block_ids:
        if ("metadata", bid) in skip:
            skipped_meta_ids.append(bid)
            continue
        ordered_meta_ids.append(bid)
        blk = by_id[bid]
        has_structured = bool(
            blk.content_json.get("spec")
            or blk.content_json.get("parent_spec")
            or blk.content_json.get("rows")
        )
        if has_structured:
            tasks.append(
                {
                    "track": "metadata",
                    "block_id": bid,
                    "raw_hash": blk.raw_hash or "",
                    "kind": "structured",
                    "snap": snapshot_raw_block(blk),
                }
            )
            continue

        try:
            if cancellation is not None and hasattr(cancellation, "raise_if_cancelled"):
                cancellation.raise_if_cancelled()
            seg_ids = analyze_block_segments(bid, settings)
            if not seg_ids:
                raise ValueError(f"No table metadata produced for block {bid}")
            seg_rows = session.query(RawBlockModel).filter(RawBlockModel.id.in_(seg_ids)).all()
            seg_by_id = {r.id: r for r in seg_rows}
            seg_snaps = [snapshot_raw_block(seg_by_id[sid]) for sid in seg_ids if sid in seg_by_id]
            if not seg_snaps:
                raise ValueError(f"No table metadata produced for block {bid}")
            tasks.append(
                {
                    "track": "metadata",
                    "block_id": bid,
                    "raw_hash": blk.raw_hash or "",
                    "kind": "segmented",
                    "segment_snaps": seg_snaps,
                }
            )
        except PRIORITY_STOP_ERRORS:
            raise
        except Exception as exc:  # noqa: BLE001
            pre_errors.append({"block_id": bid, "track": "metadata", "error": f"{type(exc).__name__}: {exc}"})

    for bid in code_set_block_ids:
        if ("code_sets", bid) in skip:
            skipped_cs_ids.append(bid)
            continue
        ordered_cs_ids.append(bid)
        blk = by_id[bid]
        tasks.append(
            {
                "track": "code_sets",
                "block_id": bid,
                "raw_hash": blk.raw_hash or "",
                "snap": snapshot_raw_block(blk),
            }
        )

    for bid in rule_block_ids:
        if ("rule_evidence", bid) in skip:
            skipped_rule_ids.append(bid)
            continue
        ordered_rule_ids.append(bid)
        blk = by_id[bid]
        tasks.append(
            {
                "track": "rule_evidence",
                "block_id": bid,
                "raw_hash": blk.raw_hash or "",
                "snap": snapshot_raw_block(blk),
            }
        )

    existing_cs_state = preload_existing_code_sets_state(session)
    return {
        "tasks": tasks,
        "pre_errors": pre_errors,
        "skipped_meta_ids": skipped_meta_ids,
        "skipped_cs_ids": skipped_cs_ids,
        "skipped_rule_ids": skipped_rule_ids,
        "ordered_meta_ids": ordered_meta_ids,
        "ordered_cs_ids": ordered_cs_ids,
        "ordered_rule_ids": ordered_rule_ids,
        "matrix_report_blocks": matrix_report_blocks,
        "existing_cs_state": existing_cs_state,
    }


def execute_block_task(
    task: Dict[str, Any],
    settings: Settings,
    *,
    bus: Any = None,
    existing_cs_state: Optional[Dict[str, Dict[str, Any]]] = None,
    cancellation: Any = None,
) -> Dict[str, Any]:
    """纯计算/HTTP 块级提取执行器（零 DB 访问）；单块普通异常收敛为 error 字段，中断/预算/账单异常原样穿透。"""
    active_cancel = cancellation if cancellation is not None else current_cancellation()
    if active_cancel is not None and hasattr(active_cancel, "raise_if_cancelled"):
        active_cancel.raise_if_cancelled()

    track = task["track"]
    bid = task["block_id"]
    raw_hash = task.get("raw_hash") or ""
    active_bus = bus if bus is not None else current_llm_bus()

    try:
        if track == "rule_evidence":
            rule_items = extract_rule_evidences_from_block_snapshot(task["snap"])
            return {
                "track": "rule_evidence",
                "block_id": bid,
                "raw_hash": raw_hash,
                "items": rule_items,
            }

        if track == "code_sets":
            items = compute_code_sets_for_block_snapshot(
                task["snap"],
                settings,
                review_with_ai="adaptive",
                existing_state=existing_cs_state,
                bus=active_bus,
            )
            return {
                "track": "code_sets",
                "block_id": bid,
                "raw_hash": raw_hash,
                "items": items,
            }

        if task.get("kind") == "segmented":
            seg_computeds: List[Tuple[str, Dict[str, Any]]] = []
            for seg_snap in task.get("segment_snaps") or []:
                if active_cancel is not None and hasattr(active_cancel, "raise_if_cancelled"):
                    active_cancel.raise_if_cancelled()
                seg_computeds.append(
                    (
                        str(seg_snap["id"]),
                        compute_metadata_for_block_snapshot(seg_snap, settings, bus=active_bus),
                    )
                )
            return {
                "track": "metadata",
                "block_id": bid,
                "raw_hash": raw_hash,
                "kind": "segmented",
                "seg_computeds": seg_computeds,
            }

        computed = compute_metadata_for_block_snapshot(task["snap"], settings, bus=active_bus)
        return {
            "track": "metadata",
            "block_id": bid,
            "raw_hash": raw_hash,
            "kind": "structured",
            "computed": computed,
        }
    except PRIORITY_STOP_ERRORS:
        raise
    except Exception as exc:  # noqa: BLE001
        return {
            "track": track,
            "block_id": bid,
            "raw_hash": raw_hash,
            "error": f"{type(exc).__name__}: {exc}",
        }


def persist_block_extraction_outputs(
    session: Session,
    meta_outputs: Dict[str, Dict[str, Any]],
    codeset_outputs: Dict[str, Dict[str, Any]],
    *,
    rule_outputs: Optional[Dict[str, Dict[str, Any]]] = None,
    ordered_meta_ids: Optional[List[str]] = None,
    ordered_cs_ids: Optional[List[str]] = None,
    ordered_rule_ids: Optional[List[str]] = None,
    pre_errors: Optional[List[Dict[str, str]]] = None,
    initial_table_ids: Optional[List[str]] = None,
    initial_code_set_ids: Optional[List[str]] = None,
    package_id: Optional[str] = None,
    on_progress: Optional[Callable[[int, int, int], None]] = None,
    auto_link: bool = True,
) -> Dict[str, Any]:
    """在主协调线程中单写落库所有块级提取结果（元数据/码表/核查规则证据），并按包执行 O(F+C) 自动引用对齐。"""
    table_ids: List[str] = list(initial_table_ids or [])
    code_set_ids: List[str] = list(initial_code_set_ids or [])
    rule_evidence_ids: List[str] = []
    errors: List[Dict[str, str]] = list(pre_errors or [])

    needed_block_ids: Set[str] = set()
    for out in meta_outputs.values():
        if "error" in out:
            continue
        if out.get("kind") == "segmented":
            for seg_id, _ in out.get("seg_computeds") or []:
                needed_block_ids.add(seg_id)
        else:
            needed_block_ids.add(str(out["block_id"]))
    for bid, out in (rule_outputs or {}).items():
        if "error" not in out:
            needed_block_ids.add(str(out.get("block_id") or bid))

    blocks_by_id: Dict[str, RawBlockModel] = (
        {
            r.id: r
            for r in session.query(RawBlockModel).filter(RawBlockModel.id.in_(needed_block_ids)).all()
        }
        if needed_block_ids
        else {}
    )

    cs_keys = ordered_cs_ids if ordered_cs_ids is not None else list(codeset_outputs.keys())
    for bid in cs_keys:
        out = codeset_outputs.get(bid)
        if not out:
            continue
        if "error" in out:
            errors.append({"block_id": bid, "track": "code_sets", "error": out["error"]})
            continue
        try:
            persisted_cs = persist_computed_code_sets(session, out.get("items") or [], auto_link=False)
            for cid in persisted_cs:
                if cid not in code_set_ids:
                    code_set_ids.append(cid)
        except Exception as exc:  # noqa: BLE001
            errors.append({"block_id": bid, "track": "code_sets", "error": f"{type(exc).__name__}: {exc}"})

    r_outputs = rule_outputs or {}
    rule_keys = ordered_rule_ids if ordered_rule_ids is not None else list(r_outputs.keys())
    for bid in rule_keys:
        out = r_outputs.get(bid)
        if not out:
            continue
        if "error" in out:
            errors.append({"block_id": bid, "track": "rule_evidence", "error": out["error"]})
            continue
        blk = blocks_by_id.get(bid)
        if blk is None:
            continue
        try:
            persisted_rules = persist_rule_evidences(session, blk, out.get("items") or [], package_id=package_id)
            for rid in persisted_rules:
                if rid not in rule_evidence_ids:
                    rule_evidence_ids.append(rid)
        except Exception as exc:  # noqa: BLE001
            errors.append({"block_id": bid, "track": "rule_evidence", "error": f"{type(exc).__name__}: {exc}"})

    meta_keys = ordered_meta_ids if ordered_meta_ids is not None else list(meta_outputs.keys())
    total_meta = len(meta_keys)
    for completed, bid in enumerate(meta_keys, start=1):
        out = meta_outputs.get(bid)
        if not out:
            continue
        if "error" in out:
            errors.append({"block_id": bid, "track": "metadata", "error": out["error"]})
        else:
            try:
                if out.get("kind") == "segmented":
                    seg_table_ids: List[str] = []
                    for seg_id, computed in out.get("seg_computeds") or []:
                        seg_blk = blocks_by_id.get(seg_id)
                        if seg_blk is None:
                            continue
                        tbl = persist_computed_metadata(session, seg_blk, computed, auto_link=False)
                        seg_table_ids.append(tbl.id)
                    if len(seg_table_ids) > 1:
                        merged_id = merge_metadata_tables(session, bid, seg_table_ids)
                        if merged_id:
                            table_ids.append(merged_id)
                    elif seg_table_ids:
                        table_ids.append(seg_table_ids[0])
                else:
                    blk = blocks_by_id.get(bid)
                    if blk is not None:
                        tbl = persist_computed_metadata(session, blk, out["computed"], auto_link=False)
                        table_ids.append(tbl.id)
            except Exception as exc:  # noqa: BLE001
                errors.append({"block_id": bid, "track": "metadata", "error": f"{type(exc).__name__}: {exc}"})
        if callable(on_progress):
            try:
                on_progress(completed, total_meta, len(table_ids))
            except Exception:
                pass

    resolved_pkg = package_id
    if resolved_pkg is None and blocks_by_id:
        doc_ids = {b.document_id for b in blocks_by_id.values()}
        pkgs = {
            p
            for (p,) in session.query(SourceDocumentModel.package_id)
            .filter(SourceDocumentModel.id.in_(doc_ids))
            .all()
            if p is not None
        }
        if len(pkgs) == 1:
            resolved_pkg = next(iter(pkgs))

    resolved_count = auto_link_all_references(session, package_id=resolved_pkg) if auto_link else 0
    session.flush()

    produced_count = len(table_ids) + len(code_set_ids) + len(rule_evidence_ids)
    status = "PARALLEL_EXTRACTION_COMPLETE"
    if errors:
        status = "PARALLEL_EXTRACTION_PARTIAL" if produced_count else "PARALLEL_EXTRACTION_FAILED"
    return {
        "table_ids": table_ids,
        "code_set_ids": code_set_ids,
        "rule_evidence_ids": rule_evidence_ids,
        "rule_evidence_count": len(rule_evidence_ids),
        "resolved_reference_count": resolved_count,
        "errors": errors,
        "status": status,
    }


def extract_parallel_for_blocks(
    block_ids: List[str],
    settings: Settings,
    targets: Optional[List[str]] = None,
    on_progress: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run Track A (Metadata), Track B (Code Sets), Track C (Rule Evidence) concurrently via pure-compute workers."""
    with SessionLocal() as session:
        prep = prepare_block_extraction_tasks(
            session,
            block_ids,
            settings,
            targets=targets,
            cancellation=current_cancellation(),
        )
        session.commit()

    tasks: List[Dict[str, Any]] = prep["tasks"]
    existing_cs_state = prep["existing_cs_state"]
    active_bus = current_llm_bus()
    cancellation = current_cancellation()
    partial_results: List[Dict[str, Any]] = []

    def _collect(
        outputs: List[Dict[str, Any]],
    ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        meta_map: Dict[str, Dict[str, Any]] = {}
        cs_map: Dict[str, Dict[str, Any]] = {}
        rule_map: Dict[str, Dict[str, Any]] = {}
        for item in outputs:
            if item["track"] == "code_sets":
                cs_map[item["block_id"]] = item
            elif item["track"] == "rule_evidence":
                rule_map[item["block_id"]] = item
            else:
                meta_map[item["block_id"]] = item
        return meta_map, cs_map, rule_map

    try:
        wave_outputs = (
            execute_wave(
                tasks,
                lambda t: execute_block_task(
                    t,
                    settings,
                    bus=active_bus,
                    existing_cs_state=existing_cs_state,
                    cancellation=cancellation,
                ),
                bus=active_bus,
                stage="parallel_extraction",
                labels=[f"{t['track']}:{t['block_id'][:8]}" for t in tasks],
                max_workers=min(4, max(1, len(tasks))),
                cancellation=cancellation,
                partial_results_out=partial_results,
            )
            if tasks
            else []
        )
    except BaseException:
        if partial_results:
            meta_map, cs_map, rule_map = _collect(partial_results)
            with SessionLocal() as session:
                persist_block_extraction_outputs(
                    session,
                    meta_map,
                    cs_map,
                    rule_outputs=rule_map,
                    pre_errors=prep["pre_errors"],
                    auto_link=False,
                )
                session.commit()
        raise

    meta_map, cs_map, rule_map = _collect(wave_outputs)
    with SessionLocal() as session:
        result = persist_block_extraction_outputs(
            session,
            meta_map,
            cs_map,
            rule_outputs=rule_map,
            ordered_meta_ids=prep["ordered_meta_ids"],
            ordered_cs_ids=prep["ordered_cs_ids"],
            ordered_rule_ids=prep["ordered_rule_ids"],
            pre_errors=prep["pre_errors"],
            on_progress=on_progress,
            auto_link=True,
        )
        result["matrix_report_blocks"] = prep.get("matrix_report_blocks", 0)
        session.commit()
    return result


def _norm_table_key(name: str) -> str:
    return re.sub(r"[\s_\-：:（）()]+", "", str(name or "")).lower()


def reconcile_package_assets(session: Session, package_id: str) -> Dict[str, Any]:
    """包级跨文件融合：对齐 Word+Excel 同表字段、检测并标记定义冲突、包内码表绑定与规则统计。"""
    from app.catalog.tables.readiness import latest_table_rows

    docs = (
        session.query(SourceDocumentModel)
        .filter(SourceDocumentModel.package_id == package_id, SourceDocumentModel.status != "DELETED")
        .all()
    )
    doc_by_id = {d.id: d for d in docs}
    doc_ids = list(doc_by_id.keys())
    if not doc_ids:
        return {
            "package_id": package_id,
            "merged_groups": 0,
            "merged_table_codes": [],
            "conflict_count": 0,
            "conflicts": [],
            "linked_references": 0,
            "rule_evidence_count": 0,
            "matrix_report_count": 0,
        }

    # 从包内所有 CATALOG_INDEX 块收集表号<->中文名映射，以及统计 MATRIX_REPORT 块数
    catalog_map: Dict[str, Dict[str, str]] = {}
    matrix_count = 0
    for blk in session.query(RawBlockModel).filter(RawBlockModel.document_id.in_(doc_ids)).all():
        cj = blk.content_json or {}
        if blk.block_type == "MATRIX_REPORT" or cj.get("candidate_role") == "MATRIX_REPORT":
            matrix_count += 1
        if isinstance(cj.get("table_index_map"), dict):
            catalog_map.update(cj["table_index_map"])

    def _lookup_catalog_code(name: str) -> str:
        name_k = _norm_table_key(name)
        if not name_k or not catalog_map:
            return ""
        if name_k in catalog_map and catalog_map[name_k].get("table_code"):
            return str(catalog_map[name_k]["table_code"]).strip().upper()[:96]
        if len(name_k) >= 4:
            for k, entry in catalog_map.items():
                if k and (k in name_k or name_k in k) and entry.get("table_code"):
                    return str(entry["table_code"]).strip().upper()[:96]
        return ""

    tables = latest_table_rows(session, package_id=package_id)
    for tbl in tables:
        if tbl.table_code.startswith("TABLE_"):
            mapped_code = _lookup_catalog_code(tbl.table_name)
            if mapped_code:
                tbl.table_code = mapped_code

    def _group_key(tbl: TableMetadataModel) -> str:
        mapped_code = _lookup_catalog_code(tbl.table_name)
        if mapped_code:
            return mapped_code
        if tbl.table_code and not tbl.table_code.startswith("TABLE_"):
            return tbl.table_code.strip().upper()
        return _norm_table_key(tbl.table_name) or tbl.id

    groups: Dict[str, List[TableMetadataModel]] = {}
    for tbl in tables:
        groups.setdefault(_group_key(tbl), []).append(tbl)

    role_rank = {"SCHEMA_APPENDIX": 3, "MAIN_SPEC": 2, "AUTO": 1}
    merged_groups = 0
    merged_table_codes: List[str] = []
    all_conflicts: List[Dict[str, Any]] = []

    for gkey, members in groups.items():
        distinct_docs = {m.document_id for m in members}
        if len(members) < 2 or len(distinct_docs) < 2:
            continue

        fields_by_tid: Dict[str, List[FieldMetadataModel]] = {
            m.id: (
                session.query(FieldMetadataModel)
                .filter(FieldMetadataModel.table_id == m.id)
                .order_by(FieldMetadataModel.sort_order)
                .all()
            )
            for m in members
        }

        def _score_member(m: TableMetadataModel) -> Tuple[int, int, int]:
            doc = doc_by_id.get(m.document_id)
            r_score = role_rank.get(doc.doc_role if doc else "AUTO", 1)
            flds = fields_by_tid[m.id]
            src_cnt = sum(1 for f in flds if (f.type_basis or "").upper() == "SOURCE")
            return (r_score, src_cnt, len(flds))

        ordered = sorted(members, key=_score_member, reverse=True)
        primary = ordered[0]
        secondaries = ordered[1:]

        primary_fields = fields_by_tid[primary.id]
        by_en: Dict[str, FieldMetadataModel] = {f.field_en.strip().upper(): f for f in primary_fields}
        by_cn: Dict[str, FieldMetadataModel] = {_norm_table_key(f.field_cn): f for f in primary_fields if _norm_table_key(f.field_cn)}
        next_sort = len(primary_fields)
        group_conflicts: List[Dict[str, Any]] = []

        # 记录多文件双源追溯
        for pf in primary_fields:
            p_doc = doc_by_id.get(primary.document_id)
            ev = dict(pf.evidence_json or {})
            ev.setdefault("sources", [{"document_id": primary.document_id, "file_name": p_doc.file_name if p_doc else ""}])
            pf.evidence_json = ev

        for sec in secondaries:
            sec_doc = doc_by_id.get(sec.document_id)
            sec_fname = sec_doc.file_name if sec_doc else sec.document_id
            for sf in fields_by_tid[sec.id]:
                target = by_en.get(sf.field_en.strip().upper())
                if target is None and _norm_table_key(sf.field_cn):
                    cand = by_cn.get(_norm_table_key(sf.field_cn))
                    # 仅当主表用哈希占位名而从表给出真实英文名时按中文名对齐
                    if cand is not None and cand.field_en.startswith("FIELD_") and not sf.field_en.startswith("FIELD_"):
                        cand.field_en = sf.field_en
                        by_en[sf.field_en.strip().upper()] = cand
                        target = cand
                if target is None:
                    new_field = FieldMetadataModel(
                        id=str(uuid.uuid4()),
                        table_id=primary.id,
                        field_cn=sf.field_cn,
                        field_en=sf.field_en,
                        field_definition=sf.field_definition,
                        check_text_raw=sf.check_text_raw,
                        type_basis=sf.type_basis,
                        data_type=sf.data_type,
                        length=sf.length,
                        decimal_places=sf.decimal_places,
                        required=sf.required,
                        code_reference_text=sf.code_reference_text,
                        confidence=sf.confidence,
                        evidence_json={
                            **(sf.evidence_json or {}),
                            "sources": [{"document_id": sec.document_id, "file_name": sec_fname}],
                        },
                        sort_order=next_sort,
                        ai_value_json=dict(sf.ai_value_json or {}),
                    )
                    session.add(new_field)
                    by_en[new_field.field_en.strip().upper()] = new_field
                    if _norm_table_key(new_field.field_cn):
                        by_cn.setdefault(_norm_table_key(new_field.field_cn), new_field)
                    next_sort += 1
                    continue

                # 融合多源字段属性并检测显式声明冲突
                p_is_src = (target.type_basis or "").upper() == "SOURCE"
                s_is_src = (sf.type_basis or "").upper() == "SOURCE"
                if not p_is_src and s_is_src:
                    target.data_type = sf.data_type
                    target.length = sf.length
                    target.decimal_places = sf.decimal_places
                    target.type_basis = sf.type_basis
                elif p_is_src and s_is_src and (
                    target.data_type != sf.data_type
                    or int(target.length or 0) != int(sf.length or 0)
                    or int(target.decimal_places or 0) != int(sf.decimal_places or 0)
                ):
                    issue = {
                        "severity": "ERROR",
                        "code": "CROSS_FILE_CONFLICT",
                        "field_en": target.field_en,
                        "message": (
                            f"多文件类型定义冲突：主来源 {target.data_type}({target.length},{target.decimal_places}) "
                            f"vs {sec_fname} {sf.data_type}({sf.length},{sf.decimal_places})"
                        ),
                    }
                    group_conflicts.append(issue)
                    all_conflicts.append({"table_code": primary.table_code, **issue})

                if sf.field_definition and sf.field_definition != sf.field_cn and sf.field_definition not in (target.field_definition or ""):
                    target.field_definition = (
                        f"{target.field_definition}；{sf.field_definition}".strip("；")
                        if target.field_definition and target.field_definition != target.field_cn
                        else sf.field_definition
                    )
                if sf.check_text_raw and sf.check_text_raw not in (target.check_text_raw or ""):
                    target.check_text_raw = (
                        f"{target.check_text_raw}；{sf.check_text_raw}".strip("；")
                        if target.check_text_raw
                        else sf.check_text_raw
                    )
                if sf.code_reference_text and not target.code_reference_text:
                    target.code_reference_text = sf.code_reference_text
                if sf.required and not target.required:
                    target.required = True

                ev = dict(target.evidence_json or {})
                sources = list(ev.get("sources") or [])
                if not any(s.get("document_id") == sec.document_id for s in sources):
                    sources.append({"document_id": sec.document_id, "file_name": sec_fname})
                ev["sources"] = sources
                target.evidence_json = ev

            if sec.table_definition and sec.table_definition not in (primary.table_definition or ""):
                primary.table_definition = f"{primary.table_definition}\n{sec.table_definition}".strip()
            sec.status = "MERGED"

        session.add(
            MetadataRevisionModel(
                id=str(uuid.uuid4()),
                table_id=primary.id,
                field_id=None,
                revision_kind="CROSS_FILE_MERGE",
                old_value_json=None,
                new_value_json={
                    "merged_table_ids": [s.id for s in secondaries],
                    "conflicts": group_conflicts,
                },
                editor="RECONCILER",
                reason="跨文件同表融合与一致性对齐",
            )
        )
        if group_conflicts:
            session.add(
                MetadataValidationModel(
                    id=str(uuid.uuid4()),
                    table_id=primary.id,
                    validator_kind="CROSS_FILE_RECONCILE",
                    round=1,
                    status="FAILED",
                    issues_json=group_conflicts,
                )
            )
            primary.status = "NEEDS_REVIEW"
        merged_groups += 1
        merged_table_codes.append(primary.table_code)

    linked_refs = auto_link_all_references(session, package_id=package_id)
    rule_cnt = (
        session.query(TableRuleEvidenceModel)
        .filter(TableRuleEvidenceModel.package_id == package_id)
        .count()
    )
    session.flush()
    return {
        "package_id": package_id,
        "merged_groups": merged_groups,
        "merged_table_codes": merged_table_codes,
        "conflict_count": len(all_conflicts),
        "conflicts": all_conflicts,
        "linked_references": linked_refs,
        "rule_evidence_count": rule_cnt,
        "matrix_report_count": matrix_count,
    }
