"""跨子域双轨并发抽取协同（Layer 4: Catalog Extraction）。

职责边界：
- 同时调度 Track A（表与字段元数据抽取 + 非结构化块细分合并）与 Track B（码表抽取与 AI 复核）；
- 全部并发轨完成后执行一次全量 O(F+C) 字段-码表自动对齐（`auto_link_all_references`）；
- 将协同调度置于 `catalog/` 顶层，彻底消除 `code_sets/` 反向依赖 `tables/` 的跨子域耦合。
"""

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from app.catalog.code_sets.linking import auto_link_all_references
from app.catalog.code_sets.service import extract_code_sets_for_block
from app.catalog.tables.mutations import merge_metadata_tables
from app.catalog.tables.service import extract_metadata_for_block
from app.infra.config import Settings
from app.infra.db import SessionLocal
from app.infra.db_tables import RawBlockModel
from app.infra.llm.guard import current_cancellation, submit_with_context
from app.ingestion.structure_service import analyze_block_segments
from app.kernel.run import BudgetExceededError, RunCancelledError


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

    def _check_cancel() -> None:
        token = current_cancellation()
        if token is not None and hasattr(token, "raise_if_cancelled"):
            token.raise_if_cancelled()

    def _run_metadata_one(bid: str) -> str:
        _check_cancel()
        with SessionLocal() as s:
            blk = s.get(RawBlockModel, bid)
            has_structured = bool(
                blk
                and (
                    blk.content_json.get("spec")
                    or blk.content_json.get("parent_spec")
                    or blk.content_json.get("rows")
                )
            )
        if has_structured:
            return extract_metadata_for_block(bid, settings, auto_link=False)
        seg_ids = analyze_block_segments(bid, settings)
        seg_tables = []
        for sid in seg_ids:
            _check_cancel()
            seg_tables.append(extract_metadata_for_block(sid, settings, auto_link=False))
        if len(seg_tables) > 1:
            with SessionLocal() as merge_session:
                return merge_metadata_tables(merge_session, bid, seg_tables)
        if seg_tables:
            return seg_tables[0]
        raise ValueError(f"No table metadata produced for block {bid}")

    def _run_code_set_one(bid: str) -> List[str]:
        _check_cancel()
        return extract_code_sets_for_block(bid, settings, auto_link=False, review_with_ai="adaptive")

    table_ids: List[str] = []
    code_set_ids: List[str] = []
    errors: List[Dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(metadata_block_ids)))) as meta_executor, ThreadPoolExecutor(
        max_workers=min(4, max(1, len(code_set_block_ids)))
    ) as cs_executor:
        meta_futures = {bid: submit_with_context(meta_executor, _run_metadata_one, bid) for bid in metadata_block_ids}
        cs_futures = {bid: submit_with_context(cs_executor, _run_code_set_one, bid) for bid in code_set_block_ids}

        for bid, future in cs_futures.items():
            try:
                for cid in future.result():
                    if cid not in code_set_ids:
                        code_set_ids.append(cid)
            except (RunCancelledError, BudgetExceededError):
                raise
            except Exception as exc:
                errors.append({"block_id": bid, "track": "code_sets", "error": f"{type(exc).__name__}: {exc}"})

        for completed, (bid, future) in enumerate(meta_futures.items(), start=1):
            try:
                table_id = future.result()
                if table_id:
                    table_ids.append(table_id)
            except (RunCancelledError, BudgetExceededError):
                raise
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
