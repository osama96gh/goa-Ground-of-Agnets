from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class SseFrame:
    """Decoded SSE frame. `data` is parsed JSON when possible, raw text
    otherwise (e.g. `ping` data is `"{}"`)."""

    event: str
    id: str | None
    data: Any


async def iter_sse(response: httpx.Response) -> AsyncIterator[SseFrame]:
    """Parse an in-flight SSE stream into SseFrame objects.

    Implements the minimum SSE rules needed by Goa: one event per
    blank-line-terminated block, fields are `event`, `id`, `data` (joined by
    newline if repeated). Comment lines (starting with `:`) are ignored.
    """

    event_name = "message"
    event_id: str | None = None
    data_parts: list[str] = []

    async for line in response.aiter_lines():
        if line == "":
            if data_parts or event_name != "message":
                raw = "\n".join(data_parts)
                try:
                    parsed: Any = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    parsed = raw
                yield SseFrame(event=event_name, id=event_id, data=parsed)
            event_name = "message"
            event_id = None
            data_parts = []
            continue
        if line.startswith(":"):
            continue
        if ":" in line:
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""
        if field == "event":
            event_name = value
        elif field == "id":
            event_id = value
        elif field == "data":
            data_parts.append(value)
        # other fields (`retry`) are ignored.
