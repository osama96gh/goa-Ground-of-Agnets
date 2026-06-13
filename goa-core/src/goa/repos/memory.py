"""In-memory implementations of the persistence Protocols.

These are the zero-config defaults wired by `create_app()` when no
custom store is supplied. They are also what `make goa` and the tests
run against. Single-replica only — persistent backends (Postgres,
SQLite, S3) implement the same Protocols.

Internal locks: `_extref_lock` serializes external_ref reservation,
per-task `asyncio.Lock` serializes event appends.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from uuid import UUID, uuid4

from goa.domain.models import Attachment, Event, MemoryEntry, Participant, Task
from goa.errors import (
    BlobTooLarge,
    ExternalRefInUse,
    MemoryEntryTooLarge,
    MemoryQuotaExceeded,
    TaskNotFound,
)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class InMemoryParticipantStore:
    def __init__(self) -> None:
        self._by_id: dict[UUID, Participant] = {}
        self._by_hash: dict[str, Participant] = {}

    async def create(self, participant: Participant) -> Participant:
        self._by_id[participant.id] = participant
        self._by_hash[participant.api_key_hash] = participant
        return participant

    async def get(self, participant_id: UUID) -> Participant | None:
        return self._by_id.get(participant_id)

    async def get_by_api_key_hash(self, api_key_hash: str) -> Participant | None:
        return self._by_hash.get(api_key_hash)

    async def search(
        self,
        *,
        capabilities: list[str] | None = None,
        q: str | None = None,
        type: str | None = None,
    ) -> list[Participant]:
        # §11: `capability` AND-ed; `q` LIKE on name+description; `type` exact.
        wanted_caps = set(capabilities or ())
        needle = q.lower() if q else None
        results: list[Participant] = []
        for p in self._by_id.values():
            if type is not None and p.type != type:
                continue
            if wanted_caps and not wanted_caps.issubset(p.capabilities):
                continue
            if needle is not None:
                hay = (p.name + " " + p.description).lower()
                if needle not in hay:
                    continue
            results.append(p)
        # Stable order for tests: by created_at then id.
        results.sort(key=lambda p: (p.created_at, str(p.id)))
        return results

    async def delete(self, participant_id: UUID) -> None:
        participant = self._by_id.pop(participant_id, None)
        if participant is not None:
            self._by_hash.pop(participant.api_key_hash, None)

    async def update(self, participant: Participant) -> Participant:
        self._by_id[participant.id] = participant
        self._by_hash[participant.api_key_hash] = participant
        return participant


class InMemoryTaskLog:
    """Single-replica `TaskLog`. Owns task headers, the per-task event
    log, the `(initiator_id, external_ref)` uniqueness index, and the
    per-task locks. All state is in-process dicts — restart wipes
    everything. Persistent backends swap in a real store.
    """

    def __init__(self) -> None:
        self._tasks: dict[UUID, Task] = {}
        self._events_by_id: dict[UUID, Event] = {}
        self._events_by_task: dict[UUID, list[Event]] = {}
        self._external_refs: dict[tuple[UUID, str], UUID] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}
        # Idempotency index: (task_id, from_id, client_event_id) → event_id.
        # Populated by `append_event` whenever the event carries a non-null
        # `client_event_id`. Read by `find_event_by_client_id` under the
        # per-task lock to short-circuit retries.
        self._client_event_index: dict[tuple[UUID, UUID, UUID], UUID] = {}
        # Serializes external_ref reservation + task creation as one atomic
        # step. Held only for the duration of `create_task`; never held
        # across `await`s that touch other state.
        self._extref_lock = asyncio.Lock()

    # ---- tasks ----

    async def create_task(self, task: Task, *, external_ref: str | None = None) -> Task:
        # Reserve external_ref + persist task atomically. Two concurrent
        # creates with the same `(initiator_id, external_ref)` see exactly
        # one success and one `ExternalRefInUse`.
        async with self._extref_lock:
            if external_ref is not None:
                key = (task.initiator_id, external_ref)
                if key in self._external_refs:
                    raise ExternalRefInUse()
                self._external_refs[key] = task.id
            self._tasks[task.id] = task
            self._locks[task.id] = asyncio.Lock()
        return task

    async def get_task(self, task_id: UUID) -> Task | None:
        return self._tasks.get(task_id)

    async def get_task_by_external_ref(
        self, initiator_id: UUID, external_ref: str,
    ) -> UUID | None:
        return self._external_refs.get((initiator_id, external_ref))

    async def list_tasks(
        self,
        *,
        parent_id: UUID | None = None,
        top_level_only: bool = True,
    ) -> list[Task]:
        results: list[Task] = []
        for t in self._tasks.values():
            if parent_id is not None:
                if t.parent_task_id != parent_id:
                    continue
            elif top_level_only and t.parent_task_id is not None:
                continue
            results.append(t)
        results.sort(key=lambda t: t.last_activity_at, reverse=True)
        return results

    async def list_tasks_for_participant(
        self,
        participant_id: UUID,
        *,
        role: str | None = None,
        parent_id: UUID | None = None,
        top_level_only: bool = True,
    ) -> list[Task]:
        results: list[Task] = []
        for t in self._tasks.values():
            if participant_id not in t.participants:
                continue
            if role == "initiator" and t.initiator_id != participant_id:
                continue
            if role == "member" and t.initiator_id == participant_id:
                continue
            if parent_id is not None:
                if t.parent_task_id != parent_id:
                    continue
            elif top_level_only and t.parent_task_id is not None:
                continue
            results.append(t)
        results.sort(key=lambda t: t.last_activity_at, reverse=True)
        return results

    async def list_children(self, parent_id: UUID) -> list[Task]:
        return [t for t in self._tasks.values() if t.parent_task_id == parent_id]

    async def add_participants(
        self, task_id: UUID, participant_ids: list[UUID],
    ) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        for pid in participant_ids:
            if pid not in task.participants:
                task.participants.append(pid)

    async def close_task(self, task_id: UUID) -> Task:
        # `TaskNotFound` is raised by the service layer when the task is
        # missing — at the protocol level we mirror what `get_task` would
        # return, but the Protocol contract promises the caller already
        # holds `lock(task_id)`, which means the task existed at lock
        # acquisition. Treat a None here as a real "not found" and raise
        # the matching error so persistent backends behave the same.
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFound()
        if task.status == "closed":
            return task  # idempotent
        task.status = "closed"
        task.updated_at = _now()
        # Release the external_ref slot so re-`upsert` against the same
        # `(initiator, ref)` creates a fresh task. The closed task keeps
        # `task.external_ref` set for audit / history; only the index
        # entry is dropped.
        if task.external_ref is not None:
            self._external_refs.pop((task.initiator_id, task.external_ref), None)
        return task

    async def touch_task(
        self, task_id: UUID, last_activity_at: datetime,
    ) -> None:
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.last_activity_at = last_activity_at
        task.updated_at = last_activity_at

    # ---- events ----

    async def append_event(self, event: Event) -> Event:
        # Per-task monotonic seq assigned here so the log itself owns the
        # invariant. Callers hold `lock(task_id)` (see Protocol doc), so
        # the read-then-mutate is race-free within a replica.
        per_task = self._events_by_task.setdefault(event.task_id, [])
        event.seq = len(per_task) + 1
        self._events_by_id[event.id] = event
        per_task.append(event)
        # Idempotency index — only client-submitted events carry a key;
        # system events (`from_ is None`) and events without a key skip this.
        if event.client_event_id is not None and event.from_ is not None:
            self._client_event_index[
                (event.task_id, event.from_, event.client_event_id)
            ] = event.id
        return event

    async def get_event(self, event_id: UUID) -> Event | None:
        return self._events_by_id.get(event_id)

    async def list_events_for_task(self, task_id: UUID) -> list[Event]:
        return list(self._events_by_task.get(task_id, ()))

    async def find_event_by_client_id(
        self,
        task_id: UUID,
        from_id: UUID,
        client_event_id: UUID,
    ) -> Event | None:
        event_id = self._client_event_index.get(
            (task_id, from_id, client_event_id)
        )
        if event_id is None:
            return None
        return self._events_by_id.get(event_id)

    # ---- locking ----

    def lock(self, task_id: UUID) -> AbstractAsyncContextManager[None]:
        @asynccontextmanager
        async def _ctx() -> AsyncIterator[None]:
            lock = self._locks.get(task_id)
            if lock is None:
                # Lock was not created with the task; create lazily so callers
                # cannot dead-end on a missing-lock race.
                lock = self._locks.setdefault(task_id, asyncio.Lock())
            async with lock:
                yield

        return _ctx()


class InMemoryBlobStore:
    """V2 in-memory blob store. Hashes streamed input incrementally and
    enforces `max_bytes` mid-stream so oversize uploads abort early without
    materializing the whole body. Single-replica only — persistent backends
    (disk, S3) implement the same `BlobStore` protocol."""

    def __init__(self) -> None:
        self._meta: dict[UUID, Attachment] = {}
        self._bytes: dict[UUID, bytes] = {}
        # One task per blob, bound at upload time and immutable.
        self._task_id: dict[UUID, UUID] = {}

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
        blob_id = uuid4()
        hasher = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        async for chunk in stream:
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise BlobTooLarge(
                    f"upload exceeds the {max_bytes}-byte limit"
                )
            hasher.update(chunk)
            chunks.append(chunk)
        body = b"".join(chunks)
        attachment = Attachment(
            blob_id=blob_id,
            filename=filename,
            mime_type=mime_type,
            size_bytes=total,
            sha256=hasher.hexdigest(),
        )
        self._meta[blob_id] = attachment
        self._bytes[blob_id] = body
        self._task_id[blob_id] = task_id
        return attachment

    async def get_meta(self, blob_id: UUID) -> Attachment | None:
        return self._meta.get(blob_id)

    async def get_task_id(self, blob_id: UUID) -> UUID | None:
        return self._task_id.get(blob_id)

    async def open(self, blob_id: UUID) -> AsyncIterator[bytes]:
        # 64 KB chunks — keeps memory bounded for the StreamingResponse
        # consumer even when the underlying buffer is one big bytes blob.
        body = self._bytes.get(blob_id)
        if body is None:
            return
        chunk_size = 64 * 1024
        for i in range(0, len(body), chunk_size):
            yield body[i : i + chunk_size]


class InMemoryMemoryStore:
    """Single-replica `MemoryStore`. Owner-private key→document map held
    in-process — restart wipes it. Persistent backends (SQLite, Postgres)
    implement the same Protocol. No locking: under asyncio there is no
    `await` between the read and the mutate in `put_memory`, so the
    check-then-write is atomic within the replica."""

    def __init__(self) -> None:
        self._by_owner: dict[UUID, dict[str, MemoryEntry]] = {}

    async def put_memory(
        self, entry: MemoryEntry, *, max_entry_bytes: int, max_entries: int,
    ) -> tuple[MemoryEntry, bool]:
        if len(json.dumps(entry.value).encode()) > max_entry_bytes:
            raise MemoryEntryTooLarge()
        owner = self._by_owner.setdefault(entry.owner_id, {})
        existing = owner.get(entry.key)
        if existing is None and len(owner) >= max_entries:
            raise MemoryQuotaExceeded()
        if existing is not None:
            # Overwrite preserves the original id and created_at; only
            # value/tags/updated_at change (updated_at already reflects "now").
            entry = entry.model_copy(
                update={"id": existing.id, "created_at": existing.created_at}
            )
        owner[entry.key] = entry
        return entry, existing is None

    async def get_memory(self, owner_id: UUID, key: str) -> MemoryEntry | None:
        return self._by_owner.get(owner_id, {}).get(key)

    async def list_memory(
        self,
        owner_id: UUID,
        *,
        key_prefix: str | None = None,
        tags: list[str] | None = None,
    ) -> list[MemoryEntry]:
        entries = list(self._by_owner.get(owner_id, {}).values())
        if key_prefix:
            # `startswith` is an exact prefix match — no `_`/`%` wildcard
            # hazard, unlike the SQL backends' range-scan equivalent.
            entries = [e for e in entries if e.key.startswith(key_prefix)]
        if tags:
            wanted = set(tags)
            entries = [e for e in entries if wanted.issubset(e.tags)]
        entries.sort(key=lambda e: e.key)
        return entries

    async def delete_memory(self, owner_id: UUID, key: str) -> int:
        owner = self._by_owner.get(owner_id)
        if owner is not None and key in owner:
            del owner[key]
            return 1
        return 0

    async def purge_memory(self, owner_id: UUID, *, key_prefix: str) -> int:
        owner = self._by_owner.get(owner_id)
        if not owner:
            return 0
        doomed = [k for k in owner if k.startswith(key_prefix)]
        for k in doomed:
            del owner[k]
        return len(doomed)

    async def purge_owner(self, owner_id: UUID) -> int:
        owner = self._by_owner.pop(owner_id, None)
        return len(owner) if owner else 0
