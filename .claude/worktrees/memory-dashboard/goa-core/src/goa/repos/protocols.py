"""Persistence Protocols — the pluggable storage surface.

Four Protocols, each backed by an `InMemory*` default in
[goa.repos.memory](memory.py) and swappable at `create_app(...)` time:

- `ParticipantStore` — the participant registry. Plain CRUD + discovery.
- `TaskLog` — task headers, the append-only event log per task, and the
  `(initiator_id, external_ref)` uniqueness index. One Protocol because
  these three concepts must commit atomically (an event's effect on
  `pending_questions` materialization, an `external_ref` reservation, and
  the task's existence all need to be consistent on a single transaction
  boundary).
- `BlobStore` — attachment bytes referenced by `Content.attachments`.
- `MemoryStore` — agent-private, cross-task key→document memory.

Consumers can implement any of these against Postgres, SQLite, S3, etc.
Every method does one logical write (or one logical read); no consumer
ever has to reason about cross-store transactions.

Method names are globally unique across the Protocols on purpose: one
adapter object (e.g. `SqliteAdapter`) implements several of them at once,
so `BlobStore` uses `get_meta`/`get_task_id` (not `get`/`put`) and
`MemoryStore` suffixes everything `_memory` (`put_memory`, `get_memory`,
…) to avoid colliding with `ParticipantStore.get` / `BlobStore.put`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from goa.domain.models import Attachment, Event, MemoryEntry, Participant, Task


class ParticipantStore(Protocol):
    """The participant registry. Long-lived identity + discovery."""

    async def create(self, participant: Participant) -> Participant: ...
    async def get(self, participant_id: UUID) -> Participant | None: ...
    async def get_by_api_key_hash(self, api_key_hash: str) -> Participant | None: ...
    async def search(
        self,
        *,
        capabilities: list[str] | None = None,
        q: str | None = None,
        type: str | None = None,
    ) -> list[Participant]:
        """§11 discovery. `capabilities` is **AND-ed** (a result must carry
        every tag); `q` is case-insensitive substring on name+description;
        `type` filters by participant type. All filters are AND-ed together;
        absent/empty filters do not constrain."""
        ...

    async def delete(self, participant_id: UUID) -> None:
        """Remove a participant from the registry. Idempotent — deleting a
        non-existent id is a silent no-op."""
        ...

    async def update(self, participant: Participant) -> Participant:
        """Persist mutations to name, description, capabilities, and
        access_policy. `id` and `api_key_hash` are immutable and must not
        change. Returns the updated participant."""
        ...


class TaskLog(Protocol):
    """Task headers + per-task append-only event log + external_ref index.

    One Protocol because the three concepts commit atomically: an
    `external_ref` reservation happens in the same transaction as task
    creation; per-task event appends serialize under `lock(task_id)` so
    materialized projections (pending_questions etc.) stay consistent
    with the log.

    Persistent backends implement this against a single transactional
    store (Postgres, SQLite, etc.); each method maps to one row write
    or one indexed read. The single-replica in-memory default lives in
    [InMemoryTaskLog](memory.py).
    """

    async def create_task(self, task: Task, *, external_ref: str | None = None) -> Task:
        """Persist a new task. When `external_ref` is set, reserves the
        `(task.initiator_id, external_ref)` slot in the same atomic step —
        a collision raises `ExternalRefInUse` and no task is persisted.
        Returns the persisted task."""
        ...

    async def get_task(self, task_id: UUID) -> Task | None: ...

    async def get_task_by_external_ref(
        self, initiator_id: UUID, external_ref: str,
    ) -> UUID | None:
        """Resolve `(initiator_id, external_ref)` to a task id. Returns
        `None` if unmapped, **or if the matching task is closed.** The
        slot is released on close (§8) so `upsert_task` naturally creates
        a fresh task after the previous one closes — closed tasks remain
        readable via `get_task` for audit, they just no longer occupy
        the external_ref namespace."""
        ...

    async def list_tasks(
        self,
        *,
        parent_id: UUID | None = None,
        top_level_only: bool = True,
    ) -> list[Task]:
        """Admin-scoped listing — every task in the store, no participant
        gate. Sorted most-recently-active first.

        - `parent_id=<uuid>` returns children of that parent.
        - `top_level_only=True` (default) restricts to root tasks
          (`parent_task_id IS NULL`). Mutually exclusive with `parent_id`.

        Pending-questions filtering (`has_pending`) is **not** a Protocol
        concern — `pending_questions` is a derived view, so the service
        post-filters via `PendingProjection` after this returns.
        """
        ...

    async def list_tasks_for_participant(
        self,
        participant_id: UUID,
        *,
        role: str | None = None,
        parent_id: UUID | None = None,
        top_level_only: bool = True,
    ) -> list[Task]:
        """§9.2 listing. Returns tasks where `participant_id` is in
        `participants`, narrowed by the filters:
        - `role`: `"initiator"` → only tasks where `initiator_id == participant_id`;
          `"member"` → only tasks where the caller is a non-initiator participant;
          `None` → both.
        - `parent_id`/`top_level_only`: as for `list_tasks`.

        `has_pending` filtering is post-applied by the service via the
        pending projection — see `list_tasks` for the rationale."""
        ...

    async def list_children(self, parent_id: UUID) -> list[Task]:
        """Return every child of `parent_id`. No participant gate — the
        caller filters by membership."""
        ...

    async def add_participants(
        self, task_id: UUID, participant_ids: list[UUID],
    ) -> None:
        """Persist the addition of `participant_ids` to `task.participants`.
        Idempotent — re-adding an existing participant is silent.

        Backends that hydrate fresh `Task` instances per `get_task` (SQLite,
        Postgres) need this so service-layer auto-join growth survives the
        next fetch. In-memory backends may also defensively mutate their
        held `Task` to keep behavior uniform across replicas in tests.

        **Caller contract:** must be invoked inside `lock(task_id)`."""
        ...

    async def close_task(self, task_id: UUID) -> Task:
        """Transition `task.status` from `'open'` to `'closed'` and release
        the `(initiator_id, external_ref)` slot if any. Idempotent: closing
        an already-closed task is a no-op that returns the existing closed
        task.

        Persistent backends update the status row and drop the row from the
        external_ref unique index in one transaction — a half-applied close
        would leak the slot. The in-memory adapter mutates the held `Task`
        and deletes the index entry under the same per-task lock.

        Returns the updated `Task` (with `status='closed'` and `updated_at`
        bumped). Raises `TaskNotFound` if the task does not exist.

        **Caller contract:** must be invoked inside `lock(task_id)`.
        """
        ...

    async def touch_task(
        self, task_id: UUID, last_activity_at: datetime,
    ) -> None:
        """Persist `last_activity_at` (and equally `updated_at`) on the
        task. Called after every event append so list/search ordering
        stays current. Same reasoning as `add_participants`: in-memory
        may be a no-op when callers mutate the in-flight `Task`, but
        persistent backends must propagate to storage.

        **Caller contract:** must be invoked inside `lock(task_id)`."""
        ...

    async def append_event(self, event: Event) -> Event:
        """Persist `event` and return it. The implementation **assigns**
        `event.seq` (per-task monotonic, starting at 1) before storing;
        callers may pass any value (typically 0) and read the assigned
        value off the returned event. `UNIQUE(task_id, seq)` is enforced
        by persistent backends; the in-memory store relies on the per-task
        lock contract below.

        **Caller contract:** must be invoked inside `async with lock(task_id):`.
        The seq assignment is a read-then-write that the lock serializes."""
        ...

    async def get_event(self, event_id: UUID) -> Event | None: ...

    async def list_events_for_task(self, task_id: UUID) -> list[Event]: ...

    async def find_event_by_client_id(
        self,
        task_id: UUID,
        from_id: UUID,
        client_event_id: UUID,
    ) -> Event | None:
        """Return the previously-persisted event for this idempotency key
        (§13), or `None` if no such event exists.

        The dedup key is `(task_id, from_id, client_event_id)` — caller-scoped
        within a task. Two appends from the same caller against the same task
        with the same `client_event_id` resolve to a single persisted event;
        the second call short-circuits via this lookup before re-running the
        type-specific handler.

        **Caller contract:** must be invoked inside `lock(task_id)` so the
        check-and-insert is atomic.
        """
        ...

    def lock(self, task_id: UUID) -> AbstractAsyncContextManager[None]:
        """Per-task serialization for atomic event-append + pending-state
        update. In-memory uses `asyncio.Lock`; Postgres backends typically
        wrap a row-level `SELECT ... FOR UPDATE` on the task row, or use
        an advisory lock keyed on `task_id`."""
        ...


class BlobStore(Protocol):
    """Stores attachment bytes referenced by `Content.attachments`. Keeps
    bytes out of the event log so SSE replay stays small. Single canonical
    representation: events always carry `Attachment` metadata only.

    `put` consumes the upload as an async byte stream so large files do not
    have to be buffered whole; the implementation is responsible for hashing
    and enforcing the configured size limit.

    Each blob is bound to exactly one task at upload time. `task_id` is
    required on `put` and immutable thereafter; authz on download is "caller
    is a participant of the blob's bound task" — one column read, no link
    table. Cross-task references in events are forbidden (`BlobForbidden 403`).
    """

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
        """Persist the streamed bytes; return the resulting `Attachment`.
        Binds the blob to `task_id` immutably. Raises `BlobTooLarge` if the
        stream exceeds `max_bytes`."""
        ...

    async def get_meta(self, blob_id: UUID) -> Attachment | None: ...
    async def get_task_id(self, blob_id: UUID) -> UUID | None:
        """Return the task this blob is bound to, or `None` if no blob
        with this id exists. Replaces the pre-Stage-5 trio
        (`get_owner` / `link_task` / `list_linked_tasks`); authz is now
        a single read of this value."""
        ...
    async def open(self, blob_id: UUID) -> AsyncIterator[bytes]: ...


class MemoryStore(Protocol):
    """Agent-private, cross-task key→document memory.

    Owner-scoped: every entry belongs to exactly one participant
    (`owner_id`), and authz is a single-column check (`owner_id ==
    caller`). Unlike the task-bound `BlobStore`, memory deliberately
    spans tasks — but it never crosses the task-boundary seal (§7),
    because a participant only ever reads entries it wrote.

    Each method does one logical write or read; no cross-store
    transactions. Backends enforce `UNIQUE(owner_id, key)`.
    """

    async def put_memory(
        self, entry: MemoryEntry, *, max_entry_bytes: int, max_entries: int,
    ) -> tuple[MemoryEntry, bool]:
        """Upsert on `(owner_id, key)`. Overwrites `value`/`tags` and bumps
        `updated_at`, **preserving the original `created_at`**. Returns
        `(stored_entry, created)` where `created` is True only when the key
        did not previously exist (drives the 201-vs-200 status, mirroring
        `POST /tasks/upsert`).

        Raises `MemoryEntryTooLarge` when the JSON-encoded `value` exceeds
        `max_entry_bytes`, and `MemoryQuotaExceeded` when storing a **new**
        key would push the owner past `max_entries` (overwriting an existing
        key is always allowed). Both caps are passed in by the caller — the
        same pattern as `BlobStore.put(..., max_bytes=...)` — so the store
        owns no config."""
        ...

    async def get_memory(self, owner_id: UUID, key: str) -> MemoryEntry | None: ...

    async def list_memory(
        self,
        owner_id: UUID,
        *,
        key_prefix: str | None = None,
        tags: list[str] | None = None,
    ) -> list[MemoryEntry]:
        """Owner-scoped listing, ordered by `key`. `key_prefix` filters via
        an index-friendly range scan (`key >= prefix AND key < prefix_upper`),
        **not** SQL `LIKE` — so `_`/`%` in keys are literal, not wildcards.
        `tags` is AND-ed (an entry must carry every tag), mirroring
        `ParticipantStore.search(capabilities=...)`."""
        ...

    async def delete_memory(self, owner_id: UUID, key: str) -> int:
        """Delete one entry by exact key. Idempotent — returns 1 if a row
        was removed, 0 if the key was absent."""
        ...

    async def purge_memory(self, owner_id: UUID, *, key_prefix: str) -> int:
        """Forget-by-prefix (same range scan as `list_memory`). Returns the
        number of entries removed. `key_prefix` is required and non-empty —
        a full-owner wipe is intentionally not expressible here (deleting the
        participant is the account-wipe path)."""
        ...
