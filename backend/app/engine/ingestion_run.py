"""Step 1 文档导入与解析的统一 Run 控制面（Layer 5: engine）。

将原本散落在 `api/v2/documents.py` 的后台线程与纯内存进度表统一收编至 ProdAgent 微内核 `kernel.run`：
1. 每次 `prepare`（结构体检 + 初步切块）与 `extract`（多块并行元数据 + 码表提取）均创建独立 `RunModel(spec_id=document_id)`，
   通过 `RUN_CREATED(run_kind="INGESTION")` 写入 `EventLog` 不可变哈希链；
2. 后台执行经由 `kernel.run.submit_run` 挂载 `CancellationToken` 注册到全局 `run_registry()`，
   同时通过 `llm_execution_scope` 将 Run 级 `Bus` + `BudgetGuard` + `CancellationToken` 自动透传至所有深层 LLM 调用；
3. 实时进度同步写入内存 `import_progress`（低延迟 + `document.progress` SSE）与 `EventLog`（`INGESTION_STAGE` / `INGESTION_COMPLETED` / `RUN_CANCELLED` / `RUN_FAILED`），
   服务重启后可从 `EventLog` 完整还原阶段、日志与 `budget_usage`，并由 `recover_interrupted_ingestion_runs()` 自动回收僵尸任务。
"""

import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from app.infra.models import (
    DocumentArchitectureModel,
    FieldCodeReferenceModel,
    FieldMetadataModel,
    RawBlockModel,
    SourceDocumentModel,
    TableMetadataModel,
)
from app.infra.config import Settings, get_settings
from app.infra.db import SessionLocal
from app.infra.errors import WorkflowError
from app.infra.models import EventModel, RunModel
from app.infra.config import get_budget
from app.ingestion.import_progress import STAGE_LABELS
from app.ingestion.import_progress import ensure as progress_ensure
from app.ingestion.import_progress import finish as progress_finish
from app.ingestion.import_progress import snapshot as progress_snapshot
from app.ingestion.import_progress import start as progress_start
from app.ingestion.import_progress import update as progress_update
from app.kernel.event_log import append_event, verify_chain
from app.kernel.run import (
    BudgetExceededError,
    BudgetGuard,
    BudgetLimits,
    CancellationToken,
    RunCancelledError,
    cancel_run as kernel_cancel_run,
    create_run as kernel_create_run,
    fail_run as kernel_fail_run,
    record_run_cancelled as kernel_record_run_cancelled,
    run_registry,
    submit_run as kernel_submit_run,
)
from app.infra.llm.guard import llm_execution_scope
from app.policy.regops_bus import default_bus
from app.policy.regops_plan import (
    ACTIVE_INGESTION_STATUSES,
    project_ingestion,
    project_ingestion_many,
)


def _stages_catalog() -> List[Dict[str, Any]]:
    return [
        {"index": index, "label": label, "description": description}
        for index, (label, description) in STAGE_LABELS.items()
    ]


def _is_ingestion_run(session: Session, run: RunModel) -> bool:
    first = (
        session.query(EventModel)
        .filter(EventModel.run_id == run.id)
        .order_by(EventModel.seq)
        .first()
    )
    if first and isinstance(first.payload_json, dict) and first.payload_json.get("run_kind") == "INGESTION":
        return True
    return session.get(SourceDocumentModel, run.spec_id) is not None


def latest_document_run(session: Session, document_id: str) -> Optional[RunModel]:
    """查询指定文档最新的一次 Step 1 Ingestion Run（同一毫秒内以最大事件序号打破平局）。"""
    rows = (
        session.query(RunModel)
        .filter(RunModel.spec_id == document_id)
        .order_by(RunModel.created_at.desc())
        .all()
    )
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    run_ids = [r.id for r in rows]
    max_seq: Dict[str, int] = {}
    for ev_run_id, ev_seq in (
        session.query(EventModel.run_id, EventModel.seq)
        .filter(EventModel.run_id.in_(run_ids))
        .all()
    ):
        if ev_seq > max_seq.get(ev_run_id, 0):
            max_seq[ev_run_id] = ev_seq
    return max(rows, key=lambda r: (r.created_at, max_seq.get(r.id, 0)))


def list_document_runs(session: Session, document_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """列出指定文档的 Ingestion Run 审计记录（含状态、成本快照与哈希链校验）。"""
    rows = (
        session.query(RunModel)
        .filter(RunModel.spec_id == document_id)
        .order_by(RunModel.created_at.desc())
        .limit(limit)
        .all()
    )
    states = project_ingestion_many(session, rows)
    reg = run_registry()
    items = []
    for run in rows:
        state = states[run.id]
        result = state.result_json or {}
        items.append(
            {
                "id": run.id,
                "document_id": document_id,
                "phase": result.get("phase") or "extract",
                "status": state.status,
                "progress_status": result.get("progress_status") or state.status,
                "stage": result.get("stage", 0),
                "detail": result.get("detail") or "",
                "cancelled": bool(result.get("cancelled")),
                "active": reg.is_active(run.id),
                "budget_usage": result.get("budget_usage"),
                "chain_valid": verify_chain(session, run),
                "created_at": run.created_at.isoformat() if run.created_at else None,
                "updated_at": run.updated_at.isoformat() if run.updated_at else None,
            }
        )
    return items


def _record_stage(
    session: Session,
    run: RunModel,
    bus: Any,
    *,
    stage: Optional[int] = None,
    node: str = "ingestion",
    detail: Optional[str] = None,
    counters: Optional[Dict[str, Any]] = None,
    log: Optional[str] = None,
    budget_usage: Optional[Dict[str, Any]] = None,
) -> None:
    payload: Dict[str, Any] = {"node": node, "at": time.time()}
    if stage is not None:
        payload["stage"] = stage
    if detail is not None:
        payload["detail"] = detail
    if counters:
        payload["counters"] = counters
    if log:
        payload["log"] = log
    if budget_usage is not None:
        payload["budget_usage"] = budget_usage
    append_event(session, run, "INGESTION_STAGE", node, payload, bus=bus)
    session.commit()


def _create_ingestion_run(
    session: Session,
    *,
    document_id: str,
    file_name: str,
    phase: str,
    stage: int,
    detail: str,
    counters: Optional[Dict[str, Any]] = None,
) -> RunModel:
    run = kernel_create_run(
        session,
        document_id,
        initial_node=phase,
        initial_payload={
            "run_kind": "INGESTION",
            "document_id": document_id,
            "file_name": file_name,
            "phase": phase,
            "stage": stage,
            "detail": detail,
            "counters": dict(counters or {}),
        },
    )
    session.commit()
    return run


def start_document_prepare_run(
    document_id: str,
    file_name: str,
    settings: Settings,
    *,
    session: Optional[Session] = None,
    reimport_deleted: bool = False,
    initial_bytes: Optional[int] = None,
    session_factory=None,
    runner=None,
    inspect_overview_fn: Optional[Callable[..., Any]] = None,
    reextract_blocks_fn: Optional[Callable[..., Any]] = None,
    store_blocks_fn: Optional[Callable[..., Any]] = None,
) -> str:
    """创建并提交 Step 1 文档结构检查与切块（prepare）Run。"""
    sf = session_factory if session_factory is not None else SessionLocal
    from app.ingestion.structure_service import inspect_document_overview, reextract_document_blocks

    overview_fn = inspect_overview_fn or inspect_document_overview
    reextract_fn = reextract_blocks_fn or reextract_document_blocks

    if store_blocks_fn is None:
        def _default_store_blocks(doc_id: str) -> None:
            from pathlib import Path
            from app.assets.code_sets.service import auto_link_all_references, extract_code_sets_for_block
            from app.ingestion.extractors import extract_document
            from app.ingestion.spec_quality import inspect_block

            with sf() as s:
                doc = s.get(SourceDocumentModel, doc_id)
                if not doc:
                    raise WorkflowError("Document not found")
                if s.query(RawBlockModel).filter(RawBlockModel.document_id == doc.id).count():
                    return
                source_path = Path(settings.blob_dir) / "uploads" / f"{doc.sha256}.{doc.file_type}"
                version, blocks = extract_document(source_path, doc.file_type)
                if not blocks:
                    raise ValueError("No raw blocks were discovered")
                doc.spec_version = version
                doc.status = "UPLOADED"
                doc.block_count = len(blocks)
                for block in blocks:
                    issues = inspect_block(block)
                    if issues:
                        block["content_json"] = {**(block.get("content_json") or {}), "quality_issues": issues}
                    s.add(RawBlockModel(id=str(uuid.uuid4()), document_id=doc.id, **block))
                s.commit()
            with sf() as s:
                cs_ids = [
                    bid for (bid,) in s.query(RawBlockModel.id)
                    .filter(RawBlockModel.document_id == doc_id, RawBlockModel.block_type.in_(["CODE_SET", "CODE_SET_CANDIDATE"]))
                    .order_by(RawBlockModel.ordinal).all()
                ]
            for bid in cs_ids:
                try:
                    extract_code_sets_for_block(bid, settings, auto_link=False, review_with_ai=False)
                except Exception:
                    continue
            if cs_ids:
                with sf() as s:
                    auto_link_all_references(s)
                    s.commit()

        store_blocks_fn = _default_store_blocks

    counters: Dict[str, Any] = {"bytes": initial_bytes} if initial_bytes is not None else {}
    initial_detail = "重新检查文档结构" if reimport_deleted else "文件已保存，准备检查文档结构"
    if reimport_deleted:
        progress_ensure(document_id, file_name)
        progress_update(document_id, stage=1, status="WORKING", detail=initial_detail, log="收到“重新检查并解析”请求")
    else:
        progress_start(document_id, file_name)
        progress_update(
            document_id,
            stage=1,
            status="WORKING",
            detail=initial_detail,
            counters=counters,
            log=f"已保存 {file_name}（{(initial_bytes or 0) // 1024} KB）" if initial_bytes else f"已开始处理 {file_name}",
        )

    if session is not None:
        run = _create_ingestion_run(
            session,
            document_id=document_id,
            file_name=file_name,
            phase="prepare",
            stage=1,
            detail=initial_detail,
            counters=counters,
        )
        run_id = run.id
        budget_cfg = get_budget(session)
    else:
        with sf() as own_session:
            run = _create_ingestion_run(
                own_session,
                document_id=document_id,
                file_name=file_name,
                phase="prepare",
                stage=1,
                detail=initial_detail,
                counters=counters,
            )
            run_id = run.id
            budget_cfg = get_budget(own_session)

    def _drive_prepare(db: Session, run: RunModel, token: CancellationToken) -> None:
        run_bus = default_bus()
        guard = BudgetGuard(BudgetLimits.from_dict(budget_cfg), run_id=run.id).attach(run_bus)
        current_node = "document_overview"

        try:
            with llm_execution_scope(bus=run_bus, cancellation=token):
                token.raise_if_cancelled()
                progress_update(
                    document_id,
                    stage=2,
                    status="WORKING",
                    detail="AI 正在判断文档结构与切块方式",
                    log="开始检查整份文件结构",
                )
                _record_stage(
                    db,
                    run,
                    run_bus,
                    stage=2,
                    node="document_overview",
                    detail="AI 正在判断文档结构与切块方式",
                    log="开始检查整份文件结构",
                )
                overview_fn(document_id, settings, force=reimport_deleted)
                token.raise_if_cancelled()

                db.expire_all()
                run = db.get(RunModel, run_id) or run
                doc = db.get(SourceDocumentModel, document_id)
                doc_status = doc.status if doc else ""
                if doc_status == "OVERVIEW_FAILED":
                    progress_update(
                        document_id,
                        detail="结构体检未完成，继续按文件类型切块",
                        log="结构体检失败，跳过诊断信息，直接进入切块",
                    )

                current_node = "initial_chunking"
                progress_update(
                    document_id,
                    stage=3,
                    detail="正在按文件类型切分内容块",
                    log="结构检查完成，开始初步切块",
                )
                _record_stage(
                    db,
                    run,
                    run_bus,
                    stage=3,
                    node="initial_chunking",
                    detail="正在按文件类型切分内容块",
                    log="结构检查完成，开始初步切块",
                    budget_usage=guard.snapshot(),
                )
                if reimport_deleted:
                    reextract_fn(document_id, settings)
                else:
                    store_blocks_fn(document_id)
                token.raise_if_cancelled()

                db.expire_all()
                run = db.get(RunModel, run_id) or run
                doc = db.get(SourceDocumentModel, document_id)
                count = doc.block_count if doc else 0
                final_doc_status = doc.status if doc else "UPLOADED"

                budget_snap = guard.snapshot()
                progress_update(
                    document_id,
                    counters={"blocks": count},
                    detail=f"已生成 {count} 个内容块",
                    log=f"初步切块完成：{count} 个内容块",
                )
                progress_finish(document_id, status="READY", detail="结构检查与切块完成，可以开始解析字段")
                append_event(
                    db,
                    run,
                    "INGESTION_COMPLETED",
                    "initial_chunking",
                    {
                        "stage": 3,
                        "progress_status": "READY",
                        "detail": "结构检查与切块完成，可以开始解析字段",
                        "counters": {"blocks": count},
                        "document_status": final_doc_status,
                        "budget_usage": budget_snap,
                    },
                    bus=run_bus,
                )
                run_bus.fire("run.completed", {"run_id": run.id, "node": "initial_chunking", "status": "COMPLETED"})
                db.commit()
        except RunCancelledError as exc:
            budget_snap = guard.snapshot()
            progress_finish(document_id, status="FAILED", detail=f"已人工中止：{exc.reason}")
            run = db.get(RunModel, run_id) or run
            kernel_record_run_cancelled(
                db,
                run,
                node=current_node,
                reason=exc.reason,
                extra_result={"budget_usage": budget_snap, "document_id": document_id, "phase": "prepare"},
                bus=run_bus,
                commit=True,
            )
        except Exception as exc:  # noqa: BLE001
            budget_snap = guard.snapshot()
            label = "预算熔断" if isinstance(exc, BudgetExceededError) else f"处理失败（{type(exc).__name__}）"
            progress_finish(document_id, status="FAILED", detail=f"{label}：{exc}")
            err_details = (
                {
                    "stage": current_node,
                    "error_type": type(exc).__name__,
                    "error_kind": "budget",
                    "metric": exc.metric,
                    "used": exc.used,
                    "limit": exc.limit,
                    "budget_snapshot": exc.snapshot,
                }
                if isinstance(exc, BudgetExceededError)
                else {"stage": current_node, "error_type": type(exc).__name__, "error_kind": "ingestion"}
            )
            run = db.get(RunModel, run_id) or run
            kernel_fail_run(
                db,
                run,
                node=current_node,
                error=f"{type(exc).__name__}: {exc}",
                error_details=err_details,
                extra_result={"budget_usage": budget_snap, "document_id": document_id, "phase": "prepare"},
                bus=run_bus,
                commit=True,
            )

    kernel_submit_run(
        run_id,
        sf,
        _drive_prepare,
        allowed_statuses=("NEW", "RUNNING"),
        projector=project_ingestion,
        runner=runner,
    )
    return run_id


def start_document_extract_run(
    document_id: str,
    file_name: str,
    block_ids: List[str],
    targets: Optional[List[str]],
    needs_analysis: bool,
    settings: Settings,
    *,
    session: Optional[Session] = None,
    session_factory=None,
    runner=None,
    analyze_structure_fn: Optional[Callable[..., Any]] = None,
    extract_parallel_fn: Optional[Callable[..., Any]] = None,
) -> str:
    """创建并提交 Step 1 文档多块并发解析（extract）Run。"""
    sf = session_factory if session_factory is not None else SessionLocal
    from app.assets.code_sets.service import extract_parallel_for_blocks
    from app.ingestion.structure_service import analyze_document_structure

    analyze_fn = analyze_structure_fn or analyze_document_structure
    parallel_fn = extract_parallel_fn or extract_parallel_for_blocks
    total = len(block_ids)

    progress_ensure(document_id, file_name)
    progress_update(
        document_id,
        stage=4,
        status="WORKING",
        detail=f"准备解析 {total} 个内容块",
        counters={"blocks_total": total, "blocks_done": 0},
        log=f"收到解析请求：{total} 个内容块",
    )

    if session is not None:
        run = _create_ingestion_run(
            session,
            document_id=document_id,
            file_name=file_name,
            phase="extract",
            stage=4,
            detail=f"准备解析 {total} 个内容块",
            counters={"blocks_total": total, "blocks_done": 0},
        )
        run_id = run.id
        budget_cfg = get_budget(session)
    else:
        with sf() as own_session:
            run = _create_ingestion_run(
                own_session,
                document_id=document_id,
                file_name=file_name,
                phase="extract",
                stage=4,
                detail=f"准备解析 {total} 个内容块",
                counters={"blocks_total": total, "blocks_done": 0},
            )
            run_id = run.id
            budget_cfg = get_budget(own_session)

    def _drive_extract(db: Session, run: RunModel, token: CancellationToken) -> None:
        run_bus = default_bus()
        guard = BudgetGuard(BudgetLimits.from_dict(budget_cfg), run_id=run.id).attach(run_bus)
        current_node = "document_structure" if needs_analysis else "parallel_extraction"

        progress_update(
            document_id,
            stage=4,
            status="WORKING",
            detail=f"正在逐块提取字段与码表（0/{total}）",
            counters={"blocks_total": total, "blocks_done": 0},
            log=f"开始解析 {total} 个内容块",
        )
        _record_stage(
            db,
            run,
            run_bus,
            stage=4,
            node=current_node,
            detail=f"正在逐块提取字段与码表（0/{total}）",
            counters={"blocks_total": total, "blocks_done": 0},
            log=f"开始解析 {total} 个内容块",
        )

        try:
            with llm_execution_scope(bus=run_bus, cancellation=token):
                token.raise_if_cancelled()
                if needs_analysis:
                    analyze_fn(document_id, settings)
                token.raise_if_cancelled()

                current_node = "parallel_extraction"

                def _on_block(done: int, count: int, tables: int) -> None:
                    detail_msg = f"正在逐块提取字段与码表（{done}/{count}）"
                    log_msg = f"已完成 {done}/{count} 个内容块，产出 {tables} 张报表"
                    progress_update(
                        document_id,
                        detail=detail_msg,
                        counters={"blocks_total": count, "blocks_done": done, "tables": tables},
                        log=log_msg,
                    )
                    cur_run = db.get(RunModel, run_id)
                    if cur_run is not None:
                        _record_stage(
                            db,
                            cur_run,
                            run_bus,
                            stage=4,
                            node="parallel_extraction",
                            detail=detail_msg,
                            counters={"blocks_total": count, "blocks_done": done, "tables": tables},
                            log=log_msg,
                            budget_usage=guard.snapshot(),
                        )

                result = parallel_fn(block_ids, settings, targets, on_progress=_on_block)
                token.raise_if_cancelled()
                final_status = "EXTRACTION_PARTIAL" if result.get("errors") else "EXTRACTED"
                if result.get("errors") and not result.get("table_ids") and not result.get("code_set_ids"):
                    final_status = "EXTRACTION_FAILED"
        except RunCancelledError as exc:
            budget_snap = guard.snapshot()
            db.expire_all()
            doc = db.get(SourceDocumentModel, document_id)
            if doc:
                doc.status = "EXTRACTION_FAILED"
            progress_finish(document_id, status="FAILED", detail=f"已人工中止：{exc.reason}")
            run = db.get(RunModel, run_id) or run
            kernel_record_run_cancelled(
                db,
                run,
                node=current_node,
                reason=exc.reason,
                extra_result={"budget_usage": budget_snap, "document_id": document_id, "phase": "extract"},
                bus=run_bus,
                commit=True,
            )
            return
        except Exception as exc:  # noqa: BLE001
            budget_snap = guard.snapshot()
            err_msg = f"{type(exc).__name__}: {exc}"
            db.expire_all()
            doc = db.get(SourceDocumentModel, document_id)
            if doc:
                doc.status = "EXTRACTION_FAILED"
                db.add(
                    DocumentArchitectureModel(
                        id=str(uuid.uuid4()),
                        document_id=document_id,
                        status="EXTRACTION_FAILED",
                        plan_json={"stage": "EXTRACTION", "result": {"table_ids": [], "code_set_ids": [], "errors": [{"error": err_msg}]}},
                        warnings_json=[err_msg],
                        trace_json={"table_count": 0, "code_set_count": 0, "budget_usage": budget_snap},
                    )
                )
            progress_finish(document_id, status="FAILED", detail=err_msg)
            err_details = (
                {
                    "stage": current_node,
                    "error_type": type(exc).__name__,
                    "error_kind": "budget",
                    "metric": exc.metric,
                    "used": exc.used,
                    "limit": exc.limit,
                    "budget_snapshot": exc.snapshot,
                }
                if isinstance(exc, BudgetExceededError)
                else {"stage": current_node, "error_type": type(exc).__name__, "error_kind": "ingestion"}
            )
            run = db.get(RunModel, run_id) or run
            kernel_fail_run(
                db,
                run,
                node=current_node,
                error=err_msg,
                error_details=err_details,
                extra_result={"budget_usage": budget_snap, "document_id": document_id, "phase": "extract"},
                bus=run_bus,
                commit=True,
            )
            return

        budget_snap = guard.snapshot()
        db.expire_all()
        run = db.get(RunModel, run_id) or run
        doc = db.get(SourceDocumentModel, document_id)
        if doc:
            doc.status = final_status
        db.add(
            DocumentArchitectureModel(
                id=str(uuid.uuid4()),
                document_id=document_id,
                status=final_status,
                plan_json={"stage": "EXTRACTION", "result": result},
                warnings_json=[item.get("error", "解析未完成") for item in result.get("errors", [])],
                trace_json={
                    "table_count": len(result.get("table_ids", [])),
                    "code_set_count": len(result.get("code_set_ids", [])),
                    "budget_usage": budget_snap,
                },
            )
        )

        tables = len(result.get("table_ids", []))
        code_sets = len(result.get("code_set_ids", []))
        if final_status == "EXTRACTION_FAILED":
            err_detail = result["errors"][0]["error"] if result.get("errors") else "解析失败"
            progress_finish(document_id, status="FAILED", detail=err_detail)
            kernel_fail_run(
                db,
                run,
                node="parallel_extraction",
                error=err_detail,
                extra_result={"budget_usage": budget_snap, "document_id": document_id, "phase": "extract", "result": result},
                bus=run_bus,
                commit=True,
            )
        else:
            summary_text = f"解析完成：{tables} 张报表、{code_sets} 张码表"
            final_counters = {"tables": tables, "code_sets": code_sets, "blocks_done": total, "blocks_total": total}
            progress_update(document_id, counters=final_counters, log=summary_text)
            progress_finish(document_id, status="DONE", detail=summary_text)
            append_event(
                db,
                run,
                "INGESTION_COMPLETED",
                "parallel_extraction",
                {
                    "stage": 4,
                    "progress_status": "DONE",
                    "detail": summary_text,
                    "counters": final_counters,
                    "document_status": final_status,
                    "budget_usage": budget_snap,
                    "result": result,
                },
                bus=run_bus,
            )
            run_bus.fire("run.completed", {"run_id": run.id, "node": "parallel_extraction", "status": "COMPLETED"})
            db.commit()

    kernel_submit_run(
        run_id,
        sf,
        _drive_extract,
        allowed_statuses=("NEW", "RUNNING"),
        projector=project_ingestion,
        runner=runner,
    )
    return run_id


def cancel_document_run(
    session: Session,
    document_id: str,
    reason: str = "文档解析已由操作员中止",
) -> Dict[str, Any]:
    """协作式取消文档当前活跃的 Step 1 Ingestion Run。"""
    doc = session.get(SourceDocumentModel, document_id)
    if not doc:
        raise WorkflowError("Document not found")
    rows = (
        session.query(RunModel)
        .filter(RunModel.spec_id == document_id)
        .order_by(RunModel.created_at.desc())
        .all()
    )
    if not rows:
        raise WorkflowError("该文档当前没有运行中的解析任务")
    reg = run_registry()
    states = project_ingestion_many(session, rows)
    run = next(
        (r for r in rows if reg.is_active(r.id) or states[r.id].status in ACTIVE_INGESTION_STATUSES),
        rows[0],
    )
    try:
        res = kernel_cancel_run(
            session,
            run,
            active_statuses=ACTIVE_INGESTION_STATUSES,
            node="cancel",
            reason=reason,
            projector=project_ingestion,
            commit=True,
        )
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    if res.get("cancelled"):
        if doc.status == "EXTRACTING":
            doc.status = "EXTRACTION_FAILED"
            session.commit()
        progress_finish(document_id, status="FAILED", detail=f"已人工中止：{reason}")
    return {**res, "document_id": document_id}


def document_progress_view(session: Session, document_id: str) -> Dict[str, Any]:
    """统一文档进度视图：优先实时内存态，重启或内存清空后自动回落至 EventLog 折叠态。"""
    document = session.get(SourceDocumentModel, document_id)
    if not document:
        raise WorkflowError("Document not found")

    block_total = session.query(RawBlockModel).filter(RawBlockModel.document_id == document_id).count()
    table_total = session.query(TableMetadataModel).filter(TableMetadataModel.document_id == document_id).count()
    field_total = (
        session.query(FieldMetadataModel)
        .join(TableMetadataModel, FieldMetadataModel.table_id == TableMetadataModel.id)
        .filter(TableMetadataModel.document_id == document_id)
        .count()
    )
    code_set_total = len(
        {
            row.code_set_id
            for row in session.query(FieldCodeReferenceModel)
            .join(FieldMetadataModel, FieldCodeReferenceModel.field_id == FieldMetadataModel.id)
            .join(TableMetadataModel, FieldMetadataModel.table_id == TableMetadataModel.id)
            .filter(TableMetadataModel.document_id == document_id)
            .all()
        }
    )

    counters: Dict[str, Any] = {
        "blocks": block_total,
        "tables": table_total,
        "fields": field_total,
        "code_sets": code_set_total,
    }

    run = latest_document_run(session, document_id)
    run_state = project_ingestion(session, run) if run else None
    run_result = (run_state.result_json or {}) if run_state else {}
    budget_usage = run_result.get("budget_usage")
    run_id = run.id if run else None
    is_active_run = bool(
        run and (run_registry().is_active(run.id) or (run_state and run_state.status in ACTIVE_INGESTION_STATUSES))
    )

    snapshot = progress_snapshot(document_id)
    if snapshot:
        for key, value in snapshot.get("counters", {}).items():
            if key in {"blocks", "tables", "fields", "code_sets"}:
                continue
            counters[key] = value
        return {
            "document_id": document_id,
            "run_id": run_id,
            "file_name": snapshot["file_name"],
            "status": snapshot["status"],
            "stage": snapshot["stage"],
            "detail": snapshot["detail"],
            "counters": counters,
            "logs": snapshot["logs"],
            "stages": snapshot["stages"],
            "elapsed": snapshot["elapsed"],
            "idle": snapshot["idle"],
            "started_at": snapshot["started_at_iso"],
            "document_status": document.status,
            "processing": snapshot["status"] == "WORKING",
            "can_cancel": snapshot["status"] == "WORKING" and is_active_run,
            "cancelled": bool(run_result.get("cancelled")),
            "budget_usage": budget_usage,
        }

    # 进程重启后从不可变 EventLog 折叠态无损还原进度、日志与成本快照
    if run and run_state:
        for key, value in (run_result.get("counters") or {}).items():
            if key in {"blocks", "tables", "fields", "code_sets"}:
                continue
            counters[key] = value
        prog_status = str(run_result.get("progress_status") or ("WORKING" if run_state.status in ACTIVE_INGESTION_STATUSES else "DONE" if run_state.status == "COMPLETED" else "FAILED"))
        return {
            "document_id": document_id,
            "run_id": run.id,
            "file_name": run_result.get("file_name") or document.file_name,
            "status": prog_status,
            "stage": int(run_result.get("stage") or 0),
            "detail": run_result.get("detail") or "",
            "counters": counters,
            "logs": list(run_result.get("logs") or []),
            "stages": _stages_catalog(),
            "elapsed": float((budget_usage or {}).get("elapsed_seconds") or 0),
            "idle": 0,
            "started_at": run.created_at.strftime("%H:%M:%S") if run.created_at else "",
            "document_status": document.status,
            "processing": prog_status == "WORKING",
            "can_cancel": is_active_run,
            "cancelled": bool(run_result.get("cancelled")),
            "budget_usage": budget_usage,
        }

    return {
        "document_id": document_id,
        "run_id": None,
        "file_name": document.file_name,
        "status": "IDLE",
        "stage": 0,
        "detail": "",
        "counters": counters,
        "logs": [],
        "stages": [],
        "elapsed": 0,
        "idle": 0,
        "started_at": "",
        "document_status": document.status,
        "processing": False,
        "can_cancel": False,
        "cancelled": False,
        "budget_usage": None,
    }


def recover_interrupted_ingestion_runs(session: Optional[Session] = None) -> List[str]:
    """进程重启恢复：回收所有遗留 RUNNING 态的 Step 1 文档解析 Run 与卡死的 EXTRACTING 文档状态。"""
    def _run(db: Session) -> List[str]:
        doc_ids = {row[0] for row in db.query(SourceDocumentModel.id).all()}
        runs = db.query(RunModel).filter(RunModel.spec_id.in_(doc_ids)).all() if doc_ids else []
        states = project_ingestion_many(db, runs)
        recovered: List[str] = []
        for run in runs:
            state = states[run.id]
            if state.status in ACTIVE_INGESTION_STATUSES:
                kernel_fail_run(
                    db,
                    run,
                    node=state.current_node or "recovery",
                    error="服务重启，文档解析任务已自动标记中断，请重新发起解析。",
                    error_details={
                        "stage": state.current_node or "recovery",
                        "error_type": "ProcessInterrupted",
                        "error_kind": "interrupted",
                        "suggestion": "后台服务重启导致文档解析中断，请重新点击解析。",
                    },
                    extra_result={"document_id": run.spec_id, "recovered": True},
                    commit=False,
                )
                recovered.append(run.id)
        stuck_docs = db.query(SourceDocumentModel).filter(SourceDocumentModel.status == "EXTRACTING").all()
        for doc in stuck_docs:
            doc.status = "EXTRACTION_FAILED"
        if recovered or stuck_docs:
            db.commit()
        return recovered

    if session is not None:
        return _run(session)
    with SessionLocal() as db:
        return _run(db)
