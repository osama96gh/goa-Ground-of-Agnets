from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Request
from sse_starlette.sse import EventSourceResponse

from goa.deps import AppContext, get_ctx, make_bearer_dependency
from goa.domain.models import Participant


router = APIRouter()
require_participant = make_bearer_dependency()


def _parse_last_event_id(raw: str | None) -> int | None:
    """`Last-Event-ID` is an int per StreamHub's id allocation. Bad values are
    silently ignored (treated as fresh subscribe) so a malformed reconnect
    can't crash the stream — the hub will just deliver from now."""
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@router.get("/stream")
async def stream(
    request: Request,
    caller: Participant = Depends(require_participant),
    ctx: AppContext = Depends(get_ctx),
    last_event_id_header: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    last_event_id = _parse_last_event_id(last_event_id_header)

    async def gen() -> AsyncIterator[dict[str, str | int]]:
        sub = await ctx.hub.subscribe(caller.id, last_event_id=last_event_id)
        try:
            for ev in sub.replay:
                yield {
                    "event": ev.event,
                    "id": str(ev.stream_event_id),
                    "data": json.dumps(ev.data),
                }

            ping_interval = ctx.settings.ping_interval_seconds
            while True:
                if await request.is_disconnected() or sub.closed.is_set():
                    return
                try:
                    ev = await asyncio.wait_for(sub.queue.get(), timeout=ping_interval)
                except asyncio.TimeoutError:
                    ping_id = await ctx.hub.allocate_id(caller.id)
                    yield {
                        "event": "ping",
                        "id": str(ping_id),
                        "data": "{}",
                    }
                    continue
                yield {
                    "event": ev.event,
                    "id": str(ev.stream_event_id),
                    "data": json.dumps(ev.data),
                }
        finally:
            sub.close()

    return EventSourceResponse(gen())
