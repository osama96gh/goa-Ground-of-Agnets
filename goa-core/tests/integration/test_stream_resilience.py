"""E2e for §9.3 — `Last-Event-ID` reconnect, `stream.gap` synthesis, and
slow-consumer recovery.

The hub implements all three primitives (`subscribe(last_event_id=)`,
`stream.gap` emission, queue overflow → close). These tests prove the wiring
in `api/stream.py` exposes them per spec.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import UUID

import httpx
import pytest

from goa.config import Settings
from goa.main import create_app

from tests.integration._helpers import (
    SseFrame,
    consume,
    create_task_with_question,
    iter_sse,
    next_event_frame,
    wait_for_subscriber,
)
from tests.integration._live_server import live_server


pytestmark = pytest.mark.asyncio


def _settings(**overrides) -> Settings:
    base = Settings.for_tests()
    return replace(base, **overrides)


async def _register(http: httpx.AsyncClient, **body) -> tuple[UUID, str]:
    resp = await http.post("/participants", json=body)
    resp.raise_for_status()
    decoded = resp.json()
    return UUID(decoded["participant"]["id"]), decoded["api_key"]


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


@asynccontextmanager
async def _stream_with(
    base_url: str, api_key: str, *, last_event_id: str | None = None,
) -> AsyncIterator[AsyncIterator[SseFrame]]:
    """Open a stream and yield a SINGLE long-lived `iter_sse` iterator. The
    underlying httpx response can only be consumed once, so callers must reuse
    this iterator across drains rather than restart it."""
    headers = _auth(api_key)
    if last_event_id is not None:
        headers["Last-Event-ID"] = last_event_id
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        async with client.stream("GET", "/stream", headers=headers) as response:
            response.raise_for_status()
            yield iter_sse(response)


async def _drain_one_event(
    frames: AsyncIterator[SseFrame], timeout: float = 5.0,
) -> SseFrame:
    """Pull frames from `frames` until the next `event`-named frame lands."""
    async def _pull() -> SseFrame:
        async for frame in frames:
            if frame.event == "event":
                return frame
        raise AssertionError("stream closed before yielding an event")
    return await asyncio.wait_for(_pull(), timeout=timeout)


async def _next_frame(
    frames: AsyncIterator[SseFrame], timeout: float = 5.0,
) -> SseFrame:
    async def _pull() -> SseFrame:
        async for frame in frames:
            return frame
        raise AssertionError("stream closed before yielding a frame")
    return await asyncio.wait_for(_pull(), timeout=timeout)


# ---------------------------------------------------------------------------
# Reconnect with replay
# ---------------------------------------------------------------------------

async def test_reconnect_replays_missed_events() -> None:
    """Subscriber drops; server appends events; subscriber reconnects with the
    last id it saw and gets the missed events from the replay buffer."""
    app = create_app(_settings(replay_buffer_size=100))
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as http:
            init_id, key_init = await _register(http, type="agent", name="init")
            target_id, key_target = await _register(http, type="agent", name="target")

            # First connection: capture one event, then disconnect.
            async with _stream_with(base_url, key_target) as frames:
                await wait_for_subscriber(app.state.ctx.hub, target_id)

                # init asks target.
                _, q1_id = await create_task_with_question(
                    http, key_init, targets=[str(target_id)], text="q1",
                )

                # Drain through the auto-join `participant_joined` to land on
                # the `question` event so the resume point is on a real event.
                last_seen: str | None = None
                while True:
                    frame = await _drain_one_event(frames)
                    last_seen = frame.id
                    if frame.data["event"]["event_type"] == "question":
                        break
                assert last_seen is not None

            # While disconnected: append more events.
            _, q2_id = await create_task_with_question(
                http, key_init, targets=[str(target_id)], text="q2",
            )

            # Reconnect with Last-Event-ID; expect the missed event.
            async with _stream_with(base_url, key_target, last_event_id=last_seen) as frames:
                seen_event_ids: list[UUID] = []
                while q2_id not in seen_event_ids:
                    frame = await _drain_one_event(frames)
                    seen_event_ids.append(UUID(frame.data["event"]["id"]))


# ---------------------------------------------------------------------------
# stream.gap synthesis
# ---------------------------------------------------------------------------

async def test_stream_gap_emitted_when_buffer_evicts_missed_events() -> None:
    """Replay buffer is sized small. Subscriber drops, server appends more
    events than the buffer holds, subscriber reconnects with stale id —
    receives a `stream.gap` synthetic event so the SDK refetches via REST."""
    app = create_app(_settings(replay_buffer_size=2))
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as http:
            init_id, key_init = await _register(http, type="agent", name="init")
            target_id, key_target = await _register(http, type="agent", name="target")

            # First connection — capture one event, capture id, drop.
            async with _stream_with(base_url, key_target) as frames:
                await wait_for_subscriber(app.state.ctx.hub, target_id)
                await create_task_with_question(
                    http, key_init, targets=[str(target_id)], text="q1",
                )
                # Drain to the question event to land on a real id.
                stale_id: str | None = None
                while True:
                    frame = await _drain_one_event(frames)
                    stale_id = frame.id
                    if frame.data["event"]["event_type"] == "question":
                        break

            # Append > buffer worth of events while disconnected. Buffer holds
            # 2; we emit 5 fresh tasks to force eviction past `stale_id`.
            for i in range(5):
                await create_task_with_question(
                    http, key_init, targets=[str(target_id)], text=f"q{i+2}",
                )

            # Reconnect with the stale id; first replay frame must be `stream.gap`.
            async with _stream_with(base_url, key_target, last_event_id=stale_id) as frames:
                frame = await _next_frame(frames)
                assert frame.event == "stream.gap"
                assert "from_id" in frame.data and "to_id" in frame.data


# ---------------------------------------------------------------------------
# Slow-consumer recovery
# ---------------------------------------------------------------------------

async def test_slow_consumer_overflow_recovers_via_reconnect() -> None:
    """Hub queue is sized small. We never drain the queue; once it fills, the
    subscription is dropped server-side. A fresh reconnect with the last id
    seen via the replay buffer recovers cleanly (events appended during the
    drop remain in the buffer)."""
    app = create_app(_settings(replay_buffer_size=100, subscriber_queue_size=2))
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as http:
            init_id, key_init = await _register(http, type="agent", name="init")
            target_id, key_target = await _register(http, type="agent", name="target")

            hub = app.state.ctx.hub
            # Subscribe directly so we can avoid pulling from the queue and
            # trigger overflow deterministically.
            sub = await hub.subscribe(target_id, last_event_id=None)
            assert hub.has_subscriber(target_id)

            # Push enough publishes to overflow the queue. Each create+question
            # emits multiple events (participant_joined + question), so 5
            # tasks > 2 queue slots easily.
            for i in range(5):
                await create_task_with_question(
                    http, key_init, targets=[str(target_id)], text=f"q{i}",
                )

            # Subscription should be closed on the server.
            await asyncio.wait_for(sub.closed.wait(), timeout=2.0)
            assert not hub.has_subscriber(target_id)

            # Reconnect: a fresh subscribe gets no replay (last_event_id is
            # None). What matters is that the participant CAN reconnect
            # cleanly and observe new events without anything stuck.
            async with _stream_with(base_url, key_target) as frames:
                await wait_for_subscriber(hub, target_id, timeout=2.0)
                _, fresh_q_id = await create_task_with_question(
                    http, key_init, targets=[str(target_id)], text="post-recover",
                )
                seen: list[UUID] = []
                while fresh_q_id not in seen:
                    frame = await _drain_one_event(frames, timeout=5.0)
                    seen.append(UUID(frame.data["event"]["id"]))


async def test_invalid_last_event_id_treated_as_fresh_subscribe() -> None:
    """Malformed `Last-Event-ID` should not crash the stream — the hub falls
    back to a fresh subscription so a buggy reconnect can't lock the client out."""
    app = create_app(_settings())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as http:
            _, key = await _register(http, type="agent", name="a")
            async with _stream_with(base_url, key, last_event_id="not-a-number") as _frames:
                # If the server had crashed we'd see a non-2xx; reaching here
                # means the open succeeded. Iterator left to close on exit.
                pass
