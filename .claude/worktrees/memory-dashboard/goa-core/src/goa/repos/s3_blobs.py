"""S3-compatible `BlobStore` adapter.

One implementation, many backends — the same code talks to **Supabase
Storage**, AWS S3, Cloudflare R2, Backblaze B2, or a local MinIO via
the standard S3 API. The endpoint URL is the only thing that changes.

This adapter stores bytes in an S3 bucket and **metadata** (filename,
mime_type, size, sha256, task binding) in a Postgres `blobs` table
owned by `PostgresAdapter`. The split keeps `get_meta` / `get_task_id`
as fast indexed reads and frees the database from carrying multi-GB
attachments.

Streaming:
- Uploads ≤ `_MULTIPART_THRESHOLD` go through a single `put_object`
  call. Buffered in memory but bounded by `BLOB_MAX_BYTES`.
- Uploads larger than the threshold use S3 multipart upload, which
  consumes the input async iterator in `_PART_SIZE` chunks — no
  whole-blob-into-memory.
- `open()` returns the response body's chunk iterator unchanged —
  true streaming download.

Mid-stream size check: `put` enforces `max_bytes` while consuming
the input iterator, raising `BlobTooLarge` before the body is
finalized. On failure during multipart upload the in-flight multipart
is aborted; on Postgres metadata-row failure the freshly-uploaded
object is best-effort deleted so we don't leak orphans (orphan
objects are not catastrophic — they are just storage cost — but
deleting on the unhappy path keeps the bucket clean).

Path-style addressing is **required** by Supabase Storage's S3
endpoint; this is also the safe default for MinIO. AWS S3 supports
both styles, so path-style here is a no-op there.

Auth: Supabase's "S3 access keys" are scoped server-side credentials
that bypass RLS. They are the right credentials for a backend service
that owns its own authz layer (the Goa hub does — see protocols.py).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from goa.domain.models import Attachment
from goa.errors import BlobTooLarge
from goa.repos.postgres import PostgresAdapter


_logger = logging.getLogger(__name__)

# S3 minimum part size for multipart upload is 5 MiB (5 * 1024 * 1024)
# for all parts except the last. The threshold below is what triggers
# the multipart code path in the first place — pick something that
# leaves room for a buffered single-part path while staying under most
# providers' "max single PUT" recommendation.
_PART_SIZE = 8 * 1024 * 1024  # 8 MiB per part
_MULTIPART_THRESHOLD = 5 * 1024 * 1024  # 5 MiB → switch to multipart


class S3BlobStore:
    """`BlobStore` against any S3-compatible endpoint.

    Holds a reference to the `PostgresAdapter` whose pool stores blob
    metadata. Both stores share one Postgres connection pool — the
    `Persistence` bundle's identity-dedup on `__aenter__` keeps the
    pool open exactly once.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        region: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        metadata_adapter: PostgresAdapter,
    ) -> None:
        self._endpoint_url = endpoint_url
        self._region = region
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._metadata_adapter = metadata_adapter
        self._exit_stack: AsyncExitStack | None = None
        self._client: Any | None = None  # aiobotocore S3 client

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "S3BlobStore":
        session = aioboto3.Session()
        stack = AsyncExitStack()
        # Path-style addressing is required by Supabase Storage and
        # works on every other S3-compatible endpoint. Virtual-hosted
        # style would break the Supabase URL shape.
        client = await stack.enter_async_context(
            session.client(
                "s3",
                endpoint_url=self._endpoint_url,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                region_name=self._region,
                config=Config(
                    s3={"addressing_style": "path"},
                    signature_version="s3v4",
                ),
            )
        )
        self._client = client
        self._exit_stack = stack
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None
        self._client = None

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RuntimeError(
                "S3BlobStore is not entered — wrap in `async with` (or rely "
                "on FastAPI lifespan via create_app)."
            )
        return self._client

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
        object_key = f"{task_id}/{blob_id}"
        hasher = hashlib.sha256()

        # Buffer until the threshold; if we never exceed it, do a single
        # `put_object`. If we cross it mid-stream, switch to multipart
        # and flush the buffered prefix as the first part.
        buffer = bytearray()
        total = 0
        multipart_upload_id: str | None = None
        parts: list[dict] = []
        part_number = 0
        # Accumulator for the current outgoing part (only used when we
        # are in multipart mode).
        part_buf = bytearray()

        try:
            async for chunk in stream:
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    if multipart_upload_id is not None:
                        await self._abort_multipart(object_key, multipart_upload_id)
                    raise BlobTooLarge(
                        f"upload exceeds the {max_bytes}-byte limit"
                    )
                hasher.update(chunk)

                if multipart_upload_id is None:
                    buffer.extend(chunk)
                    if len(buffer) > _MULTIPART_THRESHOLD:
                        # Promote: start a multipart upload, flush
                        # buffered bytes into the first part buffer.
                        multipart_upload_id = await self._start_multipart(
                            object_key, mime_type,
                        )
                        part_buf.extend(buffer)
                        buffer.clear()
                else:
                    part_buf.extend(chunk)

                # In multipart mode, flush full _PART_SIZE chunks as
                # parts so we keep memory bounded to ~one part.
                while (
                    multipart_upload_id is not None
                    and len(part_buf) >= _PART_SIZE
                ):
                    part_number += 1
                    head = bytes(part_buf[:_PART_SIZE])
                    del part_buf[:_PART_SIZE]
                    etag = await self._upload_part(
                        object_key, multipart_upload_id, part_number, head,
                    )
                    parts.append({"PartNumber": part_number, "ETag": etag})

            sha256_hex = hasher.hexdigest()

            if multipart_upload_id is None:
                # Single-part: under the threshold, `buffer` holds the
                # whole body (possibly empty).
                await self._put_object_single(
                    object_key, bytes(buffer), mime_type, sha256_hex,
                )
            else:
                # Flush the final (short) part if there is one. S3
                # allows the last part to be smaller than the minimum
                # part size.
                if part_buf:
                    part_number += 1
                    etag = await self._upload_part(
                        object_key,
                        multipart_upload_id,
                        part_number,
                        bytes(part_buf),
                    )
                    parts.append({"PartNumber": part_number, "ETag": etag})
                    part_buf.clear()
                await self._complete_multipart(
                    object_key, multipart_upload_id, parts,
                )
        except BaseException:
            # Anything other than the BlobTooLarge path: clean up the
            # in-flight multipart so we don't leave it hanging.
            if multipart_upload_id is not None:
                await self._abort_multipart(object_key, multipart_upload_id)
            raise

        attachment = Attachment(
            blob_id=blob_id,
            filename=filename,
            mime_type=mime_type,
            size_bytes=total,
            sha256=sha256_hex,
        )

        # Persist the metadata row last. If this fails, the freshly
        # uploaded S3 object becomes an orphan — we best-effort delete
        # it so the bucket stays tidy. A leftover orphan is not
        # catastrophic (storage cost only); we never expose it.
        try:
            await self._insert_metadata(
                blob_id=blob_id,
                task_id=task_id,
                owner_id=owner_id,
                filename=filename,
                mime_type=mime_type,
                size_bytes=total,
                sha256=sha256_hex,
                object_key=object_key,
            )
        except BaseException:
            try:
                await self.client.delete_object(
                    Bucket=self._bucket, Key=object_key,
                )
            except Exception:  # noqa: BLE001 — log-and-swallow on cleanup
                _logger.exception(
                    "S3BlobStore: failed to delete orphan object %r after "
                    "metadata insert failure", object_key,
                )
            raise

        return attachment

    async def get_meta(self, blob_id: UUID) -> Attachment | None:
        async with self._metadata_adapter.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT filename, mime_type, size_bytes, sha256 "
                "FROM blobs WHERE id = $1",
                blob_id,
            )
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
        async with self._metadata_adapter.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT task_id FROM blobs WHERE id = $1", blob_id,
            )
        return row["task_id"] if row else None

    async def open(self, blob_id: UUID) -> AsyncIterator[bytes]:
        # Resolve the object_key from the metadata row. An unknown
        # blob_id returns an empty stream — same contract as the
        # SQLite adapter at sqlite.py:725-735.
        async with self._metadata_adapter.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT object_key FROM blobs WHERE id = $1", blob_id,
            )
        if row is None:
            return
        try:
            response = await self.client.get_object(
                Bucket=self._bucket, Key=row["object_key"],
            )
        except ClientError as e:
            # Race: metadata row exists but object is gone (e.g. lifecycle
            # policy fired, manual deletion). Treat as missing — caller
            # expects an empty iterator for unknown blobs.
            code = e.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                return
            raise
        async with response["Body"] as body:
            # aioboto3's response wraps an aiohttp ClientResponse — the
            # StreamReader hangs off `.content`. 64 KiB matches the SQLite
            # adapter's read-side chunk size.
            async for chunk in body.content.iter_chunked(64 * 1024):
                yield chunk

    # ------------------------------------------------------------------
    # S3 plumbing
    # ------------------------------------------------------------------

    async def _put_object_single(
        self, key: str, body: bytes, mime_type: str, sha256_hex: str,
    ) -> None:
        await self.client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType=mime_type,
            # `Metadata` is user-defined object metadata — we mirror the
            # sha256 here so an audit pass on the bucket alone can verify
            # the hash without consulting the database.
            Metadata={"sha256": sha256_hex},
        )

    async def _start_multipart(self, key: str, mime_type: str) -> str:
        resp = await self.client.create_multipart_upload(
            Bucket=self._bucket, Key=key, ContentType=mime_type,
        )
        return resp["UploadId"]

    async def _upload_part(
        self, key: str, upload_id: str, part_number: int, body: bytes,
    ) -> str:
        resp = await self.client.upload_part(
            Bucket=self._bucket,
            Key=key,
            PartNumber=part_number,
            UploadId=upload_id,
            Body=body,
        )
        return resp["ETag"]

    async def _complete_multipart(
        self, key: str, upload_id: str, parts: list[dict],
    ) -> None:
        await self.client.complete_multipart_upload(
            Bucket=self._bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )

    async def _abort_multipart(self, key: str, upload_id: str) -> None:
        try:
            await self.client.abort_multipart_upload(
                Bucket=self._bucket, Key=key, UploadId=upload_id,
            )
        except Exception:  # noqa: BLE001
            _logger.exception(
                "S3BlobStore: failed to abort multipart upload for %r", key,
            )

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    async def _insert_metadata(
        self,
        *,
        blob_id: UUID,
        task_id: UUID,
        owner_id: UUID,
        filename: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        object_key: str,
    ) -> None:
        async with self._metadata_adapter.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO blobs (
                  id, task_id, owner_id, filename, mime_type,
                  size_bytes, sha256, created_at, object_key
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                blob_id,
                task_id,
                owner_id,
                filename,
                mime_type,
                size_bytes,
                sha256,
                datetime.now(tz=timezone.utc),
                object_key,
            )
