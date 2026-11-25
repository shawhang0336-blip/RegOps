"""跨子域双轨并发抽取协同（Layer 4: Catalog Extraction）。

职责边界：
- 主线程从 SQLite 读取 `RawBlockModel` 快照并在必要时完成混装块切分（`analyze_block_segments`）；
- Worker 线程池通过内核 `execute_wave` / `Scheduler` 纯计算提取表元数据与码表草案（**零 DB 访问**）；
- 主协调线程单写落库并执行一次全量 O(F+C) 字段-码表自动对齐（`auto_link_all_references`）；
- 将协同调度置于 `catalog/` 顶层，彻底消除 `code_sets/` 反向依赖 `tables/` 的跨子域耦合。
"""

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
from app.infra.db import SessionLocal
from app.infra.db_tables import RawBlockModel
from app.infra.errors import ProviderAuthOrBillingError
from app.infra.llm.guard import current_cancellation, current_llm_bus
from app.ingestion.structure_service import analyze_block_segments
from app.kernel.run import BudgetExceededError, RunCancelledError
from app.kernel.scheduler import execute_wave

PRIORITY_STOP_ERRORS = (RunCancelledError, BudgetExceededError, ProviderAuthOrBillingError)


def prepare_block_extraction_tasks(
    session: Session,
    block_ids: List[str],
    settings: Settings,
    *,
    targets: Optional[List[str]] = None,
    skip_keys: Optional[Set[Tuple[str, str]]] = None,
    cancellation: Any = None,
) -> Dict[str, Any]:
    """在主线程准备脱库块任务切片、分段子块并过滤可复用的断点检查点。"""
    active_targets = set(targets or ["metadata", "code_sets"])
    skip = skip_keys or set()
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

    tasks: List[Dict[str, Any]] = []
    pre_errors: List[Dict[str, str]] = []
    skipped_meta_ids: List[str] = []
    skipped_cs_ids: List[str] = []
    ordered_meta_ids: List[str] = []
    ordered_cs_ids: List[str] = []

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

    existing_cs_state = preload_existing_code_sets_state(session)
    return {
        "tasks": tasks,
        "pre_errors": pre_errors,
        "skipped_meta_ids": skipped_meta_ids,
        "skipped_cs_ids": skipped_cs_ids,
        "ordered_meta_ids": ordered_meta_ids,
        "ordered_cs_ids": ordered_cs_ids,
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
    ordered_meta_ids: Optional[List[str]] = None,
    ordered_cs_ids: Optional[List[str]] = None,
    pre_errors: Optional[List[Dict[str, str]]] = None,
    initial_table_ids: Optional[List[str]] = None,
    initial_code_set_ids: Optional[List[str]] = None,
    on_progress: Optional[Callable[[int, int, int], None]] = None,
    auto_link: bool = True,
) -> Dict[str, Any]:
    """在主协调线程中单写落库所有块级提取结果，并统一执行一次全量 O(F+C) 自动引用对齐。"""
    table_ids: List[str] = list(initial_table_ids or [])
    code_set_ids: List[str] = list(initial_code_set_ids or [])
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

    resolved_count = auto_link_all_references(session) if auto_link else 0
    session.flush()

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


def extract_parallel_for_blocks(
    block_ids: List[str],
    settings: Settings,
    targets: Optional[List[str]] = None,
    on_progress: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run Track A (Table Metadata) and Track B (Code Sets) concurrently via pure-compute workers, then persist once."""
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

    def _collect(outputs: List[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        meta_map: Dict[str, Dict[str, Any]] = {}
        cs_map: Dict[str, Dict[str, Any]] = {}
        for item in outputs:
            if item["track"] == "code_sets":
                cs_map[item["block_id"]] = item
            else:
                meta_map[item["block_id"]] = item
        return meta_map, cs_map

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
            meta_map, cs_map = _collect(partial_results)
            with SessionLocal() as session:
                persist_block_extraction_outputs(
                    session,
                    meta_map,
                    cs_map,
                    pre_errors=prep["pre_errors"],
                    auto_link=False,
                )
                session.commit()
        raise

    meta_map, cs_map = _collect(wave_outputs)
    with SessionLocal() as session:
        result = persist_block_extraction_outputs(
            session,
            meta_map,
            cs_map,
            ordered_meta_ids=prep["ordered_meta_ids"],
            ordered_cs_ids=prep["ordered_cs_ids"],
            pre_errors=prep["pre_errors"],
            on_progress=on_progress,
            auto_link=True,
        )
        session.commit()
    return result
