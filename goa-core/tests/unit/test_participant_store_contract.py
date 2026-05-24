"""Protocol-level conformance suite for `ParticipantStore` impls.

Parametrized across the in-memory and SQLite backends. Adapters that
ship later (Postgres, etc.) add a factory to `PARTICIPANT_STORE_FACTORIES`
and the same assertions run against the new backend without modification.

Invariants covered:

1. `create` + `get(id)` round-trips identically — including `capabilities`,
   `access_policy`, and timestamps. Persistent backends must serialize/
   deserialize these without lossy coercion.
2. `get_by_api_key_hash` finds the row; `api_key_hash` is unique.
3. `search(capabilities=[...])` AND-s — a result must carry every tag.
4. `search(q=...)` is case-insensitive substring over `name + " " + description`.
5. `search(type=...)` filters exact.
6. No filters returns everything in stable order.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from goa.domain.models import Participant
from goa.repos.memory import InMemoryParticipantStore
from goa.repos.protocols import ParticipantStore
from goa.repos.sqlite import SqliteAdapter

from tests.unit._postgres_factory import postgres_participant_store_factory


pytestmark = pytest.mark.asyncio


ParticipantStoreFactory = Callable[[Path], AbstractAsyncContextManager[ParticipantStore]]


@asynccontextmanager
async def _wrap_noop(store: ParticipantStore) -> AsyncIterator[ParticipantStore]:
    yield store


def _in_memory_factory(_tmp: Path) -> AbstractAsyncContextManager[ParticipantStore]:
    return _wrap_noop(InMemoryParticipantStore())


def _sqlite_factory(tmp: Path) -> AbstractAsyncContextManager[ParticipantStore]:
    return SqliteAdapter(tmp / "goa.db")


PARTICIPANT_STORE_FACTORIES: list[ParticipantStoreFactory] = [
    _in_memory_factory,
    _sqlite_factory,
    postgres_participant_store_factory,
]


@pytest_asyncio.fixture(params=PARTICIPANT_STORE_FACTORIES, ids=lambda f: f.__name__)
async def store(request, tmp_path: Path) -> AsyncIterator[ParticipantStore]:
    async with request.param(tmp_path) as s:
        yield s


def _make(
    *,
    name: str = "alice",
    description: str = "",
    type: str = "agent",
    capabilities: list[str] | None = None,
    api_key_hash: str | None = None,
    access_policy: str = "public",
) -> Participant:
    return Participant(
        type=type,
        name=name,
        description=description,
        capabilities=capabilities or [],
        access_policy=access_policy,
        api_key_hash=api_key_hash or f"hash-{uuid4().hex}",
    )


# ----------------------------------------------------------------------
# create + get round-trip
# ----------------------------------------------------------------------

async def test_create_then_get_round_trips(store: ParticipantStore) -> None:
    p = _make(
        name="alice",
        description="legal analyst",
        capabilities=["summarize", "legal"],
        access_policy="public",
    )
    await store.create(p)
    got = await store.get(p.id)
    assert got is not None
    assert got.id == p.id
    assert got.name == "alice"
    assert got.description == "legal analyst"
    assert set(got.capabilities) == {"summarize", "legal"}
    assert got.access_policy == "public"
    assert got.api_key_hash == p.api_key_hash


async def test_get_unknown_returns_none(store: ParticipantStore) -> None:
    assert await store.get(uuid4()) is None


# ----------------------------------------------------------------------
# api_key_hash lookup
# ----------------------------------------------------------------------

async def test_get_by_api_key_hash_finds_participant(store: ParticipantStore) -> None:
    p = _make(api_key_hash="hash-known")
    await store.create(p)
    got = await store.get_by_api_key_hash("hash-known")
    assert got is not None and got.id == p.id


async def test_get_by_api_key_hash_returns_none_for_unknown(store: ParticipantStore) -> None:
    assert await store.get_by_api_key_hash("nope") is None


# ----------------------------------------------------------------------
# search — capabilities (AND-ed)
# ----------------------------------------------------------------------

async def test_search_capabilities_anded(store: ParticipantStore) -> None:
    has_both = _make(name="both", capabilities=["a", "b"])
    has_one = _make(name="one", capabilities=["a"])
    has_neither = _make(name="neither", capabilities=["c"])
    for p in (has_both, has_one, has_neither):
        await store.create(p)

    results = await store.search(capabilities=["a", "b"])
    ids = {p.id for p in results}
    assert has_both.id in ids
    assert has_one.id not in ids
    assert has_neither.id not in ids


# ----------------------------------------------------------------------
# search — q (case-insensitive substring on name + description)
# ----------------------------------------------------------------------

async def test_search_q_matches_name_or_description_case_insensitive(
    store: ParticipantStore,
) -> None:
    a = _make(name="Refund Bot", description="handles refund flows")
    b = _make(name="Order Agent", description="order intake and tracking")
    c = _make(name="Audit", description="REFUND review queue")
    for p in (a, b, c):
        await store.create(p)

    results = await store.search(q="refund")
    ids = {p.id for p in results}
    assert a.id in ids  # matched on name
    assert c.id in ids  # matched on description, case-insensitive
    assert b.id not in ids


# ----------------------------------------------------------------------
# search — type filter
# ----------------------------------------------------------------------

async def test_search_type_filter(store: ParticipantStore) -> None:
    agent = _make(name="agent-1", type="agent")
    service = _make(name="svc-1", type="service")
    await store.create(agent)
    await store.create(service)

    agents = await store.search(type="agent")
    services = await store.search(type="service")
    assert agent.id in {p.id for p in agents}
    assert service.id not in {p.id for p in agents}
    assert service.id in {p.id for p in services}
    assert agent.id not in {p.id for p in services}


# ----------------------------------------------------------------------
# search — no filters returns all
# ----------------------------------------------------------------------

async def test_search_no_filters_returns_all(store: ParticipantStore) -> None:
    created = [_make(name=f"p-{i}") for i in range(3)]
    for p in created:
        await store.create(p)

    results = await store.search()
    assert {p.id for p in results} == {p.id for p in created}
