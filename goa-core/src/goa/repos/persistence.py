"""The `Persistence` bundle — single injection point for the three
persistence Protocols.

Collapses what used to be three independent kwargs on `create_app(...)`
into one object the consumer constructs in full. Eliminates the
partial-wiring footgun where one backend goes to Postgres but the others
silently stay in-memory and lose state on restart.

Internally the three Protocols stay separate (see
[goa.repos.protocols](protocols.py)) — different concerns, different
hot paths, different natural backends. The bundle is purely the wiring
boundary.

`Persistence` itself is an `AsyncContextManager`: `__aenter__` opens
every contained store that implements the CM protocol (persistent
adapters that own a connection/pool); in-memory stores have no
resources and are skipped silently. FastAPI's `lifespan` enters the
bundle on startup and exits it on shutdown.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from goa.repos.memory import (
    InMemoryBlobStore,
    InMemoryParticipantStore,
    InMemoryTaskLog,
)
from goa.repos.protocols import BlobStore, ParticipantStore, TaskLog

if TYPE_CHECKING:
    from goa.config import Settings


@dataclass
class Persistence:
    """Bundle of the three persistence Protocols passed as
    `create_app(persistence=…)`.

    Use `dataclasses.replace(...)` to swap one Protocol for testing while
    keeping the rest in-memory:

        >>> from dataclasses import replace
        >>> p = replace(Persistence.in_memory(), task_log=MyRecorder())
        >>> app = create_app(persistence=p)
    """

    task_log: TaskLog
    participant_store: ParticipantStore
    blob_store: BlobStore
    # `init=False` is load-bearing: it keeps `_entered` out of
    # `dataclasses.replace(...)`, so a replaced bundle gets a fresh
    # list rather than sharing the original's enter-tracking state.
    _entered: list[AbstractAsyncContextManager] = field(
        default_factory=list, init=False, repr=False, compare=False,
    )

    @classmethod
    def in_memory(cls) -> "Persistence":
        """Zero-config dev/test default. Same backends `create_app()` uses
        when no `persistence=` arg is supplied."""
        return cls(
            task_log=InMemoryTaskLog(),
            participant_store=InMemoryParticipantStore(),
            blob_store=InMemoryBlobStore(),
        )

    @classmethod
    def sqlite(cls, path: str | Path) -> "Persistence":
        """SQLite-backed persistence at `path`. Single-replica only —
        running multiple hubs against the same file is unsupported and
        will produce loud `IntegrityError`s rather than silent divergence,
        but the right answer is one hub per file.

        The connection is opened lazily inside `__aenter__` (FastAPI
        lifespan), not by this constructor — so `Persistence.sqlite(...)`
        is cheap to call from `create_app` even before the app starts."""
        # Lazy import: the sqlite module ships with the adapter (Stage 1,
        # step 4) and pulls in `aiosqlite`. Importing here keeps the
        # in-memory path free of that dependency.
        from goa.repos.sqlite import SqliteAdapter

        # One adapter satisfies all three Protocols and owns the single
        # connection. `Persistence.__aenter__` dedupes by identity so the
        # adapter is opened exactly once.
        adapter = SqliteAdapter(Path(path))
        return cls(
            task_log=adapter,
            participant_store=adapter,
            blob_store=adapter,
        )

    @classmethod
    def postgres(
        cls,
        database_url: str,
        *,
        blob_store: BlobStore,
    ) -> "Persistence":
        """Postgres-backed `TaskLog` + `ParticipantStore`. The caller
        supplies the `BlobStore` — Postgres has no blob bytes of its own
        in this design (bytes go to S3-compatible storage; metadata rows
        live in the Postgres `blobs` table, owned by `S3BlobStore`).

        Use `Persistence.supabase(...)` for the common "Postgres + S3
        storage" composition with one set of credentials. Use this
        constructor directly when you want a non-S3 `BlobStore` (e.g. a
        future filesystem adapter) paired with Postgres.
        """
        # Lazy import — `asyncpg` only loads when this code path runs.
        from goa.repos.postgres import PostgresAdapter

        adapter = PostgresAdapter(database_url)
        return cls(
            task_log=adapter,
            participant_store=adapter,
            blob_store=blob_store,
        )

    @classmethod
    def supabase(
        cls,
        *,
        database_url: str,
        storage_endpoint: str,
        storage_bucket: str,
        storage_region: str,
        storage_access_key: str,
        storage_secret_key: str,
    ) -> "Persistence":
        """Full Supabase persistence bundle in one call.

        Composes a `PostgresAdapter` (against Supabase Postgres on the
        **direct connection**, port 5432) with an `S3BlobStore` (against
        Supabase Storage's S3-compatible endpoint). Both adapters share
        the Postgres connection pool for blob metadata; the identity-
        dedup in `__aenter__` opens the pool exactly once.

        The same factory works against any non-Supabase Postgres + S3
        pair (RDS + AWS S3, Neon + R2, etc.) — Supabase is the easy
        paved path, not a hard dependency.
        """
        from goa.repos.postgres import PostgresAdapter
        from goa.repos.s3_blobs import S3BlobStore

        pg = PostgresAdapter(database_url)
        blobs = S3BlobStore(
            endpoint_url=storage_endpoint,
            region=storage_region,
            access_key=storage_access_key,
            secret_key=storage_secret_key,
            bucket=storage_bucket,
            metadata_adapter=pg,
        )
        return cls(task_log=pg, participant_store=pg, blob_store=blobs)

    @classmethod
    def from_settings(cls, settings: "Settings") -> "Persistence":
        """Resolve the bundle from `Settings.database_url` plus the
        `blob_*` settings group.

        - `None` → `Persistence.in_memory()` (zero-config dev/test default).
        - `sqlite:<path>` → `Persistence.sqlite(<path>)`. Blob bytes live
          in SQLite alongside metadata — `blob_backend` must stay at
          `"db"` (the default).
        - `postgresql://…` or `postgres://…` → `Persistence.postgres(...)`
          composed with the configured `BlobStore`:
            * `blob_backend="s3"` → `S3BlobStore` from the S3 settings.
              Pointing the endpoint at `*.storage.supabase.co` makes this
              the Supabase path; pointing it elsewhere makes it generic.

        Unknown schemes or incompatible combinations raise `ValueError`
        immediately so misconfiguration fails at startup, not on the
        first request.
        """
        url = settings.database_url
        if url is None:
            return cls.in_memory()
        if url.startswith("sqlite:"):
            return cls.sqlite(url[len("sqlite:") :])
        if url.startswith("postgres://") or url.startswith("postgresql://"):
            # Postgres always needs an external BlobStore (no blob bytes
            # live in Postgres in this design). For `blob_backend="s3"`
            # we go through `supabase(...)` since both code paths compose
            # the same way; for any other backend the caller must build
            # the bundle manually.
            from goa.config import BLOB_BACKEND_S3

            if settings.blob_backend == BLOB_BACKEND_S3:
                # `Settings.from_env()` already enforces all five S3
                # fields are present when `blob_backend == "s3"`. The
                # asserts below are belt-and-braces for callers that
                # built `Settings` by hand.
                assert settings.blob_endpoint is not None
                assert settings.blob_bucket is not None
                assert settings.blob_region is not None
                assert settings.blob_access_key is not None
                assert settings.blob_secret_key is not None
                return cls.supabase(
                    database_url=url,
                    storage_endpoint=settings.blob_endpoint,
                    storage_bucket=settings.blob_bucket,
                    storage_region=settings.blob_region,
                    storage_access_key=settings.blob_access_key,
                    storage_secret_key=settings.blob_secret_key,
                )
            raise ValueError(
                f"GOA_DATABASE_URL={url!r} requires GOA_BLOB_BACKEND=s3 "
                "(Postgres holds no blob bytes; bytes must go to an "
                "S3-compatible store). Supported blob backends with "
                f"Postgres: 's3'. Got: {settings.blob_backend!r}."
            )
        raise ValueError(
            f"Unsupported GOA_DATABASE_URL scheme: {url!r}. "
            "Supported: unset (in-memory), 'sqlite:<path>', "
            "'postgresql://…', 'postgres://…'."
        )

    async def __aenter__(self) -> "Persistence":
        # Fan-out: each store that implements the AsyncCM protocol opens
        # its resources here. In-memory stores skip silently (no resources
        # to open) — the zero-config path stays zero-config.
        #
        # Dedupe by identity: one adapter object that satisfies all three
        # Protocols (e.g. `SqliteAdapter`) is held in all three slots but
        # should be entered exactly once.
        seen: set[int] = set()
        for store in (self.task_log, self.participant_store, self.blob_store):
            key = id(store)
            if key in seen:
                continue
            seen.add(key)
            if isinstance(store, AbstractAsyncContextManager):
                try:
                    await store.__aenter__()
                except BaseException:
                    # Partial failure: tear down whatever we already entered.
                    await self._teardown(None, None, None)
                    raise
                self._entered.append(store)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self._teardown(exc_type, exc, tb)

    async def _teardown(self, exc_type: Any, exc: Any, tb: Any) -> None:
        # LIFO to mirror nested `async with` semantics; suppress nothing.
        while self._entered:
            store = self._entered.pop()
            await store.__aexit__(exc_type, exc, tb)
