"""`create_app(...)` honors a caller-supplied `Persistence` bundle.
Proves the ADK-style extension point works end-to-end: a consumer plugs
in their own `TaskLog` / `ParticipantStore` / `BlobStore` impl via the
bundle and the hub uses those instead of the in-memory defaults."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from uuid import UUID

import pytest

from goa import (
    BlobStore,
    InMemoryBlobStore,
    InMemoryParticipantStore,
    InMemoryTaskLog,
    ParticipantStore,
    Persistence,
    Settings,
    TaskLog,
    create_app,
)
from goa.domain.models import Attachment, Event, Participant, Task


pytestmark = pytest.mark.asyncio


class _RecordingParticipantStore(InMemoryParticipantStore):
    """Records every call so the test can assert the custom impl was used,
    not the default."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def create(self, participant: Participant) -> Participant:
        self.calls.append("create")
        return await super().create(participant)

    async def get_by_api_key_hash(self, api_key_hash: str) -> Participant | None:
        self.calls.append("get_by_api_key_hash")
        return await super().get_by_api_key_hash(api_key_hash)


class _RecordingTaskLog(InMemoryTaskLog):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def create_task(self, task: Task, *, external_ref: str | None = None) -> Task:
        self.calls.append("create_task")
        return await super().create_task(task, external_ref=external_ref)


class _RecordingBlobStore(InMemoryBlobStore):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def put(
        self,
        *,
        task_id: UUID,
        owner_id: UUID,
        filename: str,
        mime_type: str,
        stream: AsyncIterator[bytes],
        max_bytes: int,
    ) -> Attachment:
        self.calls.append("put")
        return await super().put(
            task_id=task_id,
            owner_id=owner_id,
            filename=filename,
            mime_type=mime_type,
            stream=stream,
            max_bytes=max_bytes,
        )


async def test_create_app_with_defaults_returns_working_app() -> None:
    app = create_app(Settings.for_tests())
    ctx = app.state.ctx
    # The default ctx wires the in-memory impls.
    assert isinstance(ctx.task_log, InMemoryTaskLog)
    assert isinstance(ctx.participant_store, InMemoryParticipantStore)
    assert isinstance(ctx.blob_store, InMemoryBlobStore)


async def test_create_app_uses_injected_task_log() -> None:
    custom = _RecordingTaskLog()
    p = replace(Persistence.in_memory(), task_log=custom)
    app = create_app(Settings.for_tests(), persistence=p)
    assert app.state.ctx.task_log is custom


async def test_create_app_uses_injected_participant_store() -> None:
    custom = _RecordingParticipantStore()
    p = replace(Persistence.in_memory(), participant_store=custom)
    app = create_app(Settings.for_tests(), persistence=p)
    assert app.state.ctx.participant_store is custom


async def test_create_app_uses_injected_blob_store() -> None:
    custom = _RecordingBlobStore()
    p = replace(Persistence.in_memory(), blob_store=custom)
    app = create_app(Settings.for_tests(), persistence=p)
    assert app.state.ctx.blob_store is custom


async def test_create_app_threads_injected_stores_into_task_service() -> None:
    """End-to-end: the TaskService composed inside create_app uses the
    injected stores. Drive one operation per store and assert the custom
    impl saw the call."""
    pstore = _RecordingParticipantStore()
    tlog = _RecordingTaskLog()
    p = Persistence(
        task_log=tlog,
        participant_store=pstore,
        blob_store=InMemoryBlobStore(),
    )
    app = create_app(Settings.for_tests(), persistence=p)

    # Touch the participant_store via TaskService dependency
    alice = Participant(type="agent", name="alice", api_key_hash="h-alice")
    await app.state.ctx.participant_store.create(alice)
    assert "create" in pstore.calls

    # Touch the task_log via TaskService.create_task — the service is the
    # one composed by build_context, so this proves wiring.
    from goa.domain.models import CreateTaskBody

    bob = Participant(type="agent", name="bob", api_key_hash="h-bob")
    await app.state.ctx.participant_store.create(bob)
    await app.state.ctx.service.create_task(alice, CreateTaskBody())
    assert "create_task" in tlog.calls


async def test_partial_override_via_replace_keeps_other_defaults() -> None:
    """`dataclasses.replace(Persistence.in_memory(), task_log=…)` is the
    canonical pattern for swapping one Protocol in tests while letting the
    other two ride the in-memory defaults."""
    tlog = _RecordingTaskLog()
    p = replace(Persistence.in_memory(), task_log=tlog)
    app = create_app(Settings.for_tests(), persistence=p)
    ctx = app.state.ctx
    assert ctx.task_log is tlog
    assert isinstance(ctx.participant_store, InMemoryParticipantStore)
    assert isinstance(ctx.blob_store, InMemoryBlobStore)


async def test_replace_yields_fresh_entered_list() -> None:
    """`_entered` is `init=False`, so `dataclasses.replace(...)` gives the
    replaced bundle a fresh tracking list — replaced bundles do not share
    the original's enter/exit state."""
    original = Persistence.in_memory()
    # Mutate the original's tracker as if it were entered.
    original._entered.append(InMemoryTaskLog())  # type: ignore[arg-type]

    replaced = replace(original, task_log=_RecordingTaskLog())
    assert replaced._entered == []
    assert replaced._entered is not original._entered


# ----------------------------------------------------------------------
# Persistence.from_settings — scheme dispatch
# ----------------------------------------------------------------------

async def test_from_settings_unset_returns_in_memory() -> None:
    s = Settings.for_tests()
    assert s.database_url is None
    p = Persistence.from_settings(s)
    assert isinstance(p.task_log, InMemoryTaskLog)


async def test_from_settings_unknown_scheme_raises_value_error() -> None:
    """Misconfiguration fails at startup, not on first request."""
    s = replace(Settings.for_tests(), database_url="redis://nope")
    with pytest.raises(ValueError, match="Unsupported GOA_DATABASE_URL"):
        Persistence.from_settings(s)


async def test_from_settings_postgres_requires_s3_blob_backend() -> None:
    """Postgres holds no blob bytes; `blob_backend` must be `"s3"`."""
    s = replace(Settings.for_tests(), database_url="postgresql://nope")
    with pytest.raises(ValueError, match="GOA_BLOB_BACKEND=s3"):
        Persistence.from_settings(s)


async def test_from_settings_sqlite_scheme_returns_sqlite_bundle(tmp_path) -> None:
    from goa.repos.sqlite import SqliteAdapter

    s = replace(Settings.for_tests(), database_url=f"sqlite:{tmp_path / 'goa.db'}")
    p = Persistence.from_settings(s)
    # All three slots hold the same adapter — `Persistence.__aenter__` dedupes
    # so the connection is opened exactly once.
    assert isinstance(p.task_log, SqliteAdapter)
    assert p.task_log is p.participant_store is p.blob_store


# ----------------------------------------------------------------------
# Type-shape sanity: the published Protocols are usable from outside goa
# without importing internal modules.
# ----------------------------------------------------------------------

async def test_protocols_are_importable_from_top_level() -> None:
    """A consumer should be able to `from goa import TaskLog, ParticipantStore,
    BlobStore` and write an adapter against just those names. This test asserts
    those names are public and that the in-memory defaults satisfy them
    structurally."""
    # Static checks at runtime — Protocols use duck typing, so we just verify
    # the in-memory impls have the methods callers expect.
    log: TaskLog = InMemoryTaskLog()
    ps: ParticipantStore = InMemoryParticipantStore()
    bs: BlobStore = InMemoryBlobStore()

    assert callable(log.create_task)
    assert callable(log.get_task)
    assert callable(log.get_task_by_external_ref)
    assert callable(log.append_event)
    assert callable(log.list_events_for_task)
    assert callable(log.lock)

    assert callable(ps.create)
    assert callable(ps.search)

    assert callable(bs.put)
    assert callable(bs.open)
