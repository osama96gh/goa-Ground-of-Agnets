"""Protocol-level conformance suite for `BlobStore` impls.

Parametrized across the in-memory and SQLite backends. The SQLite case
proves what in-memory can't: the blob → task binding survives a process
restart (we reopen the adapter against the same file and assert the
binding still resolves).

Invariants covered:

1. `put` streams bytes, returns an `Attachment` with correct `sha256`
   and `size_bytes`.
2. `open` reproduces the exact byte stream.
3. `get_meta` / `get_task_id` find the row; both return `None` for
   unknown ids.
4. `get_task_id` survives an adapter reopen against the same file
   (SQLite case only — in-memory restart wipes state by design).
5. A stream exceeding `max_bytes` raises `BlobTooLarge` **mid-stream**
   (the test feeds chunks that, if fully consumed, would overflow);
   no row is inserted afterward.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from goa.errors import BlobTooLarge
from goa.repos.memory import InMemoryBlobStore
from goa.repos.protocols import BlobStore
from goa.repos.sqlite import SqliteAdapter

from tests.unit._postgres_factory import s3_blob_store_factory


pytestmark = pytest.mark.asyncio


BlobStoreFactory = Callable[[Path], AbstractAsyncContextManager[BlobStore]]


@asynccontextmanager
async def _wrap_noop(store: BlobStore) -> AsyncIterator[BlobStore]:
    yield store


def _in_memory_factory(_tmp: Path) -> AbstractAsyncContextManager[BlobStore]:
    return _wrap_noop(InMemoryBlobStore())


def _sqlite_factory(tmp: Path) -> AbstractAsyncContextManager[BlobStore]:
    return SqliteAdapter(tmp / "goa.db")


BLOB_STORE_FACTORIES: list[BlobStoreFactory] = [
    _in_memory_factory,
    _sqlite_factory,
    s3_blob_store_factory,
]


@pytest_asyncio.fixture(params=BLOB_STORE_FACTORIES, ids=lambda f: f.__name__)
async def store(request, tmp_path: Path) -> AsyncIterator[BlobStore]:
    async with request.param(tmp_path) as s:
        yield s


async def _iter_chunks(*chunks: bytes) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c


# ----------------------------------------------------------------------
# put + get_meta + open round-trip
# ----------------------------------------------------------------------

async def test_put_returns_attachment_with_correct_hash_and_size(
    store: BlobStore,
) -> None:
    import hashlib

    payload = b"hello world"
    expected_sha = hashlib.sha256(payload).hexdigest()

    att = await store.put(
        task_id=uuid4(),
        owner_id=uuid4(),
        filename="hi.txt",
        mime_type="text/plain",
        stream=_iter_chunks(payload),
        max_bytes=1024,
    )
    assert att.filename == "hi.txt"
    assert att.mime_type == "text/plain"
    assert att.size_bytes == len(payload)
    assert att.sha256 == expected_sha


async def test_open_reproduces_stream(store: BlobStore) -> None:
    payload = b"chunk-1|chunk-2|chunk-3"
    att = await store.put(
        task_id=uuid4(),
        owner_id=uuid4(),
        filename="bin",
        mime_type="application/octet-stream",
        stream=_iter_chunks(b"chunk-1|", b"chunk-2|", b"chunk-3"),
        max_bytes=1024,
    )
    reassembled = b"".join([c async for c in store.open(att.blob_id)])
    assert reassembled == payload


async def test_get_meta_returns_attachment(store: BlobStore) -> None:
    att = await store.put(
        task_id=uuid4(),
        owner_id=uuid4(),
        filename="x.pdf",
        mime_type="application/pdf",
        stream=_iter_chunks(b"x"),
        max_bytes=1024,
    )
    got = await store.get_meta(att.blob_id)
    assert got is not None
    assert got.blob_id == att.blob_id
    assert got.filename == "x.pdf"
    assert got.mime_type == "application/pdf"
    assert got.sha256 == att.sha256


async def test_get_meta_returns_none_for_unknown(store: BlobStore) -> None:
    assert await store.get_meta(uuid4()) is None


async def test_get_task_id_returns_binding(store: BlobStore) -> None:
    bound_task = uuid4()
    att = await store.put(
        task_id=bound_task,
        owner_id=uuid4(),
        filename="x",
        mime_type="text/plain",
        stream=_iter_chunks(b"x"),
        max_bytes=1024,
    )
    assert await store.get_task_id(att.blob_id) == bound_task


async def test_get_task_id_returns_none_for_unknown(store: BlobStore) -> None:
    assert await store.get_task_id(uuid4()) is None


async def test_open_yields_nothing_for_unknown(store: BlobStore) -> None:
    chunks = [c async for c in store.open(uuid4())]
    assert chunks == []


# ----------------------------------------------------------------------
# Size-limit enforcement — must raise mid-stream
# ----------------------------------------------------------------------

async def test_oversize_upload_raises_blob_too_large_midstream(
    store: BlobStore,
) -> None:
    """The exception must fire before the entire stream is drained — feed
    a tripwire iterator whose later chunks would explode if reached."""

    async def tripwire() -> AsyncIterator[bytes]:
        yield b"a" * 600  # already over the 1024-byte limit when next chunk arrives
        yield b"b" * 600  # crosses the limit; raise here
        raise AssertionError("stream consumed past the size limit")

    with pytest.raises(BlobTooLarge):
        await store.put(
            task_id=uuid4(),
            owner_id=uuid4(),
            filename="big",
            mime_type="application/octet-stream",
            stream=tripwire(),
            max_bytes=1024,
        )


# ----------------------------------------------------------------------
# Persistence across adapter reopen — SQLite-only
# ----------------------------------------------------------------------

async def test_get_task_id_survives_adapter_reopen(tmp_path: Path) -> None:
    """Stage 1's headline guarantee for blobs: the binding outlives a
    process restart. In-memory cannot honor this (by design — restart
    wipes state); only the SQLite path is exercised here."""
    bound_task = uuid4()
    blob_id = None
    db_path = tmp_path / "goa.db"

    async with SqliteAdapter(db_path) as s1:
        att = await s1.put(
            task_id=bound_task,
            owner_id=uuid4(),
            filename="x",
            mime_type="text/plain",
            stream=_iter_chunks(b"persisted bytes"),
            max_bytes=1024,
        )
        blob_id = att.blob_id

    async with SqliteAdapter(db_path) as s2:
        assert await s2.get_task_id(blob_id) == bound_task
        # And the bytes too.
        reassembled = b"".join([c async for c in s2.open(blob_id)])
        assert reassembled == b"persisted bytes"
