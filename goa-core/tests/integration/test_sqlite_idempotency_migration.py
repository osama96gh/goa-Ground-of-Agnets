"""SQLite migration test for `client_event_id` (§8 Design Decisions).

Simulates a legacy DB created before idempotency landed — `events` table
present but without the `client_event_id` column — then opens it with
the current `SqliteAdapter` and verifies:

1. `_migrate()` adds the column via `ALTER TABLE ADD COLUMN` (idempotent
   in effect because we check `PRAGMA table_info` first).
2. The partial unique index on `(task_id, from_id, client_event_id)`
   gets created after the column lands.
3. Idempotent append works end-to-end against the migrated DB (round-
   trip through the column, not just in-memory state).
4. Re-entering the adapter on the now-current-schema DB is a no-op —
   the migration is self-skipping.

Restart-safety (event survives `__aexit__` / `__aenter__`) is covered
by `test_sqlite_e2e.py::test_sqlite_persists_across_restart` — this
file focuses on the schema-evolution path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import aiosqlite
import pytest

from goa.domain.models import Content, InfoEvent, InfoPayload, Task
from goa.repos.sqlite import SqliteAdapter


pytestmark = pytest.mark.asyncio


_LEGACY_EVENTS_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE participants (
  id              TEXT PRIMARY KEY,
  type            TEXT NOT NULL,
  name            TEXT NOT NULL,
  description     TEXT NOT NULL DEFAULT '',
  capabilities    TEXT NOT NULL DEFAULT '[]',
  access_policy   TEXT NOT NULL DEFAULT 'public',
  api_key_hash    TEXT NOT NULL,
  created_at      TEXT NOT NULL
);

CREATE TABLE tasks (
  id                TEXT PRIMARY KEY,
  initiator_id      TEXT NOT NULL,
  parent_task_id    TEXT,
  subject           TEXT NOT NULL DEFAULT '',
  external_ref      TEXT,
  metadata          TEXT NOT NULL DEFAULT '{}',
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  last_activity_at  TEXT NOT NULL
);

CREATE TABLE task_participants (
  task_id        TEXT NOT NULL,
  participant_id TEXT NOT NULL,
  PRIMARY KEY (task_id, participant_id)
);

-- Legacy events table — NO client_event_id column. This is what an
-- on-disk DB created before §8 idempotency would look like.
CREATE TABLE events (
  id           TEXT PRIMARY KEY,
  task_id      TEXT NOT NULL,
  seq          INTEGER NOT NULL CHECK (seq > 0),
  event_type   TEXT NOT NULL,
  from_id      TEXT,
  in_reply_to  TEXT,
  created_at   TEXT NOT NULL,
  body         TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_events_task_seq ON events(task_id, seq);

CREATE TABLE blobs (
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
"""


async def _seed_legacy_db(path: Path) -> None:
    """Create a DB with the legacy schema (no `client_event_id` column)
    and insert one task + one event so the migration has real rows to
    coexist with."""
    conn = await aiosqlite.connect(path, isolation_level=None)
    try:
        await conn.executescript(_LEGACY_EVENTS_SCHEMA)
        now = datetime.now(tz=timezone.utc).isoformat()
        await conn.execute(
            "INSERT INTO tasks (id, initiator_id, parent_task_id, subject, "
            "external_ref, metadata, created_at, updated_at, last_activity_at) "
            "VALUES (?, ?, NULL, '', NULL, '{}', ?, ?, ?)",
            (str(uuid4()), str(uuid4()), now, now, now),
        )
    finally:
        await conn.close()


async def test_migrate_adds_client_event_id_column(tmp_path: Path) -> None:
    """Open a legacy DB; the `_migrate` step in `__aenter__` ALTERs the
    column in. `PRAGMA table_info` then reports it."""
    db = tmp_path / "legacy.db"
    await _seed_legacy_db(db)

    async with SqliteAdapter(db) as adapter:
        async with adapter.conn.execute("PRAGMA table_info(events)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        assert "client_event_id" in cols


async def test_migrate_creates_partial_unique_index(tmp_path: Path) -> None:
    """The partial unique index `ux_events_caller_client` must exist after
    migration. We assert by querying `sqlite_master`."""
    db = tmp_path / "legacy.db"
    await _seed_legacy_db(db)

    async with SqliteAdapter(db) as adapter:
        async with adapter.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='ux_events_caller_client'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None, "partial unique index was not created"


async def test_idempotent_append_works_after_migration(tmp_path: Path) -> None:
    """End-to-end: open a legacy DB, append an event with a key, look it
    up by key. The column → index → lookup path round-trips correctly
    despite the schema having been added by ALTER."""
    db = tmp_path / "legacy.db"
    await _seed_legacy_db(db)

    async with SqliteAdapter(db) as adapter:
        task = Task(initiator_id=uuid4(), participants=[uuid4()])
        await adapter.create_task(task)

        caller = uuid4()
        key = uuid4()
        event = InfoEvent(
            task_id=task.id,
            from_=caller,
            content=Content(text="hi"),
            payload=InfoPayload(),
            client_event_id=key,
        )
        async with adapter.lock(task.id):
            persisted = await adapter.append_event(event)

        got = await adapter.find_event_by_client_id(task.id, caller, key)
        assert got is not None
        assert got.id == persisted.id
        assert got.client_event_id == key


async def test_migration_is_idempotent_on_current_schema(tmp_path: Path) -> None:
    """Open a fresh DB (current schema), close, re-open. The second
    `__aenter__` must not error — `_migrate` skips the ALTER when the
    column is already present, and the `CREATE INDEX IF NOT EXISTS`
    is naturally idempotent."""
    db = tmp_path / "current.db"

    async with SqliteAdapter(db) as adapter:
        task = Task(initiator_id=uuid4(), participants=[uuid4()])
        await adapter.create_task(task)

    # Second open — would raise if `_migrate` were not idempotent.
    async with SqliteAdapter(db) as adapter2:
        got = await adapter2.get_task(task.id)
        assert got is not None
        assert got.id == task.id


async def test_legacy_event_survives_migration(tmp_path: Path) -> None:
    """Rows inserted under the legacy schema must still be readable after
    the column is added — `client_event_id` reads back as `None`."""
    db = tmp_path / "legacy.db"
    await _seed_legacy_db(db)

    # Insert one event under the legacy schema directly.
    pre_event_id = uuid4()
    legacy_task_id: UUID | None = None
    conn = await aiosqlite.connect(db, isolation_level=None)
    conn.row_factory = aiosqlite.Row
    try:
        async with conn.execute("SELECT id FROM tasks LIMIT 1") as cur:
            row = await cur.fetchone()
        assert row is not None
        legacy_task_id = UUID(row["id"])
        now = datetime.now(tz=timezone.utc).isoformat()
        # Build the legacy event JSON body — `client_event_id` is absent.
        body = (
            f'{{"id": "{pre_event_id}", "task_id": "{legacy_task_id}", '
            f'"seq": 1, "event_type": "info", "from": null, '
            f'"content": {{"text": "pre"}}, "in_reply_to": null, '
            f'"metadata": {{}}, "payload": {{}}, '
            f'"created_at": "{now}"}}'
        )
        await conn.execute(
            "INSERT INTO events (id, task_id, seq, event_type, from_id, "
            "in_reply_to, created_at, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(pre_event_id), str(legacy_task_id), 1, "info", None, None, now, body),
        )
    finally:
        await conn.close()

    assert legacy_task_id is not None
    async with SqliteAdapter(db) as adapter:
        got = await adapter.get_event(pre_event_id)
        assert got is not None
        assert got.id == pre_event_id
        # `_EventBase.client_event_id` defaults to None when missing
        # from the body — that's what gives us forward compatibility
        # for legacy rows.
        assert got.client_event_id is None
