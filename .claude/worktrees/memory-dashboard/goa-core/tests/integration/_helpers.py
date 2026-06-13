"""Shared SSE / live-server helpers for integration tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from goa.stream.hub import InMemoryStreamHub


async def create_task_with_question(
    http: httpx.AsyncClient,
    api_key: str,
    *,
    targets: list[str],
    subject: str = "",
    parent_task_id: str | None = None,
    text: str = "?",
    metadata: dict | None = None,
) -> tuple[UUID, UUID]:
    """Helper — `POST /tasks` then `POST /tasks/{id}/events` for a question.
    Returns `(task_id, question_event_id)`."""
    body: dict[str, Any] = {"subject": subject, "metadata": metadata or {}}
    if parent_task_id is not None:
        body["parent_task_id"] = parent_task_id
    create = await http.post(
        "/tasks", headers={"Authorization": f"Bearer {api_key}"}, json=body,
    )
    create.raise_for_status()
    task_id = UUID(create.json()["task"]["id"])
    q = await http.post(
        f"/tasks/{task_id}/events",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "event_type": "question",
            "payload": {"to": targets},
            "content": {"text": text},
            "in_reply_to": None,
            "metadata": {},
        },
    )
    q.raise_for_status()
    question_id = UUID(q.json()["event"]["id"])
    return task_id, question_id


@dataclass
class SseFrame:
    event: str
    id: str | None
    data: Any


async def iter_sse(response: httpx.Response) -> AsyncIterator[SseFrame]:
    name = "message"
    eid: str | None = None
    parts: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if parts or name != "message":
                raw = "\n".join(parts)
                try:
                    data: Any = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    data = raw
                yield SseFrame(event=name, id=eid, data=data)
            name, eid, parts = "message", None, []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":") if ":" in line else (line, "", "")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            name = value
        elif field == "id":
            eid = value
        elif field == "data":
            parts.append(value)


async def consume(
    base_url: str,
    api_key: str,
    queue: asyncio.Queue[SseFrame],
    started: asyncio.Event,
) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        async with client.stream(
            "GET",
            "/stream",
            headers={"Authorization": f"Bearer {api_key}"},
        ) as response:
            response.raise_for_status()
            started.set()
            async for frame in iter_sse(response):
                await queue.put(frame)


async def wait_for_subscriber(
    hub: InMemoryStreamHub, participant_id: UUID, timeout: float = 5.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if hub.has_subscriber(participant_id):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"subscriber for {participant_id} never registered")


async def next_event_frame(queue: asyncio.Queue[SseFrame], timeout: float = 5.0) -> SseFrame:
    """Drain `ping` frames; return the first `event` frame."""
    while True:
        frame = await asyncio.wait_for(queue.get(), timeout=timeout)
        if frame.event == "event":
            return frame


async def drain_until_event_type(
    queue: asyncio.Queue[SseFrame],
    event_type: str,
    timeout: float = 5.0,
) -> SseFrame:
    """Read event frames until one matches `event_type`. Drops earlier frames.

    Useful when fan-out order isn't precisely controllable (e.g. multiple
    auto-joins racing the question fan-out) and the test only cares about a
    specific downstream event.
    """
    while True:
        frame = await next_event_frame(queue, timeout=timeout)
        if frame.data["event"]["event_type"] == event_type:
            return frame
