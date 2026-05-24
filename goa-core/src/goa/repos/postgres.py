"""Postgres adapter for `TaskLog` and `ParticipantStore`.

Implements two of the three persistence Protocols against any Postgres
server reachable via an `asyncpg` connection pool. The headline target
is **Supabase Postgres** (direct connection, port 5432) but the same
adapter works against Amazon RDS, Neon, Crunchy, or a self-hosted
cluster — nothing here is Supabase-specific.

This adapter does **not** implement `BlobStore`. Blob bytes go to an
S3-compatible object store via [s3_blobs.py](s3_blobs.py); the blob
**metadata** row (filename, mime_type, size, sha256, task binding)
lives in this adapter's `blobs` table, so `BlobStore.get_meta` and
`get_task_id` are fast indexed Postgres reads rather than S3 HEADs.
`S3BlobStore` holds a reference back to this adapter and acquires
connections from the same pool.

Storage conventions:
- UUIDs as native `UUID`.
- Timestamps as `TIMESTAMPTZ`.
- JSON payloads as `JSONB`.
- Events use **hybrid storage**: typed columns for everything we index
  (`task_id`, `seq`, `event_type`, `from_id`, `in_reply_to`, `created_at`,
  `client_event_id`) plus a `body` JSONB column holding the full
  serialized event JSON. The read path round-trips through
  `TypeAdapter(Event)` so the discriminated-union variant is preserved.

Concurrency:
- One `asyncpg.Pool` owned by the adapter.
- Per-task `asyncio.Lock` dict (mirrors `InMemoryTaskLog` /
  `SqliteAdapter`) serializes the read-then-write `seq` generation
  inside the replica. Multi-replica advisory locks
  (`pg_advisory_xact_lock`) are deferred to the multi-replica roadmap
  item — single-replica is the supported deployment for Stage 2.

Connection mode (Supabase-specific guidance):
- **Use direct connection (port 5432)** for long-running hub processes.
  Prepared statements work normally; the asyncpg pool can carry
  per-connection state across requests.
- Transaction-mode pooler (port 6543) is explicitly **not** supported
  here — its lack of prepared-statement support would force
  `statement_cache_size=0`, which is the wrong default for a long-lived
  service. Use the session-mode pooler (port 5432, `aws-0-...
  pooler.supabase.com`) if IPv4 is required instead.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg
from pydantic import TypeAdapter

from goa.domain.models import Event, Participant, Task
from goa.errors import ExternalRefInUse, TaskNotFound


_EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


# Statements that must run outside a transaction (CREATE INDEX CONCURRENTLY
# would be one example; here we keep everything inside `_bootstrap`).
_SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
  id              UUID PRIMARY KEY,
  type            TEXT NOT NULL,
  name            TEXT NOT NULL,
  description     TEXT NOT NULL DEFAULT '',
  capabilities    JSONB NOT NULL DEFAULT '[]'::jsonb,
  access_policy   TEXT NOT NULL DEFAULT 'public',
  api_key_hash    TEXT NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_participants_api_key_hash
  ON participants(api_key_hash);

CREATE TABLE IF NOT EXISTS tasks (
  id                UUID PRIMARY KEY,
  initiator_id      UUID NOT NULL,
  parent_task_id    UUID,
  status            TEXT NOT NULL DEFAULT 'open',
  subject           TEXT NOT NULL DEFAULT '',
  external_ref      TEXT,
  metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL,
  updated_at        TIMESTAMPTZ NOT NULL,
  last_activity_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tasks_parent
  ON tasks(parent_task_id) WHERE parent_task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_tasks_last_activity
  ON tasks(last_activity_at DESC);
-- Partial unique index on `external_ref` is created in `_migrate()` so
-- the predicate's column references resolve even on DBs upgraded from
-- a pre-status schema.

CREATE TABLE IF NOT EXISTS task_participants (
  task_id        UUID NOT NULL,
  participant_id UUID NOT NULL,
  PRIMARY KEY (task_id, participant_id)
);
CREATE INDEX IF NOT EXISTS ix_tp_participant ON task_participants(participant_id);

CREATE TABLE IF NOT EXISTS events (
  id              UUID PRIMARY KEY,
  task_id         UUID NOT NULL,
  seq             BIGINT NOT NULL CHECK (seq > 0),
  event_type      TEXT NOT NULL,
  from_id         UUID,
  in_reply_to     UUID,
  client_event_id UUID,
  created_at      TIMESTAMPTZ NOT NULL,
  body            JSONB NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_task_seq ON events(task_id, seq);
CREATE INDEX IF NOT EXISTS ix_events_task_order ON events(task_id, seq);

-- Blob metadata only. Bytes live in S3-compatible storage; this table
-- carries everything needed for authz and listing — `get_meta` and
-- `get_task_id` resolve here without touching object storage.
CREATE TABLE IF NOT EXISTS blobs (
  id          UUID PRIMARY KEY,
  task_id     UUID NOT NULL,
  owner_id    UUID NOT NULL,
  filename    TEXT NOT NULL,
  mime_type   TEXT NOT NULL,
  size_bytes  BIGINT NOT NULL,
  sha256      TEXT NOT NULL,
  created_at  TIMESTAMPTZ NOT NULL,
  -- Object key under which the bytes are stored in the S3 bucket. The
  -- adapter chooses `<task_id>/<blob_id>` but stores the resolved key
  -- so a future scheme change does not lose old objects.
  object_key  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_blobs_task ON blobs(task_id);
"""


def _row_to_task(row: asyncpg.Record, participants: list[UUID]) -> Task:
    return Task(
        id=row["id"],
        initiator_id=row["initiator_id"],
        parent_task_id=row["parent_task_id"],
        status=row["status"],
        participants=participants,
        subject=row["subject"],
        external_ref=row["external_ref"],
        metadata=_load_jsonb(row["metadata"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_activity_at=row["last_activity_at"],
    )


def _row_to_participant(row: asyncpg.Record) -> Participant:
    return Participant(
        id=row["id"],
        type=row["type"],
        name=row["name"],
        description=row["description"],
        capabilities=_load_jsonb(row["capabilities"]),
        access_policy=row["access_policy"],
        api_key_hash=row["api_key_hash"],
        created_at=row["created_at"],
    )


def _load_jsonb(value: Any) -> Any:
    # asyncpg returns JSONB as `str` by default unless a codec is registered;
    # we register a codec in `_init_connection`, but be defensive in case
    # a connection slipped through (or the row came from a server-side cast
    # that bypassed the codec).
    if isinstance(value, (str, bytes)):
        return json.loads(value)
    return value


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Pool init hook. Registers a JSONB codec so JSONB columns round-trip
    as Python dict/list instead of raw `str`. Done once per connection."""
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


class PostgresAdapter:
    """Postgres-backed implementation of `TaskLog` and `ParticipantStore`.

    Pool is opened lazily by `__aenter__` (driven by FastAPI's lifespan).
    Construction is cheap and does no I/O — `Persistence.postgres(...)`
    is safe to call from `create_app` before the app starts.
    """

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 2,
        max_size: int = 10,
    ) -> None:
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None
        self._locks: dict[UUID, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "PostgresAdapter":
        self._pool = await asyncpg.create_pool(
            dsn=self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            init=_init_connection,
        )
        await self._bootstrap()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError(
                "PostgresAdapter is not entered — wrap in `async with` (or rely "
                "on FastAPI lifespan via create_app)."
            )
        return self._pool

    async def _bootstrap(self) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(_SCHEMA)
            await self._migrate(conn)

    async def _migrate(self, conn: asyncpg.Connection) -> None:
        """Forward-only schema migrations for existing DBs.

        Each block introspects the live schema via
        `information_schema.columns` (Postgres' answer to SQLite's
        `PRAGMA table_info`) and runs an `ALTER TABLE ADD COLUMN` when
        the column is missing. Indexes that reference newly-added
        columns also live here — Postgres resolves column names at
        `CREATE INDEX` parse time, so a partial index on a yet-to-exist
        column would fail if put in the main schema script.
        """
        # tasks.status — created in the main schema for fresh DBs, but
        # any legacy DB built before §8 needs it ALTERed in.
        if not await self._column_exists(conn, "tasks", "status"):
            await conn.execute(
                "ALTER TABLE tasks ADD COLUMN status TEXT NOT NULL DEFAULT 'open'"
            )
            # Drop the stale status-blind index if a legacy DB created
            # one under the same name. Idempotent.
            await conn.execute("DROP INDEX IF EXISTS ux_tasks_extref")
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_extref "
            "ON tasks(initiator_id, external_ref) "
            "WHERE external_ref IS NOT NULL AND status = 'open'"
        )

        # events.client_event_id — §13 idempotent append. Same pattern.
        if not await self._column_exists(conn, "events", "client_event_id"):
            await conn.execute(
                "ALTER TABLE events ADD COLUMN client_event_id UUID"
            )
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_events_caller_client "
            "ON events(task_id, from_id, client_event_id) "
            "WHERE client_event_id IS NOT NULL"
        )

    @staticmethod
    async def _column_exists(
        conn: asyncpg.Connection, table: str, column: str,
    ) -> bool:
        row = await conn.fetchrow(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = $1 AND column_name = $2",
            table,
            column,
        )
        return row is not None

    # ------------------------------------------------------------------
    # TaskLog
    # ------------------------------------------------------------------

    async def create_task(
        self, task: Task, *, external_ref: str | None = None,
    ) -> Task:
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        """
                        INSERT INTO tasks (
                          id, initiator_id, parent_task_id, status, subject,
                          external_ref, metadata, created_at, updated_at,
                          last_activity_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10)
                        """,
                        task.id,
                        task.initiator_id,
                        task.parent_task_id,
                        task.status,
                        task.subject,
                        task.external_ref,
                        json.dumps(task.metadata),
                        task.created_at,
                        task.updated_at,
                        task.last_activity_at,
                    )
                    if task.participants:
                        await conn.executemany(
                            "INSERT INTO task_participants (task_id, participant_id) "
                            "VALUES ($1, $2)",
                            [(task.id, pid) for pid in task.participants],
                        )
        except asyncpg.UniqueViolationError as e:
            # The only UNIQUE within create_task's write set that the
            # caller can trip is `ux_tasks_extref` — task PK collision
            # on a UUID is statistically zero, and task_participants is
            # keyed on a different column set.
            if external_ref is not None:
                raise ExternalRefInUse() from e
            raise
        # Pre-warm the lock so the first append takes it cheaply.
        self._locks.setdefault(task.id, asyncio.Lock())
        return task

    async def get_task(self, task_id: UUID) -> Task | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM tasks WHERE id = $1", task_id,
            )
            if row is None:
                return None
            participants = await self._participants_for_task(conn, task_id)
        return _row_to_task(row, participants)

    async def get_task_by_external_ref(
        self, initiator_id: UUID, external_ref: str,
    ) -> UUID | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM tasks "
                "WHERE initiator_id = $1 AND external_ref = $2 AND status = 'open'",
                initiator_id,
                external_ref,
            )
        return row["id"] if row else None

    async def list_tasks(
        self,
        *,
        parent_id: UUID | None = None,
        top_level_only: bool = True,
    ) -> list[Task]:
        if parent_id is not None:
            sql = (
                "SELECT * FROM tasks WHERE parent_task_id = $1 "
                "ORDER BY last_activity_at DESC"
            )
            params: tuple = (parent_id,)
        elif top_level_only:
            sql = (
                "SELECT * FROM tasks WHERE parent_task_id IS NULL "
                "ORDER BY last_activity_at DESC"
            )
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
        clauses = ["tp.participant_id = $1"]
        params: list = [participant_id]
        if role == "initiator":
            params.append(participant_id)
            clauses.append(f"t.initiator_id = ${len(params)}")
        elif role == "member":
            params.append(participant_id)
            clauses.append(f"t.initiator_id <> ${len(params)}")
        if parent_id is not None:
            params.append(parent_id)
            clauses.append(f"t.parent_task_id = ${len(params)}")
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
            "SELECT * FROM tasks WHERE parent_task_id = $1",
            (parent_id,),
        )

    async def add_participants(
        self, task_id: UUID, participant_ids: list[UUID],
    ) -> None:
        if not participant_ids:
            return
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(
                    "INSERT INTO task_participants (task_id, participant_id) "
                    "VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    [(task_id, pid) for pid in participant_ids],
                )

    async def touch_task(
        self, task_id: UUID, last_activity_at: datetime,
    ) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE tasks SET last_activity_at = $1, updated_at = $1 "
                    "WHERE id = $2",
                    last_activity_at,
                    task_id,
                )

    async def close_task(self, task_id: UUID) -> Task:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # The partial unique index on (initiator_id, external_ref)
                # filters on `status = 'open'`, so flipping the status here
                # drops this task out of the index automatically.
                status = await conn.execute(
                    "UPDATE tasks SET status = 'closed', updated_at = NOW() "
                    "WHERE id = $1 AND status = 'open'",
                    task_id,
                )
                # asyncpg returns the command tag, e.g. "UPDATE 1" or
                # "UPDATE 0" — split to count rows.
                changed = int(status.split()[-1]) if status else 0
                row = await conn.fetchrow(
                    "SELECT * FROM tasks WHERE id = $1", task_id,
                )
                if row is None:
                    raise TaskNotFound()
                participants = await self._participants_for_task(conn, task_id)
        task = _row_to_task(row, participants)
        # `changed == 0 and task.status == 'open'` would mean the row
        # disappeared between the UPDATE and the SELECT — impossible
        # under the documented per-task lock contract. Surface loud.
        assert task.status == "closed" or changed == 1, (
            "close_task UPDATE affected 0 rows but task is still open — "
            "lock contract violated?"
        )
        return task

    async def append_event(self, event: Event) -> Event:
        # Service layer holds `lock(task_id)` around this; the
        # read-then-write is therefore single-writer per task within
        # the replica.
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
                    "FROM events WHERE task_id = $1",
                    event.task_id,
                )
                event.seq = int(row["next_seq"])
                body = json.dumps(event.model_dump(mode="json"))
                await conn.execute(
                    """
                    INSERT INTO events (
                      id, task_id, seq, event_type, from_id, in_reply_to,
                      client_event_id, created_at, body
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                    """,
                    event.id,
                    event.task_id,
                    event.seq,
                    event.event_type,
                    event.from_,
                    event.in_reply_to,
                    event.client_event_id,
                    event.created_at,
                    body,
                )
        return event

    async def get_event(self, event_id: UUID) -> Event | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT body FROM events WHERE id = $1", event_id,
            )
        if row is None:
            return None
        return _EVENT_ADAPTER.validate_python(_load_jsonb(row["body"]))

    async def list_events_for_task(self, task_id: UUID) -> list[Event]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT body FROM events WHERE task_id = $1 ORDER BY seq",
                task_id,
            )
        return [
            _EVENT_ADAPTER.validate_python(_load_jsonb(r["body"]))
            for r in rows
        ]

    async def find_event_by_client_id(
        self,
        task_id: UUID,
        from_id: UUID,
        client_event_id: UUID,
    ) -> Event | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT body FROM events "
                "WHERE task_id = $1 AND from_id = $2 AND client_event_id = $3",
                task_id,
                from_id,
                client_event_id,
            )
        if row is None:
            return None
        return _EVENT_ADAPTER.validate_python(_load_jsonb(row["body"]))

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
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO participants (
                      id, type, name, description, capabilities,
                      access_policy, api_key_hash, created_at
                    ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8)
                    """,
                    participant.id,
                    participant.type,
                    participant.name,
                    participant.description,
                    json.dumps(list(participant.capabilities)),
                    participant.access_policy,
                    participant.api_key_hash,
                    participant.created_at,
                )
        return participant

    async def get(self, participant_id: UUID) -> Participant | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM participants WHERE id = $1", participant_id,
            )
        return _row_to_participant(row) if row else None

    async def get_by_api_key_hash(self, api_key_hash: str) -> Participant | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM participants WHERE api_key_hash = $1",
                api_key_hash,
            )
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
            params.append(type)
            clauses.append(f"type = ${len(params)}")
        if q:
            params.append(f"%{q.lower()}%")
            clauses.append(
                f"LOWER(name || ' ' || description) LIKE ${len(params)}"
            )
        sql = "SELECT * FROM participants"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, id"
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        results = [_row_to_participant(r) for r in rows]
        if capabilities:
            wanted = set(capabilities)
            results = [p for p in results if wanted.issubset(p.capabilities)]
        return results

    async def delete(self, participant_id: UUID) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM participants WHERE id = $1", participant_id,
                )

    async def update(self, participant: Participant) -> Participant:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE participants
                    SET name = $1, description = $2,
                        capabilities = $3::jsonb, access_policy = $4
                    WHERE id = $5
                    """,
                    participant.name,
                    participant.description,
                    json.dumps(list(participant.capabilities)),
                    participant.access_policy,
                    participant.id,
                )
        return participant

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _participants_for_task(
        self, conn: asyncpg.Connection, task_id: UUID,
    ) -> list[UUID]:
        rows = await conn.fetch(
            "SELECT participant_id FROM task_participants WHERE task_id = $1",
            task_id,
        )
        return [r["participant_id"] for r in rows]

    async def _hydrate_tasks(self, sql: str, params: tuple) -> list[Task]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            tasks: list[Task] = []
            for row in rows:
                participants = await self._participants_for_task(conn, row["id"])
                tasks.append(_row_to_task(row, participants))
        return tasks
