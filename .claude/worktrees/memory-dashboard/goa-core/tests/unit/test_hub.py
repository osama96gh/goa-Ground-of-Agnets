from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from goa.stream.hub import InMemoryStreamHub


@pytest.mark.asyncio
async def test_publish_then_subscribe_replays_when_last_event_id_set() -> None:
    hub = InMemoryStreamHub(replay_buffer_size=10, queue_size=10)
    aid = uuid4()
    await hub.publish(aid, "message", {"n": 1})
    await hub.publish(aid, "message", {"n": 2})

    sub = await hub.subscribe(aid, last_event_id=0)
    try:
        ids = [e.stream_event_id for e in sub.replay]
        assert ids == [1, 2]
        assert [e.data["n"] for e in sub.replay] == [1, 2]
    finally:
        sub.close()


@pytest.mark.asyncio
async def test_subscribe_no_last_event_id_does_not_replay() -> None:
    hub = InMemoryStreamHub()
    aid = uuid4()
    await hub.publish(aid, "message", {"n": 1})
    sub = await hub.subscribe(aid, last_event_id=None)
    try:
        assert sub.replay == []
    finally:
        sub.close()


@pytest.mark.asyncio
async def test_stale_last_event_id_emits_gap() -> None:
    hub = InMemoryStreamHub(replay_buffer_size=2, queue_size=10)
    aid = uuid4()
    # Fill buffer with 5 events; only last 2 remain.
    for i in range(5):
        await hub.publish(aid, "message", {"n": i})
    sub = await hub.subscribe(aid, last_event_id=0)
    try:
        # First replayed must be the gap event (covering [1, 3]).
        first = sub.replay[0]
        assert first.event == "stream.gap"
        assert first.data == {"from_id": 1, "to_id": 3}
        # Then the still-buffered events 4, 5.
        rest = [e for e in sub.replay[1:]]
        assert [e.stream_event_id for e in rest] == [4, 5]
    finally:
        sub.close()


@pytest.mark.asyncio
async def test_slow_consumer_drops_subscription() -> None:
    hub = InMemoryStreamHub(replay_buffer_size=100, queue_size=2)
    aid = uuid4()
    sub = await hub.subscribe(aid, last_event_id=None)
    try:
        await hub.publish(aid, "message", {"n": 1})
        await hub.publish(aid, "message", {"n": 2})
        # Queue is full; this triggers a drop + closed signal.
        await hub.publish(aid, "message", {"n": 3})
        assert not hub.has_subscriber(aid)
        assert sub.closed.is_set()
        # Buffer still has all three events for replay on reconnect.
        snap = hub.buffer_snapshot(aid)
        assert [e.data["n"] for e in snap] == [1, 2, 3]
    finally:
        sub.close()


@pytest.mark.asyncio
async def test_publish_delivers_to_active_subscriber() -> None:
    hub = InMemoryStreamHub()
    aid = uuid4()
    sub = await hub.subscribe(aid, last_event_id=None)
    try:
        await hub.publish(aid, "message", {"n": 42})
        item = await asyncio.wait_for(sub.queue.get(), timeout=1.0)
        assert item.event == "message"
        assert item.data == {"n": 42}
    finally:
        sub.close()


@pytest.mark.asyncio
async def test_allocate_id_does_not_buffer() -> None:
    hub = InMemoryStreamHub()
    aid = uuid4()
    n1 = await hub.allocate_id(aid)
    n2 = await hub.allocate_id(aid)
    assert n2 == n1 + 1
    assert hub.buffer_snapshot(aid) == []
