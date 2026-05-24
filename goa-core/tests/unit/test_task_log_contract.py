"""Protocol-level conformance suite for `TaskLog` impls.

Parametrized over `InMemoryTaskLog` today; consumer-shipped adapters
(Postgres, SQLite, etc.) run this same suite against their backend to
verify they honor the contract.

What every TaskLog impl must guarantee:

1. **Atomic external_ref reservation.** Two concurrent `create_task` calls
   with the same `(initiator_id, external_ref)` see exactly one success
   and one `ExternalRefInUse`. The store stays consistent — exactly one
   task lands, the loser's task is not persisted.

2. **`get_task_by_external_ref` reflects writes.** Immediately after
   `create_task(...)` returns, the ref is queryable.

3. **Per-task lock serializes appends.** Two concurrent `append_event`
   calls on the same task observe a total order; the lock prevents
   interleaving.

4. **Event log is task-scoped.** `list_events_for_task` returns only
   events whose `task_id` matches; events from other tasks don't leak.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from goa.domain.models import (
    Content,
    InfoEvent,
    InfoPayload,
    Task,
)
from goa.errors import ExternalRefInUse, TaskNotFound
from goa.repos.memory import InMemoryTaskLog
from goa.repos.protocols import TaskLog
from goa.repos.sqlite import SqliteAdapter

from tests.unit._postgres_factory import postgres_task_log_factory


pytestmark = pytest.mark.asyncio


# Each factory takes a tmp_path and returns an `AsyncContextManager[TaskLog]`.
# Persistent adapters use the tmp_path to scope state to the test; in-memory
# stores ignore it and wrap themselves in a no-op CM for fixture uniformity.
TaskLogFactory = Callable[[Path], AbstractAsyncContextManager[TaskLog]]


@asynccontextmanager
async def _wrap_noop(store: TaskLog) -> AsyncIterator[TaskLog]:
    yield store


def _in_memory_factory(_tmp: Path) -> AbstractAsyncContextManager[TaskLog]:
    return _wrap_noop(InMemoryTaskLog())


def _sqlite_factory(tmp: Path) -> AbstractAsyncContextManager[TaskLog]:
    return SqliteAdapter(tmp / "goa.db")


TASK_LOG_FACTORIES: list[TaskLogFactory] = [
    _in_memory_factory,
    _sqlite_factory,
    postgres_task_log_factory,
]


def _make_task(initiator_id: UUID | None = None, external_ref: str | None = None) -> Task:
    return Task(
        initiator_id=initiator_id or uuid4(),
        participants=[initiator_id or uuid4()],
        external_ref=external_ref,
    )


def _info_event(task_id: UUID, *, ord: int = 0) -> InfoEvent:
    return InfoEvent(
        task_id=task_id,
        from_=uuid4(),
        content=Content(text=f"#{ord}"),
        payload=InfoPayload(),
        created_at=datetime.now(tz=timezone.utc),
    )


@pytest_asyncio.fixture(params=TASK_LOG_FACTORIES, ids=lambda f: f.__name__)
async def task_log(request, tmp_path: Path) -> AsyncIterator[TaskLog]:
    async with request.param(tmp_path) as log:
        yield log


# ----------------------------------------------------------------------
# (1) atomic external_ref reservation
# ----------------------------------------------------------------------

async def test_create_task_reserves_external_ref_atomically(task_log: TaskLog) -> None:
    initiator = uuid4()
    t = _make_task(initiator, external_ref="ref-a")
    persisted = await task_log.create_task(t, external_ref="ref-a")
    assert persisted.id == t.id
    assert await task_log.get_task_by_external_ref(initiator, "ref-a") == t.id


async def test_create_task_collision_raises_external_ref_in_use(task_log: TaskLog) -> None:
    initiator = uuid4()
    first = _make_task(initiator, external_ref="ref-a")
    second = _make_task(initiator, external_ref="ref-a")

    await task_log.create_task(first, external_ref="ref-a")
    with pytest.raises(ExternalRefInUse):
        await task_log.create_task(second, external_ref="ref-a")

    # First task still wins; second task did not land. Compare by id —
    # persistent backends hydrate fresh `Task` instances rather than
    # returning the same Python object.
    got = await task_log.get_task(first.id)
    assert got is not None and got.id == first.id
    assert await task_log.get_task(second.id) is None


async def test_concurrent_create_task_same_ref_exactly_one_wins(task_log: TaskLog) -> None:
    """Two concurrent creates with the same `(initiator, ref)` race. The
    contract says exactly one wins; the other observes `ExternalRefInUse`.
    The store is consistent — `get_task_by_external_ref` resolves to the
    winner's task_id."""
    initiator = uuid4()
    a = _make_task(initiator, external_ref="ref-x")
    b = _make_task(initiator, external_ref="ref-x")

    results = await asyncio.gather(
        task_log.create_task(a, external_ref="ref-x"),
        task_log.create_task(b, external_ref="ref-x"),
        return_exceptions=True,
    )

    winners = [r for r in results if isinstance(r, Task)]
    losers = [r for r in results if isinstance(r, ExternalRefInUse)]
    assert len(winners) == 1
    assert len(losers) == 1

    winner = winners[0]
    assert await task_log.get_task_by_external_ref(initiator, "ref-x") == winner.id


async def test_external_ref_is_per_initiator(task_log: TaskLog) -> None:
    """§6.4: same ref string under different initiators is two slots."""
    a, b = uuid4(), uuid4()
    ta = _make_task(a, external_ref="shared")
    tb = _make_task(b, external_ref="shared")
    await task_log.create_task(ta, external_ref="shared")
    await task_log.create_task(tb, external_ref="shared")

    assert await task_log.get_task_by_external_ref(a, "shared") == ta.id
    assert await task_log.get_task_by_external_ref(b, "shared") == tb.id


async def test_create_task_without_external_ref_does_not_touch_index(task_log: TaskLog) -> None:
    initiator = uuid4()
    t = _make_task(initiator, external_ref=None)
    await task_log.create_task(t)
    got = await task_log.get_task(t.id)
    assert got is not None and got.id == t.id
    # Sanity: no leakage — a different ref lookup still returns None.
    assert await task_log.get_task_by_external_ref(initiator, "anything") is None


# ----------------------------------------------------------------------
# (3) per-task lock serializes appends
# ----------------------------------------------------------------------

async def test_lock_serializes_concurrent_appends(task_log: TaskLog) -> None:
    """Two coroutines both want the lock; the second must wait for the
    first. We assert by observing that both appends land and the log
    has exactly two entries — no lost write under contention."""
    t = _make_task()
    await task_log.create_task(t)

    async def append(seq: int) -> None:
        async with task_log.lock(t.id):
            ev = _info_event(t.id, ord=seq)
            await task_log.append_event(ev)

    await asyncio.gather(append(0), append(1))

    log = await task_log.list_events_for_task(t.id)
    assert len(log) == 2


# ----------------------------------------------------------------------
# (4) event log is task-scoped
# ----------------------------------------------------------------------

async def test_list_events_for_task_isolates_tasks(task_log: TaskLog) -> None:
    a = _make_task()
    b = _make_task()
    await task_log.create_task(a)
    await task_log.create_task(b)

    async with task_log.lock(a.id):
        await task_log.append_event(_info_event(a.id, ord=0))
    async with task_log.lock(b.id):
        await task_log.append_event(_info_event(b.id, ord=0))
    async with task_log.lock(a.id):
        await task_log.append_event(_info_event(a.id, ord=1))

    log_a = await task_log.list_events_for_task(a.id)
    log_b = await task_log.list_events_for_task(b.id)
    assert len(log_a) == 2
    assert len(log_b) == 1
    assert all(ev.task_id == a.id for ev in log_a)
    assert all(ev.task_id == b.id for ev in log_b)


async def test_get_event_returns_appended_event(task_log: TaskLog) -> None:
    t = _make_task()
    await task_log.create_task(t)
    ev = _info_event(t.id)
    async with task_log.lock(t.id):
        await task_log.append_event(ev)

    got = await task_log.get_event(ev.id)
    assert got is not None and got.id == ev.id


async def test_get_event_returns_none_for_unknown_id(task_log: TaskLog) -> None:
    assert await task_log.get_event(uuid4()) is None


# ----------------------------------------------------------------------
# (5) per-task monotonic seq
# ----------------------------------------------------------------------

async def test_append_event_assigns_monotonic_seq_per_task(task_log: TaskLog) -> None:
    """`append_event` is the assigner of record for `seq`; the per-task
    sequence starts at 1 and increments by 1 per append. Independent
    across tasks (each starts fresh at 1)."""
    a = _make_task()
    b = _make_task()
    await task_log.create_task(a)
    await task_log.create_task(b)

    async with task_log.lock(a.id):
        ev_a1 = await task_log.append_event(_info_event(a.id, ord=0))
        ev_a2 = await task_log.append_event(_info_event(a.id, ord=1))
    async with task_log.lock(b.id):
        ev_b1 = await task_log.append_event(_info_event(b.id, ord=0))

    assert ev_a1.seq == 1
    assert ev_a2.seq == 2
    assert ev_b1.seq == 1  # independent per task

    # Persisted state agrees with returned events.
    log_a = await task_log.list_events_for_task(a.id)
    assert [ev.seq for ev in log_a] == [1, 2]


async def test_concurrent_appends_get_distinct_seq_under_lock(task_log: TaskLog) -> None:
    """Two concurrent appends on the same task — taken inside `lock(task_id)`
    on both sides — land with distinct seq values. The lock contract is
    what makes the seq assignment race-free."""
    t = _make_task()
    await task_log.create_task(t)

    async def append() -> int:
        async with task_log.lock(t.id):
            ev = await task_log.append_event(_info_event(t.id))
            return ev.seq

    seqs = await asyncio.gather(append(), append(), append())
    assert sorted(seqs) == [1, 2, 3]


# ----------------------------------------------------------------------
# (6) idempotency index — `find_event_by_client_id`
# ----------------------------------------------------------------------

def _info_event_with_key(
    task_id: UUID,
    *,
    from_id: UUID,
    client_event_id: UUID | None,
    ord: int = 0,
) -> InfoEvent:
    return InfoEvent(
        task_id=task_id,
        from_=from_id,
        content=Content(text=f"#{ord}"),
        payload=InfoPayload(),
        client_event_id=client_event_id,
        created_at=datetime.now(tz=timezone.utc),
    )


async def test_find_event_by_client_id_returns_none_for_unknown_key(
    task_log: TaskLog,
) -> None:
    t = _make_task()
    await task_log.create_task(t)
    got = await task_log.find_event_by_client_id(t.id, uuid4(), uuid4())
    assert got is None


async def test_find_event_by_client_id_returns_appended_event(
    task_log: TaskLog,
) -> None:
    """Happy path: append with a key, then look it up. Returns the
    same persisted event (matching `id`, `seq`)."""
    t = _make_task()
    await task_log.create_task(t)
    caller = uuid4()
    key = uuid4()
    async with task_log.lock(t.id):
        appended = await task_log.append_event(
            _info_event_with_key(t.id, from_id=caller, client_event_id=key),
        )

    got = await task_log.find_event_by_client_id(t.id, caller, key)
    assert got is not None
    assert got.id == appended.id
    assert got.seq == appended.seq
    # client_event_id survives round-trip through hybrid SQLite storage
    # (it lives both as a column and inside the `body` JSON).
    assert got.client_event_id == key


async def test_find_event_by_client_id_no_key_event_not_indexed(
    task_log: TaskLog,
) -> None:
    """Events without a `client_event_id` are not findable via the
    idempotency index — they have no key. Back-compat for clients that
    opt out of idempotency."""
    t = _make_task()
    await task_log.create_task(t)
    caller = uuid4()
    async with task_log.lock(t.id):
        await task_log.append_event(
            _info_event_with_key(t.id, from_id=caller, client_event_id=None),
        )

    # Any lookup is None — no key was ever associated.
    assert await task_log.find_event_by_client_id(t.id, caller, uuid4()) is None


async def test_find_event_by_client_id_is_caller_scoped(task_log: TaskLog) -> None:
    """Same key from two different callers in the same task → two distinct
    events. Lookup is namespaced by `from_id`."""
    t = _make_task()
    await task_log.create_task(t)
    alice, bob = uuid4(), uuid4()
    key = uuid4()
    async with task_log.lock(t.id):
        ev_alice = await task_log.append_event(
            _info_event_with_key(t.id, from_id=alice, client_event_id=key, ord=0),
        )
        ev_bob = await task_log.append_event(
            _info_event_with_key(t.id, from_id=bob, client_event_id=key, ord=1),
        )

    assert ev_alice.id != ev_bob.id
    got_alice = await task_log.find_event_by_client_id(t.id, alice, key)
    got_bob = await task_log.find_event_by_client_id(t.id, bob, key)
    assert got_alice is not None and got_alice.id == ev_alice.id
    assert got_bob is not None and got_bob.id == ev_bob.id


async def test_find_event_by_client_id_is_task_scoped(task_log: TaskLog) -> None:
    """Same key from the same caller in two different tasks → two distinct
    events. Lookup is namespaced by `task_id`."""
    a, b = _make_task(), _make_task()
    await task_log.create_task(a)
    await task_log.create_task(b)
    caller = uuid4()
    key = uuid4()
    async with task_log.lock(a.id):
        ev_a = await task_log.append_event(
            _info_event_with_key(a.id, from_id=caller, client_event_id=key, ord=0),
        )
    async with task_log.lock(b.id):
        ev_b = await task_log.append_event(
            _info_event_with_key(b.id, from_id=caller, client_event_id=key, ord=1),
        )

    assert ev_a.id != ev_b.id
    got_a = await task_log.find_event_by_client_id(a.id, caller, key)
    got_b = await task_log.find_event_by_client_id(b.id, caller, key)
    assert got_a is not None and got_a.id == ev_a.id
    assert got_b is not None and got_b.id == ev_b.id


# ----------------------------------------------------------------------
# (7) explicit task close — `close_task`
# ----------------------------------------------------------------------

async def test_close_task_flips_status(task_log: TaskLog) -> None:
    t = _make_task()
    await task_log.create_task(t)
    async with task_log.lock(t.id):
        closed = await task_log.close_task(t.id)
    assert closed.status == "closed"
    # Persisted state agrees with the returned task.
    got = await task_log.get_task(t.id)
    assert got is not None and got.status == "closed"


async def test_close_task_is_idempotent(task_log: TaskLog) -> None:
    t = _make_task()
    await task_log.create_task(t)
    async with task_log.lock(t.id):
        first = await task_log.close_task(t.id)
        second = await task_log.close_task(t.id)
    assert first.id == second.id
    # `updated_at` from the second call equals the first — second close
    # is a no-op flip, not a re-flip.
    assert first.updated_at == second.updated_at


async def test_close_task_missing_raises_task_not_found(task_log: TaskLog) -> None:
    with pytest.raises(TaskNotFound):
        async with task_log.lock(uuid4()):
            await task_log.close_task(uuid4())


async def test_close_task_releases_external_ref_slot(task_log: TaskLog) -> None:
    """After close, `get_task_by_external_ref` returns None for the same
    `(initiator, ref)` and a new `create_task` with the same ref succeeds."""
    initiator = uuid4()
    first = _make_task(initiator, external_ref="thread-x")
    await task_log.create_task(first, external_ref="thread-x")
    assert await task_log.get_task_by_external_ref(initiator, "thread-x") == first.id

    async with task_log.lock(first.id):
        await task_log.close_task(first.id)

    # Slot is now free.
    assert await task_log.get_task_by_external_ref(initiator, "thread-x") is None

    # And a new task with the same ref can be created.
    second = _make_task(initiator, external_ref="thread-x")
    await task_log.create_task(second, external_ref="thread-x")
    assert second.id != first.id
    assert await task_log.get_task_by_external_ref(initiator, "thread-x") == second.id


async def test_get_task_by_external_ref_excludes_closed(task_log: TaskLog) -> None:
    """Explicit check that closed tasks are invisible to the upsert
    lookup, even before re-create."""
    initiator = uuid4()
    t = _make_task(initiator, external_ref="ref-q")
    await task_log.create_task(t, external_ref="ref-q")
    async with task_log.lock(t.id):
        await task_log.close_task(t.id)
    assert await task_log.get_task_by_external_ref(initiator, "ref-q") is None
    # The task itself is still readable.
    got = await task_log.get_task(t.id)
    assert got is not None
    assert got.status == "closed"
    assert got.external_ref == "ref-q"
