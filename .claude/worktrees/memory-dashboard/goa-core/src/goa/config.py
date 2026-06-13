from __future__ import annotations

import os
from dataclasses import dataclass


_DEFAULT_BLOB_MAX_BYTES = 100 * 1024 * 1024  # 100 MB

# Per-entry JSON-encoded value size cap and per-owner entry-count cap for
# agent memory. Generous defaults — memory holds facts/small docs, not blobs.
_DEFAULT_MEMORY_MAX_ENTRY_BYTES = 64 * 1024  # 64 KB
_DEFAULT_MEMORY_MAX_ENTRIES_PER_OWNER = 10_000

# Sentinel: blob bytes live inside whatever the database adapter provides
# (SQLite stores a BLOB column; in-memory keeps bytes in a dict). Choosing
# this with a Postgres `database_url` is a misconfiguration — Postgres has
# no blob storage of its own here.
BLOB_BACKEND_DB = "db"
# Bytes go to any S3-compatible endpoint (AWS S3, Cloudflare R2, MinIO,
# Backblaze B2, **Supabase Storage**). Metadata still lives in the database.
BLOB_BACKEND_S3 = "s3"
_VALID_BLOB_BACKENDS = (BLOB_BACKEND_DB, BLOB_BACKEND_S3)


@dataclass(frozen=True)
class Settings:
    server_pepper: str
    replay_buffer_size: int
    subscriber_queue_size: int
    ping_interval_seconds: float
    cors_origins: tuple[str, ...] = ()
    admin_token: str | None = None
    blob_max_bytes: int = _DEFAULT_BLOB_MAX_BYTES
    # `GOA_DATABASE_URL`. `None` → in-memory. `sqlite:<path>` → SQLite adapter
    # at `<path>` (e.g. `sqlite:./goa.db`). `postgresql://…` or `postgres://…`
    # → PostgresAdapter (Supabase Postgres uses this scheme). Resolved into a
    # concrete `Persistence` bundle by `Persistence.from_settings(...)`.
    database_url: str | None = None
    # Blob storage backend selector. `"db"` (default) means the database
    # adapter handles blobs. `"s3"` means an S3-compatible endpoint owns the
    # bytes; the database still owns the metadata row.
    blob_backend: str = BLOB_BACKEND_DB
    blob_endpoint: str | None = None
    blob_bucket: str | None = None
    blob_region: str | None = None
    blob_access_key: str | None = None
    blob_secret_key: str | None = None
    # Agent memory caps (§ memory). `max_entry_bytes` bounds the JSON-encoded
    # value of a single entry; `max_entries_per_owner` bounds how many keys a
    # participant may hold. Both are passed into `MemoryStore.put_memory(...)`.
    memory_max_entry_bytes: int = _DEFAULT_MEMORY_MAX_ENTRY_BYTES
    memory_max_entries_per_owner: int = _DEFAULT_MEMORY_MAX_ENTRIES_PER_OWNER

    @classmethod
    def from_env(cls) -> "Settings":
        pepper = os.environ.get("GOA_SERVER_PEPPER")
        if not pepper:
            raise RuntimeError(
                "GOA_SERVER_PEPPER must be set. For tests, use Settings(server_pepper='test', ...)."
            )
        origins_raw = os.environ.get("GOA_CORS_ORIGINS", "http://localhost:5173")
        cors_origins = tuple(o.strip() for o in origins_raw.split(",") if o.strip())
        admin_token = os.environ.get("GOA_ADMIN_TOKEN") or None
        blob_backend = os.environ.get("GOA_BLOB_BACKEND", BLOB_BACKEND_DB)
        if blob_backend not in _VALID_BLOB_BACKENDS:
            raise RuntimeError(
                f"GOA_BLOB_BACKEND={blob_backend!r} is not one of {_VALID_BLOB_BACKENDS}."
            )
        blob_endpoint = os.environ.get("GOA_BLOB_ENDPOINT") or None
        blob_bucket = os.environ.get("GOA_BLOB_BUCKET") or None
        blob_region = os.environ.get("GOA_BLOB_REGION") or None
        blob_access_key = os.environ.get("GOA_BLOB_ACCESS_KEY") or None
        blob_secret_key = os.environ.get("GOA_BLOB_SECRET_KEY") or None
        if blob_backend == BLOB_BACKEND_S3:
            missing = [
                name
                for name, val in (
                    ("GOA_BLOB_ENDPOINT", blob_endpoint),
                    ("GOA_BLOB_BUCKET", blob_bucket),
                    ("GOA_BLOB_REGION", blob_region),
                    ("GOA_BLOB_ACCESS_KEY", blob_access_key),
                    ("GOA_BLOB_SECRET_KEY", blob_secret_key),
                )
                if not val
            ]
            if missing:
                raise RuntimeError(
                    "GOA_BLOB_BACKEND=s3 requires: " + ", ".join(missing)
                )
        return cls(
            server_pepper=pepper,
            replay_buffer_size=int(os.environ.get("GOA_REPLAY_BUFFER_SIZE", "1000")),
            subscriber_queue_size=int(os.environ.get("GOA_SUBSCRIBER_QUEUE_SIZE", "100")),
            ping_interval_seconds=float(os.environ.get("GOA_PING_INTERVAL_SECONDS", "20")),
            cors_origins=cors_origins,
            admin_token=admin_token,
            blob_max_bytes=int(
                os.environ.get("GOA_BLOB_MAX_BYTES", str(_DEFAULT_BLOB_MAX_BYTES))
            ),
            memory_max_entry_bytes=int(
                os.environ.get(
                    "GOA_MEMORY_MAX_ENTRY_BYTES", str(_DEFAULT_MEMORY_MAX_ENTRY_BYTES)
                )
            ),
            memory_max_entries_per_owner=int(
                os.environ.get(
                    "GOA_MEMORY_MAX_ENTRIES_PER_OWNER",
                    str(_DEFAULT_MEMORY_MAX_ENTRIES_PER_OWNER),
                )
            ),
            database_url=os.environ.get("GOA_DATABASE_URL") or None,
            blob_backend=blob_backend,
            blob_endpoint=blob_endpoint,
            blob_bucket=blob_bucket,
            blob_region=blob_region,
            blob_access_key=blob_access_key,
            blob_secret_key=blob_secret_key,
        )

    @classmethod
    def for_tests(
        cls,
        *,
        admin_token: str | None = None,
        blob_max_bytes: int = _DEFAULT_BLOB_MAX_BYTES,
    ) -> "Settings":
        return cls(
            server_pepper="test-pepper",
            replay_buffer_size=1000,
            subscriber_queue_size=100,
            ping_interval_seconds=20.0,
            admin_token=admin_token,
            blob_max_bytes=blob_max_bytes,
        )
