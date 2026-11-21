"""Step 1 文档导入与解析（Ingestion Run）的事件折叠策略（策略层 Layer 2）。

复用微内核 `app.kernel.event_log.project_with` 与 `app.kernel.run`，将 Step 1 的
“结构体检 + 初步切块（prepare）”与“并发字段/码表提取（extract）”统一纳入不可变事件账本：
- 服务重启后可从 `EventLog` 无损恢复阶段、进度计数、日志与 `budget_usage`；
- 与 Step 2 规则生成 Run 共享 `RunModel` + `EventModel` + `RunRegistry` + `BudgetGuard`，
  通过 `RUN_CREATED.payload["run_kind"] == "INGESTION"` 明确区分任务类型。
"""

from dataclasses import replace
from typing import Any, Dict, Iterable, List

from sqlalchemy.orm import Session

from app.infra.models import RunModel
from app.kernel.event_log import RunState, project_many_with, project_with

ACTIVE_INGESTION_STATUSES = frozenset({"NEW", "RUNNING", "WORKING"})
_MAX_FOLDED_LOGS = 40


def _merge_ingestion_result(base: Any, patch: Dict[str, Any]) -> Dict[str, Any]:
    current = dict(base) if isinstance(base, dict) else {}
    counters = dict(current.get("counters") or {})
    if isinstance(patch.get("counters"), dict):
        counters.update(patch["counters"])
    logs: List[Dict[str, Any]] = list(current.get("logs") or [])
    if patch.get("log"):
        entry = patch["log"]
        if isinstance(entry, str):
            logs.append({"at": patch.get("at") or 0, "text": entry, "stage": patch.get("stage", current.get("stage", 0))})
        elif isinstance(entry, dict):
            logs.append(entry)
        if len(logs) > _MAX_FOLDED_LOGS:
            logs = logs[-_MAX_FOLDED_LOGS:]
    merged = {**current, **{k: v for k, v in patch.items() if k not in {"counters", "log"}}}
    merged["counters"] = counters
    merged["logs"] = logs
    return merged


def fold_ingestion_event(state: RunState, kind: str, payload: Dict[str, Any]) -> RunState:
    """将单条 Step 1 文档解析事件折叠为新的 RunState（纯函数）。"""
    payload = payload or {}
    if kind == "RUN_CREATED":
        base = {
            "run_kind": payload.get("run_kind") or "INGESTION",
            "document_id": payload.get("document_id"),
            "file_name": payload.get("file_name") or "",
            "phase": payload.get("phase") or "prepare",
            "stage": int(payload.get("stage") or 1),
            "progress_status": "WORKING",
            "detail": payload.get("detail") or "任务已创建，准备执行",
            "counters": dict(payload.get("counters") or {}),
            "logs": [],
        }
        return replace(
            state,
            status="RUNNING",
            current_node=payload.get("phase") or "created",
            result_json=base,
            when="always",
        )
    if kind == "INGESTION_STAGE":
        patch: Dict[str, Any] = {}
        for key in ("stage", "detail", "counters", "log", "at", "file_name", "phase", "budget_usage"):
            if key in payload and payload[key] is not None:
                patch[key] = payload[key]
        if payload.get("progress_status"):
            patch["progress_status"] = payload["progress_status"]
        merged = _merge_ingestion_result(state.result_json, patch)
        return replace(
            state,
            status="RUNNING",
            current_node=payload.get("node") or state.current_node,
            result_json=merged,
            when="always",
        )
    if kind == "INGESTION_COMPLETED":
        patch = {
            "progress_status": payload.get("progress_status") or "DONE",
            "detail": payload.get("detail") or "解析完成",
            "stage": int(payload.get("stage") or 4),
        }
        for key in ("counters", "log", "at", "budget_usage", "result", "document_status"):
            if key in payload and payload[key] is not None:
                patch[key] = payload[key]
        merged = _merge_ingestion_result(state.result_json, patch)
        return replace(
            state,
            status="COMPLETED",
            current_node=payload.get("node") or "completed",
            result_json=merged,
            when="always",
        )
    if kind == "RUN_CANCELLED":
        reason = payload.get("reason") or (payload.get("result") or {}).get("error") or "Run cancelled by operator"
        res_patch = dict(payload.get("result") or {})
        res_patch.setdefault("error", reason)
        res_patch["cancelled"] = True
        res_patch["progress_status"] = "FAILED"
        res_patch["detail"] = f"已人工中止：{reason}"
        merged = _merge_ingestion_result(state.result_json, res_patch)
        return replace(
            state,
            status="FAILED",
            current_node=payload.get("node") or state.current_node,
            result_json=merged,
        )
    if kind == "RUN_FAILED":
        res_patch = dict(payload.get("result") or {})
        res_patch["progress_status"] = "FAILED"
        if res_patch.get("error") and not res_patch.get("detail"):
            res_patch["detail"] = str(res_patch["error"])
        merged = _merge_ingestion_result(state.result_json, res_patch)
        return replace(
            state,
            status="FAILED",
            current_node=payload.get("node") or state.current_node,
            result_json=merged,
        )
    return state


def project_ingestion(session: Session, run: RunModel) -> RunState:
    """折叠单条文档导入 Run 的最新状态。"""
    return project_with(session, run, reducer=fold_ingestion_event)


def project_ingestion_many(session: Session, runs: Iterable[RunModel]) -> Dict[str, RunState]:
    """批量折叠多条文档导入 Run 的状态。"""
    return project_many_with(session, runs, reducer=fold_ingestion_event)
