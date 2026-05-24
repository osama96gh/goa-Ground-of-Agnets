from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class Event:
    """A single SSE-bound event for one stream (per-agent or admin firehose)."""

    stream_event_id: int
    event: str  # "message" | "ping" | "stream.gap"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Subscription:
    """A live subscription to one event stream.

    Caller drains events from `queue` and watches `closed` for shutdown.
    `replay` holds events to yield before draining the queue, including any
    synthetic `stream.gap` event.
    """

    queue: asyncio.Queue[Event]
    closed: asyncio.Event
    replay: list[Event]
    _close_fn: Callable[[], None] = field(default_factory=lambda: lambda: None)

    def close(self) -> None:
        self._close_fn()


class StreamHub(Protocol):
    async def publish(
        self, agent_id: UUID, event_type: str, data: dict[str, Any]
    ) -> Event: ...
    async def subscribe(
        self, agent_id: UUID, last_event_id: int | None
    ) -> Subscription: ...
    async def subscribe_admin(
        self, last_event_id: int | None
    ) -> Subscription: ...
    async def allocate_id(self, agent_id: UUID) -> int: ...
    async def allocate_admin_id(self) -> int: ...


class InMemoryStreamHub:
    """In-process StreamHub. Single-process MVP impl per §10.1.

    - Per-agent monotonic stream_event_id (int).
    - Per-agent bounded asyncio.Queue (drop subscription on full → reconnect heals).
    - Per-agent bounded deque replay buffer.
    - On reconnect with stale Last-Event-ID → emit `stream.gap` (consuming a fresh id).

    Admin firehose: a SHARED server-side ring buffer + monotonic counter, with
    multiple concurrent admin subscribers fanned out from one publish path.
    Per-connection rings would make Last-Event-ID resume always-gap on reconnect.
    """

    def __init__(self, *, replay_buffer_size: int = 1000, queue_size: int = 100) -> None:
        self._buffer_size = replay_buffer_size
        self._queue_size = queue_size
        self._counters: dict[UUID, int] = {}
        self._buffers: dict[UUID, deque[Event]] = {}
        self._subscribers: dict[UUID, tuple[asyncio.Queue[Event], asyncio.Event]] = {}
        # Admin firehose state. Sees every event published to any agent.
        self._admin_counter = 0
        self._admin_buffer: deque[Event] = deque(maxlen=replay_buffer_size)
        self._admin_subs: set[tuple[asyncio.Queue[Event], asyncio.Event]] = set()
        self._mu = asyncio.Lock()

    def _next_id(self, agent_id: UUID) -> int:
        n = self._counters.get(agent_id, 0) + 1
        self._counters[agent_id] = n
        return n

    def _next_admin_id(self) -> int:
        self._admin_counter += 1
        return self._admin_counter

    def _ensure_buffer(self, agent_id: UUID) -> deque[Event]:
        buf = self._buffers.get(agent_id)
        if buf is None:
            buf = deque(maxlen=self._buffer_size)
            self._buffers[agent_id] = buf
        return buf

    async def allocate_id(self, agent_id: UUID) -> int:
        """Fresh per-agent stream id with no buffering or fanout. Used for `ping`."""
        async with self._mu:
            return self._next_id(agent_id)

    async def allocate_admin_id(self) -> int:
        """Fresh admin firehose id with no buffering or fanout. Used for `ping`."""
        async with self._mu:
            return self._next_admin_id()

    async def publish(
        self, agent_id: UUID, event_type: str, data: dict[str, Any]
    ) -> Event:
        async with self._mu:
            ev = Event(
                stream_event_id=self._next_id(agent_id),
                event=event_type,
                data=data,
            )
            self._ensure_buffer(agent_id).append(ev)
            entry = self._subscribers.get(agent_id)
            if entry is not None:
                queue, closed = entry
                try:
                    queue.put_nowait(ev)
                except asyncio.QueueFull:
                    # Slow consumer: drop the subscription; reconnect heals
                    # via the replay buffer + Last-Event-ID.
                    self._subscribers.pop(agent_id, None)
                    closed.set()

            # Admin firehose fanout: separate id, same envelope.
            admin_ev = Event(
                stream_event_id=self._next_admin_id(),
                event=event_type,
                data=data,
            )
            self._admin_buffer.append(admin_ev)
            dead: list[tuple[asyncio.Queue[Event], asyncio.Event]] = []
            for sub_entry in self._admin_subs:
                q, c = sub_entry
                try:
                    q.put_nowait(admin_ev)
                except asyncio.QueueFull:
                    dead.append(sub_entry)
            for sub_entry in dead:
                self._admin_subs.discard(sub_entry)
                sub_entry[1].set()
        return ev

    async def subscribe(
        self, agent_id: UUID, last_event_id: int | None
    ) -> Subscription:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        closed = asyncio.Event()
        async with self._mu:
            buf = self._ensure_buffer(agent_id)
            current_max = self._counters.get(agent_id, 0)
            replay: list[Event] = []
            if last_event_id is not None and last_event_id < current_max:
                oldest_id = buf[0].stream_event_id if buf else None
                tail = [e for e in buf if e.stream_event_id > last_event_id]
                if oldest_id is None or last_event_id + 1 < oldest_id:
                    gap_from = last_event_id + 1
                    gap_to = (oldest_id - 1) if oldest_id is not None else current_max
                    gap = Event(
                        stream_event_id=self._next_id(agent_id),
                        event="stream.gap",
                        data={"from_id": gap_from, "to_id": gap_to},
                    )
                    replay = [gap, *tail]
                else:
                    replay = tail

            old = self._subscribers.get(agent_id)
            if old is not None:
                _, old_closed = old
                old_closed.set()
            self._subscribers[agent_id] = (queue, closed)

        return Subscription(
            queue=queue,
            closed=closed,
            replay=replay,
            _close_fn=lambda: self._unsubscribe(agent_id, queue),
        )

    async def subscribe_admin(self, last_event_id: int | None) -> Subscription:
        """Subscribe to the admin firehose. Multiple concurrent subscribers OK."""
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._queue_size)
        closed = asyncio.Event()
        async with self._mu:
            buf = self._admin_buffer
            current_max = self._admin_counter
            replay: list[Event] = []
            if last_event_id is not None and last_event_id < current_max:
                oldest_id = buf[0].stream_event_id if buf else None
                tail = [e for e in buf if e.stream_event_id > last_event_id]
                if oldest_id is None or last_event_id + 1 < oldest_id:
                    gap_from = last_event_id + 1
                    gap_to = (oldest_id - 1) if oldest_id is not None else current_max
                    gap = Event(
                        stream_event_id=self._next_admin_id(),
                        event="stream.gap",
                        data={"from_id": gap_from, "to_id": gap_to},
                    )
                    replay = [gap, *tail]
                else:
                    replay = tail

            entry = (queue, closed)
            self._admin_subs.add(entry)

        return Subscription(
            queue=queue,
            closed=closed,
            replay=replay,
            _close_fn=lambda: self._unsubscribe_admin((queue, closed)),
        )

    def _unsubscribe(self, agent_id: UUID, queue: asyncio.Queue[Event]) -> None:
        current = self._subscribers.get(agent_id)
        if current is not None and current[0] is queue:
            self._subscribers.pop(agent_id, None)
            current[1].set()

    def _unsubscribe_admin(
        self, entry: tuple[asyncio.Queue[Event], asyncio.Event]
    ) -> None:
        if entry in self._admin_subs:
            self._admin_subs.discard(entry)
            entry[1].set()

    # Test helpers ---------------------------------------------------------

    def has_subscriber(self, agent_id: UUID) -> bool:
        return agent_id in self._subscribers

    def buffer_snapshot(self, agent_id: UUID) -> list[Event]:
        return list(self._buffers.get(agent_id, ()))

    def admin_buffer_snapshot(self) -> list[Event]:
        return list(self._admin_buffer)
