"""The server-sent event stream.

One ``EventSource`` per browser tab; every session event (cells, trace steps,
progress jobs, the map, the file list, settings) arrives here. A keepalive
comment is emitted while idle so proxies and the browser do not time the stream
out, and a disconnected client is dropped on its next tick.
"""

from __future__ import annotations

import asyncio
import json
import queue

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..deps import app_state

router = APIRouter(tags=["events"])

#: Seconds a poll waits for an event before emitting a keepalive.
POLL_TIMEOUT = 0.5


@router.get("/api/events")
async def api_events(request: Request) -> StreamingResponse:
    """Stream session events to the browser."""
    state = app_state()
    subscriber = state.subscribe()

    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.to_thread(subscriber.get, timeout=POLL_TIMEOUT)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {item['event']}\ndata: {json.dumps(item['data'])}\n\n"
        finally:
            state.unsubscribe(subscriber)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
