"""SQLite migration test for `tasks.status` (§8 explicit task close).

Simulates a legacy DB created before task-close landed:
- `tasks` table without the `status` column
- the legacy status-blind unique index `ux_tasks_extref` on
  `(initiator_id, external_ref) WHERE external_ref IS NOT NULL`

Then opens it with the current `SqliteAdapter` and verifies:

1. The `status` column is added via `ALTER TABLE` and defaults to `'open'`
   for existing rows.
2. The legacy status-blind index is dropped and replaced by the new
   partial form `WHERE external_ref IS NOT NULL AND status = 'open'`.
3. `close_task` works end-to-end against the migrated DB and releases
   the external_ref slot (a follow-up `create_task` with the same ref
   succeeds because the partial index no longer covers the closed row).
4. Re-entering the adapter on the now-current-schema DB is a no-op —
   the migration is self-skipping.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest

from goa.domain.models import Task
from goa.repos.sqlite import SqliteAdapter


pytestmark = pytest.mark.asyncio


_LEGACY_SCHEMA = """
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

-- Legacy tasks table — NO `status` column. This is the shape an on-disk
-- DB would have if it was created before §8 task close.
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
-- Legacy status-blind unique index. The migration must drop this and
-- create the new partial form `WHERE ... AND status = 'open'`.
CREATE UNIQUE INDEX ux_tasks_extref
  ON tasks(initiator_id, external_ref) WHERE external_ref IS NOT NULL;

CREATE TABLE task_participants (
  task_id        TEXT NOT NULL,
  participant_id TEXT NOT NULL,
  PRIMARY KEY (task_id, participant_id)
);

CREATE TABLE events (
  id              TEXT PRIMARY KEY,
  task_id         TEXT NOT NULL,
  seq             INTEGER NOT NULL CHECK (seq > 0),
  event_type      TEXT NOT NULL,
  from_id         TEXT,
  in_reply_to     TEXT,
  client_event_id TEXT,
  created_at      TEXT NOT NULL,
  body            TEXT NOT NULL
);

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


async def _seed_legacy_db(path: Path, *, with_extref_row: bool = True) -> tuple[uuid4, uuid4] | None:
    """Create the legacy DB and (optionally) insert one task row carrying
    an `external_ref`. Returns `(initiator_id, task_id)` of the inserted
    row, or `None` if `with_extref_row=False`."""
    conn = await aiosqlite.connect(path, isolation_level=None)
    try:
        await conn.executescript(_LEGACY_SCHEMA)
        if not with_extref_row:
            return None
        now = datetime.now(tz=timezone.utc).isoformat()
        task_id = uuid4()
        initiator_id = uuid4()
        await conn.execute(
            "INSERT INTO tasks (id, initiator_id, parent_task_id, subject, "
            "external_ref, metadata, created_at, updated_at, last_activity_at) "
            "VALUES (?, ?, NULL, '', ?, '{}', ?, ?, ?)",
            (str(task_id), str(initiator_id), "thread-legacy", now, now, now),
        )
        return initiator_id, task_id
    finally:
        await conn.close()


async def test_migrate_adds_status_column(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    await _seed_legacy_db(db)

    async with SqliteAdapter(db) as adapter:
        async with adapter.conn.execute("PRAGMA table_info(tasks)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        assert "status" in cols


async def test_legacy_row_defaults_to_open(tmp_path: Path) -> None:
    """Rows inserted under the legacy schema must come back as `open`
    after migration — `DEFAULT 'open'` on the `ALTER TABLE ADD COLUMN`
    applies to existing rows."""
    db = tmp_path / "legacy.db"
    seeded = await _seed_legacy_db(db)
    assert seeded is not None
    _initiator, legacy_task_id = seeded

    async with SqliteAdapter(db) as adapter:
        got = await adapter.get_task(legacy_task_id)
        assert got is not None
        assert got.status == "open"


async def test_legacy_index_is_swapped_for_partial(tmp_path: Path) -> None:
    """The new partial index predicate must include `status = 'open'`.
    We assert on the index DDL recorded in `sqlite_master`."""
    db = tmp_path / "legacy.db"
    await _seed_legacy_db(db)

    async with SqliteAdapter(db) as adapter:
        async with adapter.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='ux_tasks_extref'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        sql = row["sql"] or ""
        assert "status = 'open'" in sql or "status='open'" in sql, sql


async def test_close_and_re_create_works_on_migrated_db(tmp_path: Path) -> None:
    """The full slot-release round-trip survives migration. Close the
    legacy task (which has an `external_ref`), then create a new task
    with the same ref — the partial unique index must allow it because
    the legacy row is now `status='closed'` and falls out of the index."""
    db = tmp_path / "legacy.db"
    seeded = await _seed_legacy_db(db)
    assert seeded is not None
    initiator_id, legacy_task_id = seeded

    async with SqliteAdapter(db) as adapter:
        # Sanity: the legacy task is bound to the slot under the new index.
        assert (
            await adapter.get_task_by_external_ref(initiator_id, "thread-legacy")
            == legacy_task_id
        )

        # Close it.
        async with adapter.lock(legacy_task_id):
            closed = await adapter.close_task(legacy_task_id)
        assert closed.status == "closed"

        # Slot is now free.
        assert (
            await adapter.get_task_by_external_ref(initiator_id, "thread-legacy")
            is None
        )

        # New task with the same ref can be persisted — the partial index
        # excludes the closed row, so no unique-constraint violation.
        new_task = Task(
            initiator_id=initiator_id,
            participants=[initiator_id],
            external_ref="thread-legacy",
        )
        await adapter.create_task(new_task, external_ref="thread-legacy")
        assert (
            await adapter.get_task_by_external_ref(initiator_id, "thread-legacy")
            == new_task.id
        )


async def test_migration_is_idempotent_on_current_schema(tmp_path: Path) -> None:
    """Open a fresh DB (current schema), close, re-open. The second
    `__aenter__` must not error — `_migrate` skips the ALTER and the
    `CREATE INDEX IF NOT EXISTS` is naturally idempotent."""
    db = tmp_path / "current.db"
    async with SqliteAdapter(db) as adapter:
        t = Task(initiator_id=uuid4(), participants=[uuid4()])
        await adapter.create_task(t)

    async with SqliteAdapter(db) as adapter2:
        got = await adapter2.get_task(t.id)
        assert got is not None
        assert got.id == t.id
        assert got.status == "open"
