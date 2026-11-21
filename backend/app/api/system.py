"""系统健康检查、Blob 读取、Bus 实时事件流（SSE）与业务库只读抽样路由（`/api/*`）。"""

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List, Optional, Set

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.delivery.business_db import BusinessDatabaseError, ReadOnlyBusinessDatabase
from app.infra.blob_store import LocalBlobStore
from app.infra.config import get_settings
from app.kernel.bus import Bus, global_bus

router = APIRouter(tags=["system"])

SSE_KEEPALIVE_SECONDS = 15.0


def _parse_topics(topics: Optional[str]) -> Optional[Set[str]]:
    if not topics:
        return None
    items = {item.strip() for item in topics.split(",") if item.strip()}
    if not items or "*" in items:
        return None
    return items


def _matches_event(
    event: Dict[str, Any],
    *,
    run_id: Optional[str] = None,
    document_id: Optional[str] = None,
    topic_set: Optional[Set[str]] = None,
) -> bool:
    if topic_set is not None and event.get("topic") not in topic_set:
        return False
    if run_id and event.get("run_id") != run_id:
        return False
    if document_id and event.get("document_id") != document_id:
        return False
    return True


def _format_sse(event: Dict[str, Any]) -> str:
    topic = str(event.get("topic") or "message").replace("\n", "")
    data = json.dumps(event, ensure_ascii=False, default=str)
    seq = event.get("seq")
    prefix = f"id: {seq}\n" if seq is not None else ""
    return f"{prefix}event: {topic}\ndata: {data}\n\n"


async def stream_bus_events(
    *,
    bus: Optional[Bus] = None,
    run_id: Optional[str] = None,
    document_id: Optional[str] = None,
    topics: Optional[str] = None,
    replay: bool = False,
    keepalive_seconds: float = SSE_KEEPALIVE_SECONDS,
    is_disconnected=None,
) -> AsyncIterator[str]:
    """订阅 Bus 全生命周期主题并产出标准 SSE 文本帧；断开时保证退订防泄漏。"""
    active_bus = bus if bus is not None else global_bus()
    topic_set = _parse_topics(topics)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    dropped_frames = 0

    def _on_event(envelope: Dict[str, Any]) -> None:
        if not _matches_event(envelope, run_id=run_id, document_id=document_id, topic_set=topic_set):
            return

        def _enqueue() -> None:
            nonlocal dropped_frames
            if queue.full():
                try:
                    queue.get_nowait()
                    dropped_frames += 1
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(envelope)
            except asyncio.QueueFull:
                dropped_frames += 1

        try:
            loop.call_soon_threadsafe(_enqueue)
        except RuntimeError:
            pass

    active_bus.subscribe("*", _on_event)
    try:
        yield ": connected\n\n"
        if replay:
            for item in active_bus.history(run_id=run_id):
                if _matches_event(item, run_id=run_id, document_id=document_id, topic_set=topic_set):
                    yield _format_sse(item)
        while True:
            if is_disconnected is not None and await is_disconnected():
                break
            try:
                envelope = await asyncio.wait_for(queue.get(), timeout=keepalive_seconds)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if dropped_frames:
                reported = dropped_frames
                dropped_frames = 0
                yield f": dropped={reported}\n\n"
            yield _format_sse(envelope)
    finally:
        active_bus.unsubscribe("*", _on_event)


@router.get("/api/health")
def health() -> Dict:
    settings = get_settings()
    return {
        "status": "ok",
        "database": settings.database_url.split(":", 1)[0],
        "llm_provider": settings.llm_provider,
        "business_database_configured": bool(settings.business_database_url),
    }


@router.get("/api/events/stream")
async def event_stream(
    request: Request,
    run_id: Optional[str] = Query(default=None),
    document_id: Optional[str] = Query(default=None),
    topics: Optional[str] = Query(default=None),
    replay: bool = Query(default=False),
):
    """实时推送 ProdAgent Bus 全生命周期事件（SSE），取代高频轮询。"""
    generator = stream_bus_events(
        run_id=run_id,
        document_id=document_id,
        topics=topics,
        replay=replay,
        is_disconnected=request.is_disconnected,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/blobs/{digest}")
def get_blob(digest: str) -> Dict:
    settings = get_settings()
    try:
        content = LocalBlobStore(settings.blob_dir).get(f"blob://{digest}").decode("utf-8")
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="Blob not found") from exc
    try:
        return {"digest": digest, "content": content, "json": json.loads(content)}
    except json.JSONDecodeError:
        return {"digest": digest, "content": content, "json": None}


@router.get("/api/business/preview")
def business_preview(table: str = Query(min_length=1), limit: int = Query(default=20, ge=1, le=100)) -> List[Dict]:
    settings = get_settings()
    try:
        return ReadOnlyBusinessDatabase(settings).preview(table, limit)
    except BusinessDatabaseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Business database query failed: {type(exc).__name__}") from exc
