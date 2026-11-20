"""Bus SSE 实时事件流单元测试：验证按 run_id / document_id / topic 过滤与断开自动退订。"""

import asyncio
import json

from app.api.system import stream_bus_events
from app.kernel.bus import Bus


def test_sse_stream_filters_events_and_unsubscribes_on_close():
    bus = Bus()

    async def scenario():
        gen = stream_bus_events(
            bus=bus,
            run_id="run-1",
            topics="node.enter,run.completed",
            keepalive_seconds=1.0,
        )
        first = await gen.__anext__()
        assert first == ": connected\n\n"
        assert len(bus._subscribers["*"]) == 1

        # 不匹配的事件（不同 run_id / 不同 topic）应被过滤
        bus.fire("node.enter", {"run_id": "run-2", "node": "plan"})
        bus.fire("tool.before", {"run_id": "run-1", "tool": "assign_rules"})
        # 匹配的事件应立即投递
        bus.fire("node.enter", {"run_id": "run-1", "node": "rule_binder"})

        frame = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        assert "event: node.enter\n" in frame
        data_line = next(line for line in frame.splitlines() if line.startswith("data: "))
        payload = json.loads(data_line[len("data: "):])
        assert payload["run_id"] == "run-1"
        assert payload["node"] == "rule_binder"

        await gen.aclose()
        assert bus._subscribers["*"] == []

    asyncio.run(scenario())


def test_sse_stream_replays_matching_document_progress():
    bus = Bus()
    bus.fire("document.progress", {"document_id": "doc-a", "stage": 2, "status": "WORKING"})
    bus.fire("document.progress", {"document_id": "doc-b", "stage": 2, "status": "WORKING"})

    async def scenario():
        gen = stream_bus_events(
            bus=bus,
            document_id="doc-a",
            topics="document.progress",
            replay=True,
            keepalive_seconds=0.05,
        )
        assert await gen.__anext__() == ": connected\n\n"
        replayed = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        assert "event: document.progress\n" in replayed
        assert '"document_id": "doc-a"' in replayed
        keepalive = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
        assert keepalive == ": keepalive\n\n"
        await gen.aclose()
        assert bus._subscribers["*"] == []

    asyncio.run(scenario())
