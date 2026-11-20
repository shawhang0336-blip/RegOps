"""导入与解析过程的细粒度进度。

这些状态只活在本进程里：当前部署是单 worker 的 uvicorn，够用；
如果以后要多 worker，把 _STATE 换成 Redis 就行，调用点不用改。
"""

import threading
import time
from typing import Any, Dict, List, Optional

from .bus import global_bus

_LOCK = threading.Lock()
_STATE: Dict[str, Dict[str, Any]] = {}

MAX_LOGS = 40

STAGE_LABELS = {
    1: ("校验并保存文件", "检查格式、大小和文件指纹"),
    2: ("检查整份文件结构", "AI 判断文档结构与切块方式"),
    3: ("程序执行初步切块", "按文件类型生成初始内容块"),
    4: ("提取报表字段和码表", "自动解析并复核，完成后直接进入结果"),
}


def _now() -> float:
    return time.time()


def start(document_id: str, file_name: str, *, auto_parse: bool = False) -> None:
    with _LOCK:
        _STATE[document_id] = {
            "document_id": document_id,
            "file_name": file_name,
            "stage": 1,
            "status": "WORKING",
            "detail": "正在校验文件格式与指纹",
            "counters": {},
            "logs": [],
            "started_at": _now(),
            "updated_at": _now(),
            "auto_parse": auto_parse,
        }
    global_bus().fire(
        "document.progress",
        {
            "document_id": document_id,
            "file_name": file_name,
            "stage": 1,
            "status": "WORKING",
            "detail": "正在校验文件格式与指纹",
        },
    )


def ensure(document_id: str, file_name: str = "") -> None:
    """没有进行中的记录时补一条（老文档重新解析、页面刷新后直接点解析）。"""
    with _LOCK:
        if document_id in _STATE and _STATE[document_id]["status"] == "WORKING":
            return
    start(document_id, file_name)


def update(
    document_id: str,
    *,
    stage: Optional[int] = None,
    status: Optional[str] = None,
    detail: Optional[str] = None,
    counters: Optional[Dict[str, Any]] = None,
    log: Optional[str] = None,
) -> None:
    if not document_id:
        return
    with _LOCK:
        state = _STATE.get(document_id)
        if state is None:
            return
        if stage is not None:
            state["stage"] = stage
        if status is not None:
            state["status"] = status
        if detail is not None:
            state["detail"] = detail
        if counters:
            state["counters"].update(counters)
        if log:
            logs: List[Dict[str, Any]] = state["logs"]
            logs.append({"at": _now(), "text": log, "stage": state["stage"]})
            del logs[:-MAX_LOGS]
        state["updated_at"] = _now()
        snapshot_event = {
            "document_id": document_id,
            "file_name": state.get("file_name", ""),
            "stage": state["stage"],
            "status": state["status"],
            "detail": state["detail"],
            "log": log,
        }
    global_bus().fire("document.progress", snapshot_event)


def finish(document_id: str, *, status: str = "DONE", detail: Optional[str] = None) -> None:
    update(document_id, status=status, detail=detail, stage=4 if status == "DONE" else None)


def snapshot(document_id: str) -> Optional[Dict[str, Any]]:
    with _LOCK:
        state = _STATE.get(document_id)
        if state is None:
            return None
        copy_state = {
            **state,
            "counters": dict(state["counters"]),
            "logs": list(state["logs"]),
        }
    now = _now()
    copy_state["elapsed"] = round(now - copy_state["started_at"], 1)
    copy_state["idle"] = round(now - copy_state["updated_at"], 1)
    copy_state["started_at_iso"] = time.strftime("%H:%M:%S", time.localtime(copy_state["started_at"]))
    copy_state["stages"] = [
        {"index": index, "label": label, "description": description}
        for index, (label, description) in STAGE_LABELS.items()
    ]
    return copy_state


def is_running(document_id: str) -> bool:
    with _LOCK:
        state = _STATE.get(document_id)
        return bool(state and state["status"] == "WORKING")


def forget(document_id: str) -> None:
    with _LOCK:
        _STATE.pop(document_id, None)
