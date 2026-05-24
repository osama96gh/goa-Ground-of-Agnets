"""Shared testcontainer factories for Postgres + S3/MinIO contract tests.

Each `*_factory(tmp_path)` returns an `AsyncContextManager` that the
parametrized fixtures in `test_*_contract.py` enter for one test. The
containers themselves are **session-scoped singletons** behind module
globals — spinning up Postgres / MinIO once per test would be too slow.
Per-test isolation is achieved by:

- Creating a fresh Postgres schema (a UUID-named database, dropped on
  exit) per `postgres_*_factory(...)` call.
- Creating a fresh bucket per `s3_blob_store_factory(...)` call.

If `testcontainers` (and Docker) are unavailable, the factories return
a fixture that calls `pytest.skip(...)` on entry — the test is reported
as skipped, not failed.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path

import pytest

from goa.repos.protocols import BlobStore, ParticipantStore, TaskLog


# ---------------------------------------------------------------------------
# Lazy imports — testcontainers + Docker are optional. The tests skip
# rather than fail when these are missing, so we import inside helpers.
# ---------------------------------------------------------------------------


def _have_docker() -> bool:
    # The `testcontainers` package can be installed without Docker
    # actually being available. Probing `docker info` is the canonical
    # cheap check, but `testcontainers` itself fails late with a
    # `DockerException` — we let that bubble through `pytest.skip` in
    # the factory.
    if os.environ.get("GOA_SKIP_TESTCONTAINERS"):
        return False
    return True


_pg_container = None
_pg_dsn: str | None = None
_minio_container = None
_minio_endpoint: str | None = None
_minio_access_key: str | None = None
_minio_secret_key: str | None = None


def _get_pg_dsn() -> str:
    """Boot a single Postgres container per pytest session; return its DSN."""
    global _pg_container, _pg_dsn
    if _pg_dsn is not None:
        return _pg_dsn
    if not _have_docker():
        pytest.skip("testcontainers disabled via GOA_SKIP_TESTCONTAINERS")
    try:
        import asyncpg  # noqa: F401  — also a hard requirement for the factory
        from testcontainers.postgres import PostgresContainer
    except ImportError as e:
        pytest.skip(f"Postgres testcontainer deps missing: {e!r}")
    try:
        # `postgres:16-alpine` is small and fast. `with_driver=False`
        # gives us a raw DSN that asyncpg accepts.
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as e:  # noqa: BLE001 — broad on purpose
        pytest.skip(f"Postgres testcontainer unavailable: {e!r}")
    _pg_container = container
    # `get_connection_url()` returns a SQLAlchemy-style URL with the
    # `postgresql+psycopg2://` driver prefix. Strip the driver suffix
    # so asyncpg accepts it.
    raw = container.get_connection_url()
    _pg_dsn = raw.replace("postgresql+psycopg2://", "postgresql://")
    return _pg_dsn


def _get_minio() -> tuple[str, str, str]:
    """Boot a single MinIO container per pytest session; return (endpoint, key, secret)."""
    global _minio_container, _minio_endpoint, _minio_access_key, _minio_secret_key
    if _minio_endpoint is not None:
        assert _minio_access_key is not None and _minio_secret_key is not None
        return _minio_endpoint, _minio_access_key, _minio_secret_key
    if not _have_docker():
        pytest.skip("testcontainers disabled via GOA_SKIP_TESTCONTAINERS")
    try:
        from testcontainers.minio import MinioContainer
    except ImportError:
        pytest.skip("testcontainers[minio] not installed")
    try:
        container = MinioContainer()
        container.start()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"MinIO testcontainer unavailable: {e!r}")
    _minio_container = container
    config = container.get_config()
    _minio_endpoint = f"http://{config['endpoint']}"
    _minio_access_key = config["access_key"]
    _minio_secret_key = config["secret_key"]
    return _minio_endpoint, _minio_access_key, _minio_secret_key


async def _per_test_dsn() -> tuple[str, str]:
    """Create a fresh database in the session-scoped Postgres container.
    Returns (dsn, db_name). Caller drops the db on exit."""
    import asyncpg

    base_dsn = _get_pg_dsn()
    db_name = f"goa_test_{uuid.uuid4().hex[:12]}"
    # Connect to the default `postgres` db just to issue the CREATE.
    admin = await asyncpg.connect(base_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin.close()
    # Rewrite the path component of the base DSN to point at the new db.
    # `base_dsn` ends in `/test` (the default DB name for PostgresContainer);
    # the substring-find approach is robust to query strings.
    scheme_and_rest = base_dsn.split("://", 1)
    rest = scheme_and_rest[1]
    # Split off any leading `user:pw@host:port/`. We keep everything up
    # through the last `/` and append the new db name.
    host_part, _, _ = rest.rpartition("/")
    fresh_dsn = f"{scheme_and_rest[0]}://{host_part}/{db_name}"
    return fresh_dsn, db_name


async def _drop_database(db_name: str) -> None:
    import asyncpg

    base_dsn = _get_pg_dsn()
    admin = await asyncpg.connect(base_dsn)
    try:
        # `WITH (FORCE)` evicts any leftover connections (asyncpg pool may
        # not have released its sockets immediately).
        await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    finally:
        await admin.close()


# ---------------------------------------------------------------------------
# TaskLog + ParticipantStore factories — both yield a PostgresAdapter
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _postgres_adapter_cm(_tmp: Path) -> AsyncIterator:
    from goa.repos.postgres import PostgresAdapter

    dsn, db_name = await _per_test_dsn()
    adapter = PostgresAdapter(dsn)
    try:
        async with adapter:
            yield adapter
    finally:
        await _drop_database(db_name)


def postgres_task_log_factory(tmp: Path) -> AbstractAsyncContextManager[TaskLog]:
    return _postgres_adapter_cm(tmp)


def postgres_participant_store_factory(
    tmp: Path,
) -> AbstractAsyncContextManager[ParticipantStore]:
    return _postgres_adapter_cm(tmp)


# ---------------------------------------------------------------------------
# S3 BlobStore factory — boots Postgres + MinIO, creates a fresh bucket
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _s3_blob_store_cm(_tmp: Path) -> AsyncIterator:
    try:
        import aioboto3
        from botocore.config import Config
    except ImportError:
        pytest.skip("aioboto3 not installed — install the [s3] extra")
    try:
        from goa.repos.s3_blobs import S3BlobStore
    except ImportError as e:
        pytest.skip(f"S3BlobStore import failed: {e!r}")
    from goa.repos.postgres import PostgresAdapter

    endpoint, access_key, secret_key = _get_minio()
    bucket = f"goa-test-{uuid.uuid4().hex[:12]}"
    region = "us-east-1"  # MinIO accepts any region; this is the conventional default.

    session = aioboto3.Session()
    async with session.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    ) as bootstrap:
        await bootstrap.create_bucket(Bucket=bucket)

    dsn, db_name = await _per_test_dsn()
    pg = PostgresAdapter(dsn)
    blobs = S3BlobStore(
        endpoint_url=endpoint,
        region=region,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        metadata_adapter=pg,
    )
    try:
        async with pg:
            async with blobs:
                yield blobs
    finally:
        await _drop_database(db_name)


def s3_blob_store_factory(tmp: Path) -> AbstractAsyncContextManager[BlobStore]:
    return _s3_blob_store_cm(tmp)
