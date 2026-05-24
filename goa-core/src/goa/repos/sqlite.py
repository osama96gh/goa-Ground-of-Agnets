"""SQLite adapter for `TaskLog`, `ParticipantStore`, and `BlobStore`.

One `SqliteAdapter` owns one `aiosqlite.Connection` and implements all
three Protocols at once. `Persistence.sqlite(path)` stuffs the same
adapter instance into all three slots of the bundle; `Persistence`
dedupes by identity on `__aenter__` so the connection is opened once.

**Single-replica only.** This adapter assumes one hub process per
database file. Running two hubs against the same file is unsupported
— `UNIQUE(task_id, seq)` will surface as `IntegrityError` rather than
silent divergence, but the right answer is one hub per file. Multi-
replica coordination (LISTEN/NOTIFY-style invalidation, cross-replica
SSE fanout) is Stage 2.

Storage conventions:
- UUIDs as canonical hyphenated TEXT (matches `model_dump(mode="json")`).
- Timestamps as ISO-8601 TEXT with `+00:00` suffix (sorts lexicographically;
  Python `datetime.fromisoformat()` round-trips natively).
- Event rows use **hybrid storage**: typed columns for everything we
  index/filter (`task_id`, `seq`, `event_type`, `from_id`, `in_reply_to`,
  `created_at`), plus a `body` TEXT column holding the full serialized
  event JSON. Read path round-trips through `TypeAdapter(Event)` so the
  discriminated-union variant is reconstructed faithfully.

Concurrency:
- A per-task `asyncio.Lock` dict (mirrors `InMemoryTaskLog`) is what
  serializes the read-then-write `seq` generation inside the replica.
- Each write transaction is `BEGIN IMMEDIATE ... COMMIT` so SQLite
  refuses concurrent writers at the file level — defensive against
  the unsupported multi-process case.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import aiosqlite
from pydantic import TypeAdapter

from goa.domain.models import Attachment, Event, Participant, Task
from goa.errors import BlobTooLarge, ExternalRefInUse, TaskNotFound


_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS participants (
  id              TEXT PRIMARY KEY,
  type            TEXT NOT NULL,
  name            TEXT NOT NULL,
  description     TEXT NOT NULL DEFAULT '',
  capabilities    TEXT NOT NULL DEFAULT '[]',
  access_policy   TEXT NOT NULL DEFAULT 'public',
  api_key_hash    TEXT NOT NULL,
  created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_participants_api_key_hash
  ON participants(api_key_hash);

CREATE TABLE IF NOT EXISTS tasks (
  id                TEXT PRIMARY KEY,
  initiator_id      TEXT NOT NULL,
  parent_task_id    TEXT,
  -- Lifecycle (§8). Defaults to 'open'; transitions to 'closed' via
  -- `close_task` which also drops the row out of the external_ref
  -- unique index by virtue of the index's partial predicate.
  status            TEXT NOT NULL DEFAULT 'open',
  subject           TEXT NOT NULL DEFAULT '',
  external_ref      TEXT,
  metadata          TEXT NOT NULL DEFAULT '{}',
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  last_activity_at  TEXT NOT NULL
);
-- Note: the partial unique index on `external_ref` is created in
-- `SqliteAdapter._migrate()` because its predicate references the
-- `status` column, which on legacy DBs is ALTERed in at startup —
-- and `CREATE INDEX` resolves column names at parse time.
CREATE INDEX IF NOT EXISTS ix_tasks_parent
  ON tasks(parent_task_id) WHERE parent_task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_tasks_last_activity
  ON tasks(last_activity_at DESC);

CREATE TABLE IF NOT EXISTS task_participants (
  task_id        TEXT NOT NULL,
  participant_id TEXT NOT NULL,
  PRIMARY KEY (task_id, participant_id)
);
CREATE INDEX IF NOT EXISTS ix_tp_participant ON task_participants(participant_id);

CREATE TABLE IF NOT EXISTS events (
  id              TEXT PRIMARY KEY,
  task_id         TEXT NOT NULL,
  -- CHECK(seq > 0): defense against any future path that bypasses
  -- `append_event` and inserts the placeholder `seq=0` from _EventBase.
  -- Append-time assignment under `lock(task_id)` always produces >= 1.
  seq             INTEGER NOT NULL CHECK (seq > 0),
  event_type      TEXT NOT NULL,
  from_id         TEXT,
  in_reply_to     TEXT,
  -- Optional idempotency key (§13). NULL for system events (`from_id` NULL)
  -- and for client appends that opt out. Uniqueness enforced by a partial
  -- index keyed on `(task_id, from_id, client_event_id)`.
  client_event_id TEXT,
  created_at      TEXT NOT NULL,
  body            TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_task_seq ON events(task_id, seq);
CREATE INDEX IF NOT EXISTS ix_events_task_order ON events(task_id, seq);
-- Note: the partial unique index on `client_event_id` is created in
-- `SqliteAdapter._migrate()` *after* the legacy-DB column add. Putting
-- it here would fail on existing DBs that predate the column because
-- `CREATE INDEX IF NOT EXISTS` still requires the referenced column to
-- exist at parse time.

CREATE TABLE IF NOT EXISTS blobs (
  id          TEXT PRIMARY KEY,
  task_id     TEXT NOT NULL,
  owner_id    TEXT NOT NULL,
  filename    TEXT NOT NULL,
  mime_type   TEXT NOT NULL,
  size_bytes  INTEGER NOT NULL,
  sha256      TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  body        BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_blobs_task ON blobs(task_id);
"""


def _task_to_row(task: Task) -> tuple:
    return (
        str(task.id),
        str(task.initiator_id),
        str(task.parent_task_id) if task.parent_task_id else None,
        task.status,
        task.subject,
        task.external_ref,
        json.dumps(task.metadata),
        task.created_at.isoformat(),
        task.updated_at.isoformat(),
        task.last_activity_at.isoformat(),
    )


def _row_to_task(row: aiosqlite.Row, participants: list[UUID]) -> Task:
    return Task(
        id=UUID(row["id"]),
        initiator_id=UUID(row["initiator_id"]),
        parent_task_id=UUID(row["parent_task_id"]) if row["parent_task_id"] else None,
        status=row["status"],
        participants=participants,
        subject=row["subject"],
        external_ref=row["external_ref"],
        metadata=json.loads(row["metadata"]),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
        last_activity_at=_parse_dt(row["last_activity_at"]),
    )


def _parse_dt(s: str):
    # Stored as ISO-8601 with +00:00 suffix from `datetime.isoformat()`.
    from datetime import datetime

    return datetime.fromisoformat(s)


def _participant_to_row(p: Participant) -> tuple:
    return (
        str(p.id),
        p.type,
        p.name,
        p.description,
        json.dumps(list(p.capabilities)),
        p.access_policy,
        p.api_key_hash,
        p.created_at.isoformat(),
    )


def _row_to_participant(row: aiosqlite.Row) -> Participant:
    return Participant(
        id=UUID(row["id"]),
        type=row["type"],
        name=row["name"],
        description=row["description"],
        capabilities=json.loads(row["capabilities"]),
        access_policy=row["access_policy"],
        api_key_hash=row["api_key_hash"],
        created_at=_parse_dt(row["created_at"]),
    )


class SqliteAdapter:
    """SQLite-backed implementation of `TaskLog`, `ParticipantStore`, and
    `BlobStore`. One instance per database file.

    The connection is opened lazily by `__aenter__` (typically driven by
    FastAPI's `lifespan`). Construction is cheap and does no I/O.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._locks: dict[UUID, asyncio.Lock] = {}
        # One connection-level lock around explicit BEGIN/COMMIT pairs
        # so concurrent transactions within the same Python process don't
        # interleave their statements. The per-task asyncio.Lock above
        # serializes service-layer read-then-write; this is a defense
        # against unrelated writers racing the same connection.
        self._conn_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "SqliteAdapter":
        # `isolation_level=None` disables aiosqlite's implicit BEGIN so
        # we control every transaction boundary explicitly.
        conn = await aiosqlite.connect(self._path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.executescript(_SCHEMA)
        self._conn = conn
        await self._migrate()
        return self

    async def _migrate(self) -> None:
        """Forward-only schema migrations for existing DBs.

        `CREATE TABLE IF NOT EXISTS` is a no-op on tables that already
        exist — it does not pick up new columns. Each block below
        introspects the live schema via `PRAGMA table_info` and runs
        an `ALTER TABLE ADD COLUMN` when the column is missing.

        Indexes that reference newly-added columns also live here:
        SQLite resolves column names at `CREATE INDEX` parse time, so
        a partial index on a yet-to-exist column would fail if put in
        the main schema script and executed against a legacy DB.
        """
        # events.client_event_id — added 2026-05-19 (§13 idempotent append).
        async with self.conn.execute("PRAGMA table_info(events)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        if "client_event_id" not in cols:
            await self.conn.execute(
                "ALTER TABLE events ADD COLUMN client_event_id TEXT"
            )
        # Partial unique index — idempotent (`IF NOT EXISTS`). Created
        # here because on legacy DBs the column above must land first.
        await self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_events_caller_client "
            "ON events(task_id, from_id, client_event_id) "
            "WHERE client_event_id IS NOT NULL"
        )

        # tasks.status — added 2026-05-20 (§8 explicit task close).
        # The external_ref unique index also changes shape: from
        # `WHERE external_ref IS NOT NULL` to additionally `AND status = 'open'`,
        # so closed tasks release their slot. The index name stays the
        # same; we drop+recreate when we just added the column.
        async with self.conn.execute("PRAGMA table_info(tasks)") as cur:
            task_cols = {row["name"] for row in await cur.fetchall()}
        if "status" not in task_cols:
            await self.conn.execute(
                "ALTER TABLE tasks ADD COLUMN status TEXT NOT NULL DEFAULT 'open'"
            )
            # The legacy status-blind index was the old form; drop it so
            # the new partial form can take over the name. Skipped on
            # fresh DBs (the name never existed).
            await self.conn.execute("DROP INDEX IF EXISTS ux_tasks_extref")
        # Idempotent: on already-migrated DBs the index already exists in
        # the new form; on fresh DBs and just-migrated DBs we create it.
        await self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_extref "
            "ON tasks(initiator_id, external_ref) "
            "WHERE external_ref IS NOT NULL AND status = 'open'"
        )

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError(
                "SqliteAdapter is not entered — wrap in `async with` (or rely "
                "on FastAPI lifespan via create_app)."
            )
        return self._conn

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        """BEGIN IMMEDIATE ... COMMIT, with ROLLBACK on any exception."""
        async with self._conn_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            else:
                await self.conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # TaskLog
    # ------------------------------------------------------------------

    async def create_task(
        self, task: Task, *, external_ref: str | None = None,
    ) -> Task:
        try:
            async with self._transaction():
                await self.conn.execute(
                    """
                    INSERT INTO tasks (
                      id, initiator_id, parent_task_id, status, subject,
                      external_ref, metadata, created_at, updated_at,
                      last_activity_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _task_to_row(task),
                )
                await self.conn.executemany(
                    "INSERT INTO task_participants (task_id, participant_id) VALUES (?, ?)",
                    [(str(task.id), str(pid)) for pid in task.participants],
                )
        except sqlite3.IntegrityError as e:
            # Two-layer check, both stable across SQLite versions:
            # 1. `sqlite_errorname` (Python 3.11+) identifies the
            #    constraint *kind* — UNIQUE vs FK vs CHECK vs etc.
            # 2. `external_ref is not None` narrows scope. Within
            #    `create_task`'s write set, the only UNIQUE constraint
            #    that can fire when `external_ref` is set is the
            #    partial unique index on (initiator_id, external_ref);
            #    task PK collision on a UUID is statistically zero, and
            #    `task_participants` is keyed on a different column set.
            # Any other IntegrityError re-raises unchanged.
            is_unique = getattr(e, "sqlite_errorname", "") == "SQLITE_CONSTRAINT_UNIQUE"
            if external_ref is not None and is_unique:
                raise ExternalRefInUse() from e
            raise
        # Pre-warm the per-task lock so the service can take it on the
        # first append without an extra dict roundtrip.
        self._locks.setdefault(task.id, asyncio.Lock())
        return task

    async def get_task(self, task_id: UUID) -> Task | None:
        async with self.conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (str(task_id),)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        participants = await self._participants_for_task(task_id)
        return _row_to_task(row, participants)

    async def get_task_by_external_ref(
        self, initiator_id: UUID, external_ref: str,
    ) -> UUID | None:
        # `status = 'open'` filter matches the partial unique index
        # predicate, so closed tasks are invisible to upsert lookups
        # and the slot is effectively released (§8). Closed tasks
        # remain readable via `get_task` for audit.
        async with self.conn.execute(
            "SELECT id FROM tasks "
            "WHERE initiator_id = ? AND external_ref = ? AND status = 'open'",
            (str(initiator_id), external_ref),
        ) as cur:
            row = await cur.fetchone()
        return UUID(row["id"]) if row else None

    async def list_tasks(
        self,
        *,
        parent_id: UUID | None = None,
        top_level_only: bool = True,
    ) -> list[Task]:
        if parent_id is not None:
            sql = "SELECT * FROM tasks WHERE parent_task_id = ? ORDER BY last_activity_at DESC"
            params: tuple = (str(parent_id),)
        elif top_level_only:
            sql = "SELECT * FROM tasks WHERE parent_task_id IS NULL ORDER BY last_activity_at DESC"
            params = ()
        else:
            sql = "SELECT * FROM tasks ORDER BY last_activity_at DESC"
            params = ()
        return await self._hydrate_tasks(sql, params)

    async def list_tasks_for_participant(
        self,
        participant_id: UUID,
        *,
        role: str | None = None,
        parent_id: UUID | None = None,
        top_level_only: bool = True,
    ) -> list[Task]:
        clauses = ["tp.participant_id = ?"]
        params: list = [str(participant_id)]
        if role == "initiator":
            clauses.append("t.initiator_id = ?")
            params.append(str(participant_id))
        elif role == "member":
            clauses.append("t.initiator_id <> ?")
            params.append(str(participant_id))
        if parent_id is not None:
            clauses.append("t.parent_task_id = ?")
            params.append(str(parent_id))
        elif top_level_only:
            clauses.append("t.parent_task_id IS NULL")
        sql = (
            "SELECT t.* FROM tasks t "
            "JOIN task_participants tp ON tp.task_id = t.id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY t.last_activity_at DESC"
        )
        return await self._hydrate_tasks(sql, tuple(params))

    async def list_children(self, parent_id: UUID) -> list[Task]:
        return await self._hydrate_tasks(
            "SELECT * FROM tasks WHERE parent_task_id = ?",
            (str(parent_id),),
        )

    async def add_participants(
        self, task_id: UUID, participant_ids: list[UUID],
    ) -> None:
        if not participant_ids:
            return
        async with self._transaction():
            await self.conn.executemany(
                "INSERT OR IGNORE INTO task_participants (task_id, participant_id) "
                "VALUES (?, ?)",
                [(str(task_id), str(pid)) for pid in participant_ids],
            )

    async def touch_task(
        self, task_id: UUID, last_activity_at: datetime,
    ) -> None:
        ts = last_activity_at.isoformat()
        async with self._transaction():
            await self.conn.execute(
                "UPDATE tasks SET last_activity_at = ?, updated_at = ? WHERE id = ?",
                (ts, ts, str(task_id)),
            )

    async def close_task(self, task_id: UUID) -> Task:
        # The partial unique index on `(initiator_id, external_ref)` is
        # predicated on `status = 'open'`, so flipping the status here
        # drops this task's row out of the index automatically — no
        # separate slot-release step. Closed-task data is preserved.
        now = datetime.now(tz=timezone.utc).isoformat()
        async with self._transaction():
            cur = await self.conn.execute(
                "UPDATE tasks SET status = 'closed', updated_at = ? "
                "WHERE id = ? AND status = 'open'",
                (now, str(task_id)),
            )
            # `cur.rowcount` distinguishes the three outcomes: 1 = we
            # closed it; 0 = either missing or already-closed (idempotent).
            changed = cur.rowcount
        # Read back to return the canonical Task. Either path needs the
        # fresh row — for the just-closed case, to pick up the new
        # status/updated_at columns; for the idempotent case, to
        # distinguish missing from already-closed.
        task = await self.get_task(task_id)
        if task is None:
            raise TaskNotFound()
        # If `changed == 0` and `task.status == 'open'`, something else
        # raced us — unreachable under the documented per-task lock
        # contract, but we surface it loud rather than returning a stale
        # `open` task to a caller expecting `closed`.
        assert task.status == "closed" or changed == 1, (
            "close_task UPDATE affected 0 rows but task is still open — "
            "lock contract violated?"
        )
        return task

    async def append_event(self, event: Event) -> Event:
        # Service layer holds `lock(task_id)` around this; the read-then-
        # insert is therefore single-writer per task within the replica.
        # `BEGIN IMMEDIATE` is defense against the unsupported multi-process
        # case (SQLite will refuse + raise rather than silently divergent
        # seq values).
        async with self._transaction():
            async with self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM events WHERE task_id = ?",
                (str(event.task_id),),
            ) as cur:
                row = await cur.fetchone()
            event.seq = int(row["next_seq"])
            body = json.dumps(event.model_dump(mode="json"))
            await self.conn.execute(
                """
                INSERT INTO events (
                  id, task_id, seq, event_type, from_id, in_reply_to,
                  client_event_id, created_at, body
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event.id),
                    str(event.task_id),
                    event.seq,
                    event.event_type,
                    str(event.from_) if event.from_ else None,
                    str(event.in_reply_to) if event.in_reply_to else None,
                    str(event.client_event_id) if event.client_event_id else None,
                    event.created_at.isoformat(),
                    body,
                ),
            )
        return event

    async def get_event(self, event_id: UUID) -> Event | None:
        async with self.conn.execute(
            "SELECT body FROM events WHERE id = ?", (str(event_id),)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return _EVENT_ADAPTER.validate_python(json.loads(row["body"]))

    async def list_events_for_task(self, task_id: UUID) -> list[Event]:
        async with self.conn.execute(
            "SELECT body FROM events WHERE task_id = ? ORDER BY seq",
            (str(task_id),),
        ) as cur:
            rows = await cur.fetchall()
        return [_EVENT_ADAPTER.validate_python(json.loads(r["body"])) for r in rows]

    async def find_event_by_client_id(
        self,
        task_id: UUID,
        from_id: UUID,
        client_event_id: UUID,
    ) -> Event | None:
        # Caller holds `lock(task_id)`. The partial unique index
        # `ux_events_caller_client` makes this a single B-tree lookup;
        # we hydrate from `body` so the discriminated-union variant is
        # preserved (same path as `get_event` / `list_events_for_task`).
        async with self.conn.execute(
            "SELECT body FROM events "
            "WHERE task_id = ? AND from_id = ? AND client_event_id = ?",
            (str(task_id), str(from_id), str(client_event_id)),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return _EVENT_ADAPTER.validate_python(json.loads(row["body"]))

    def lock(self, task_id: UUID) -> AbstractAsyncContextManager[None]:
        @asynccontextmanager
        async def _ctx() -> AsyncIterator[None]:
            lock = self._locks.get(task_id)
            if lock is None:
                lock = self._locks.setdefault(task_id, asyncio.Lock())
            async with lock:
                yield

        return _ctx()

    # ------------------------------------------------------------------
    # ParticipantStore
    # ------------------------------------------------------------------

    async def create(self, participant: Participant) -> Participant:
        async with self._transaction():
            await self.conn.execute(
                """
                INSERT INTO participants (
                  id, type, name, description, capabilities,
                  access_policy, api_key_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _participant_to_row(participant),
            )
        return participant

    async def get(self, participant_id: UUID) -> Participant | None:
        async with self.conn.execute(
            "SELECT * FROM participants WHERE id = ?", (str(participant_id),)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_participant(row) if row else None

    async def get_by_api_key_hash(self, api_key_hash: str) -> Participant | None:
        async with self.conn.execute(
            "SELECT * FROM participants WHERE api_key_hash = ?", (api_key_hash,)
        ) as cur:
            row = await cur.fetchone()
        return _row_to_participant(row) if row else None

    async def search(
        self,
        *,
        capabilities: list[str] | None = None,
        q: str | None = None,
        type: str | None = None,
    ) -> list[Participant]:
        clauses: list[str] = []
        params: list = []
        if type is not None:
            clauses.append("type = ?")
            params.append(type)
        if q:
            clauses.append("LOWER(name || ' ' || description) LIKE ?")
            params.append(f"%{q.lower()}%")
        sql = "SELECT * FROM participants"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, id"
        async with self.conn.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        results = [_row_to_participant(r) for r in rows]
        # Capabilities AND-filter applied in Python because the JSON-array
        # storage doesn't have a clean SQLite-portable contains operator.
        if capabilities:
            wanted = set(capabilities)
            results = [p for p in results if wanted.issubset(p.capabilities)]
        return results

    async def delete(self, participant_id: UUID) -> None:
        async with self._transaction():
            await self.conn.execute(
                "DELETE FROM participants WHERE id = ?", (str(participant_id),)
            )

    async def update(self, participant: Participant) -> Participant:
        async with self._transaction():
            await self.conn.execute(
                """
                UPDATE participants
                SET name = ?, description = ?, capabilities = ?, access_policy = ?
                WHERE id = ?
                """,
                (
                    participant.name,
                    participant.description,
                    json.dumps(list(participant.capabilities)),
                    participant.access_policy,
                    str(participant.id),
                ),
            )
        return participant

    # ------------------------------------------------------------------
    # BlobStore
    # ------------------------------------------------------------------

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
        from datetime import datetime, timezone

        attachment = Attachment(
            blob_id=blob_id,
            filename=filename,
            mime_type=mime_type,
            size_bytes=total,
            sha256=hasher.hexdigest(),
        )
        async with self._transaction():
            await self.conn.execute(
                """
                INSERT INTO blobs (
                  id, task_id, owner_id, filename, mime_type,
                  size_bytes, sha256, created_at, body
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(blob_id),
                    str(task_id),
                    str(owner_id),
                    filename,
                    mime_type,
                    total,
                    hasher.hexdigest(),
                    datetime.now(tz=timezone.utc).isoformat(),
                    body,
                ),
            )
        return attachment

    async def get_meta(self, blob_id: UUID) -> Attachment | None:
        async with self.conn.execute(
            "SELECT filename, mime_type, size_bytes, sha256 FROM blobs WHERE id = ?",
            (str(blob_id),),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return Attachment(
            blob_id=blob_id,
            filename=row["filename"],
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
        )

    async def get_task_id(self, blob_id: UUID) -> UUID | None:
        async with self.conn.execute(
            "SELECT task_id FROM blobs WHERE id = ?", (str(blob_id),)
        ) as cur:
            row = await cur.fetchone()
        return UUID(row["task_id"]) if row else None

    async def open(self, blob_id: UUID) -> AsyncIterator[bytes]:
        async with self.conn.execute(
            "SELECT body FROM blobs WHERE id = ?", (str(blob_id),)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return
        body: bytes = row["body"]
        chunk_size = 64 * 1024
        for i in range(0, len(body), chunk_size):
            yield body[i : i + chunk_size]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _participants_for_task(self, task_id: UUID) -> list[UUID]:
        async with self.conn.execute(
            "SELECT participant_id FROM task_participants WHERE task_id = ?",
            (str(task_id),),
        ) as cur:
            rows = await cur.fetchall()
        return [UUID(r["participant_id"]) for r in rows]

    async def _hydrate_tasks(self, sql: str, params: tuple) -> list[Task]:
        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        tasks: list[Task] = []
        for row in rows:
            task_id = UUID(row["id"])
            participants = await self._participants_for_task(task_id)
            tasks.append(_row_to_task(row, participants))
        return tasks
