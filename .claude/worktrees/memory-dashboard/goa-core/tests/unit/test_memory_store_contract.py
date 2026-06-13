"""Protocol-level conformance suite for `MemoryStore` impls.

Parametrized across the in-memory, SQLite, and Postgres backends — the same
assertions run against every backend so they stay behaviorally identical.

Invariants covered:

1. `put_memory` + `get_memory` round-trips value / tags / owner identically.
2. Owner isolation — one participant never sees another's entries.
3. Overwrite preserves `created_at`, advances `updated_at`, and reports
   `created == False`.
4. Prefix lookup is an **exact** range scan, not a LIKE pattern — a prefix
   ending in `_` must not over-match (`user_` ≠ `userX...`).
5. Tags are AND-ed.
6. `delete_memory` (by key) and `purge_memory` (by prefix) are idempotent
   and return accurate counts.
7. Per-entry size cap and per-owner count cap are enforced inside `put_memory`
   (overwriting an existing key is always allowed, even at the count cap).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from goa.domain.models import MemoryEntry
from goa.errors import MemoryEntryTooLarge, MemoryQuotaExceeded
from goa.repos.memory import InMemoryMemoryStore
from goa.repos.protocols import MemoryStore
from goa.repos.sqlite import SqliteAdapter

from tests.unit._postgres_factory import postgres_memory_store_factory


pytestmark = pytest.mark.asyncio


MemoryStoreFactory = Callable[[Path], AbstractAsyncContextManager[MemoryStore]]

# Generous caps so the non-cap tests never trip them; cap tests pass small
# values explicitly.
BIG = 1_000_000
MANY = 1_000


@asynccontextmanager
async def _wrap_noop(store: MemoryStore) -> AsyncIterator[MemoryStore]:
    yield store


def _in_memory_factory(_tmp: Path) -> AbstractAsyncContextManager[MemoryStore]:
    return _wrap_noop(InMemoryMemoryStore())


def _sqlite_factory(tmp: Path) -> AbstractAsyncContextManager[MemoryStore]:
    return SqliteAdapter(tmp / "goa.db")


MEMORY_STORE_FACTORIES: list[MemoryStoreFactory] = [
    _in_memory_factory,
    _sqlite_factory,
    postgres_memory_store_factory,
]


@pytest_asyncio.fixture(params=MEMORY_STORE_FACTORIES, ids=lambda f: f.__name__)
async def store(request, tmp_path: Path) -> AsyncIterator[MemoryStore]:
    async with request.param(tmp_path) as s:
        yield s


def _entry(owner: UUID, key: str, value=None, tags: list[str] | None = None) -> MemoryEntry:
    return MemoryEntry(owner_id=owner, key=key, value=value, tags=tags or [])


async def _put(store: MemoryStore, entry: MemoryEntry, **caps):
    return await store.put_memory(
        entry,
        max_entry_bytes=caps.get("max_entry_bytes", BIG),
        max_entries=caps.get("max_entries", MANY),
    )


# ----------------------------------------------------------------------
# round-trip + get
# ----------------------------------------------------------------------

async def test_put_then_get_round_trips(store: MemoryStore) -> None:
    owner = uuid4()
    stored, created = await _put(store, _entry(owner, "prefers", {"contact": "email"}, ["pref"]))
    assert created is True
    got = await store.get_memory(owner, "prefers")
    assert got is not None
    assert got.owner_id == owner
    assert got.key == "prefers"
    assert got.value == {"contact": "email"}
    assert got.tags == ["pref"]


async def test_get_unknown_returns_none(store: MemoryStore) -> None:
    assert await store.get_memory(uuid4(), "nope") is None


async def test_scalar_and_null_values_round_trip(store: MemoryStore) -> None:
    owner = uuid4()
    await _put(store, _entry(owner, "s", "hello"))
    await _put(store, _entry(owner, "n", None))
    await _put(store, _entry(owner, "i", 42))
    assert (await store.get_memory(owner, "s")).value == "hello"
    assert (await store.get_memory(owner, "n")).value is None
    assert (await store.get_memory(owner, "i")).value == 42


# ----------------------------------------------------------------------
# owner isolation
# ----------------------------------------------------------------------

async def test_owner_isolation(store: MemoryStore) -> None:
    a, b = uuid4(), uuid4()
    await _put(store, _entry(a, "k", "a-secret"))
    assert await store.get_memory(b, "k") is None
    assert await store.list_memory(b) == []
    assert (await store.get_memory(a, "k")).value == "a-secret"


# ----------------------------------------------------------------------
# overwrite semantics
# ----------------------------------------------------------------------

async def test_overwrite_preserves_id_and_created_at_and_reports_not_created(store: MemoryStore) -> None:
    owner = uuid4()
    first, created1 = await _put(store, _entry(owner, "k", 1))
    assert created1 is True
    second, created2 = await _put(store, _entry(owner, "k", 2, ["t"]))
    assert created2 is False
    # id and created_at are stable across overwrite; the returned entry must
    # match what is actually persisted (a subsequent read).
    assert second.id == first.id
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at
    got = await store.get_memory(owner, "k")
    assert got.id == first.id
    assert got.value == 2
    assert got.tags == ["t"]
    assert got.created_at == first.created_at


# ----------------------------------------------------------------------
# prefix range scan — exact, not LIKE
# ----------------------------------------------------------------------

async def test_prefix_scan_is_exact_not_like(store: MemoryStore) -> None:
    """A prefix ending in `_` must not behave like SQL `LIKE`, where `_`
    matches any single character. This is the forget-path correctness guard."""
    owner = uuid4()
    await _put(store, _entry(owner, "user_1:a", 1))
    await _put(store, _entry(owner, "userX1:a", 2))  # the LIKE over-match trap
    await _put(store, _entry(owner, "user_2:b", 3))
    got = await store.list_memory(owner, key_prefix="user_")
    assert sorted(e.key for e in got) == ["user_1:a", "user_2:b"]


async def test_list_orders_by_key_and_no_filter_returns_all(store: MemoryStore) -> None:
    owner = uuid4()
    for k in ("c", "a", "b"):
        await _put(store, _entry(owner, k, 1))
    got = await store.list_memory(owner)
    assert [e.key for e in got] == ["a", "b", "c"]


# ----------------------------------------------------------------------
# tags AND-ed
# ----------------------------------------------------------------------

async def test_tags_anded(store: MemoryStore) -> None:
    owner = uuid4()
    await _put(store, _entry(owner, "k1", 1, ["a", "b"]))
    await _put(store, _entry(owner, "k2", 2, ["a"]))
    got = await store.list_memory(owner, tags=["a", "b"])
    assert [e.key for e in got] == ["k1"]


# ----------------------------------------------------------------------
# delete + purge
# ----------------------------------------------------------------------

async def test_delete_by_key_is_idempotent(store: MemoryStore) -> None:
    owner = uuid4()
    await _put(store, _entry(owner, "k", 1))
    assert await store.delete_memory(owner, "k") == 1
    assert await store.delete_memory(owner, "k") == 0
    assert await store.get_memory(owner, "k") is None


async def test_purge_by_prefix_returns_count(store: MemoryStore) -> None:
    owner = uuid4()
    for k in ("u:1:a", "u:1:b", "u:2:a", "other"):
        await _put(store, _entry(owner, k, 1))
    assert await store.purge_memory(owner, key_prefix="u:1:") == 2
    remaining = sorted(e.key for e in await store.list_memory(owner))
    assert remaining == ["other", "u:2:a"]
    # idempotent — purging an empty prefix range removes nothing.
    assert await store.purge_memory(owner, key_prefix="u:1:") == 0


# ----------------------------------------------------------------------
# caps
# ----------------------------------------------------------------------

async def test_entry_too_large_raises_and_persists_nothing(store: MemoryStore) -> None:
    owner = uuid4()
    with pytest.raises(MemoryEntryTooLarge):
        await _put(store, _entry(owner, "k", "x" * 200), max_entry_bytes=50)
    assert await store.get_memory(owner, "k") is None


async def test_quota_blocks_new_key_but_allows_overwrite(store: MemoryStore) -> None:
    owner = uuid4()
    await _put(store, _entry(owner, "k1", 1), max_entries=1)
    with pytest.raises(MemoryQuotaExceeded):
        await _put(store, _entry(owner, "k2", 2), max_entries=1)
    # Overwriting an existing key is allowed even at the cap.
    _stored, created = await _put(store, _entry(owner, "k1", 99), max_entries=1)
    assert created is False
    assert (await store.get_memory(owner, "k1")).value == 99
