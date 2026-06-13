"""Blob upload + download endpoints (spec §6.5).

`POST /tasks/{task_id}/blobs` accepts a multipart/form-data upload,
streams it into the configured `BlobStore`, and binds the blob to
`task_id` at upload time. The binding is immutable, cross-task reference
in events is forbidden (`BlobForbidden 403`), and authz on download is a
one-column read of the blob's bound `task_id`.

`GET /blobs/{blob_id}` streams the raw bytes back. Authorization: the
caller must be a participant of the blob's bound task (see
`TaskService.is_blob_visible`).

`GET /blobs/{blob_id}/meta` returns the `Attachment` metadata only — used
by clients (e.g. the dashboard) to decide whether to preview before paying
the download cost.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse

from goa.deps import AppContext, get_ctx, make_bearer_dependency
from goa.domain.models import Attachment, Participant
from goa.errors import (
    AUTH_401,
    VALIDATION_422,
    BlobForbidden,
    BlobNotFound,
    TaskNotFound,
    error_response,
)


router = APIRouter()
require_participant = make_bearer_dependency()


async def _stream_upload(upload: UploadFile, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        yield chunk


@router.post(
    "/tasks/{task_id}/blobs",
    status_code=201,
    response_model=Attachment,
    summary="Upload a blob",
    responses={
        **AUTH_401,
        404: error_response(
            "`task_not_found` — the task does not exist or you are not a participant."
        ),
        413: error_response(
            "`blob_too_large` — the upload exceeds the configured size limit."
        ),
        **VALIDATION_422,
    },
)
async def upload_blob(
    task_id: UUID,
    request: Request,
    file: UploadFile = File(...),
    filename: str | None = Form(default=None),
    caller: Participant = Depends(require_participant),
    ctx: AppContext = Depends(get_ctx),
) -> Attachment:
    """Upload a blob bound to `task_id`. The `file` field is required;
    `filename` overrides the filename derived from the upload part. The
    response is the `Attachment` record the caller embeds in subsequent
    `Content.attachments` on events in this same task. Cross-task reference
    is rejected at `append_event` time with `BlobForbidden 403`.

    404 (`task_not_found`) if the task does not exist or the caller is not
    a participant — same gating as the read-side `GET /tasks/{id}`."""
    task = await ctx.task_log.get_task(task_id)
    if task is None or caller.id not in task.participants:
        raise TaskNotFound()

    chosen_name = (filename or file.filename or f"upload-{file.size or 0}.bin").strip()
    if not chosen_name:
        chosen_name = "upload.bin"
    chosen_mime = file.content_type or "application/octet-stream"
    return await ctx.blob_store.put(
        task_id=task_id,
        owner_id=caller.id,
        filename=chosen_name,
        mime_type=chosen_mime,
        stream=_stream_upload(file),
        max_bytes=ctx.settings.blob_max_bytes,
    )


@router.get(
    "/blobs/{blob_id}/meta",
    response_model=Attachment,
    summary="Get blob metadata",
    responses={
        **AUTH_401,
        403: error_response(
            "`blob_forbidden` — you are not a participant of the blob's task."
        ),
        404: error_response("`blob_not_found` — no blob with that id."),
        **VALIDATION_422,
    },
)
async def get_blob_meta(
    blob_id: UUID,
    caller: Participant = Depends(require_participant),
    ctx: AppContext = Depends(get_ctx),
) -> Attachment:
    meta = await ctx.blob_store.get_meta(blob_id)
    if meta is None:
        raise BlobNotFound()
    if not await ctx.service.is_blob_visible(caller, blob_id):
        raise BlobForbidden()
    return meta


@router.get(
    "/blobs/{blob_id}",
    summary="Download a blob",
    responses={
        200: {
            "description": "The raw blob bytes.",
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        },
        **AUTH_401,
        403: error_response(
            "`blob_forbidden` — you are not a participant of the blob's task."
        ),
        404: error_response("`blob_not_found` — no blob with that id."),
        **VALIDATION_422,
    },
)
async def download_blob(
    blob_id: UUID,
    caller: Participant = Depends(require_participant),
    ctx: AppContext = Depends(get_ctx),
) -> StreamingResponse:
    meta = await ctx.blob_store.get_meta(blob_id)
    if meta is None:
        raise BlobNotFound()
    if not await ctx.service.is_blob_visible(caller, blob_id):
        raise BlobForbidden()
    # RFC 5987-style filename* with percent-encoding so non-ASCII names
    # survive the round-trip; fallback `filename=` for older clients.
    safe = quote(meta.filename, safe="")
    return StreamingResponse(
        ctx.blob_store.open(blob_id),
        media_type=meta.mime_type,
        headers={
            "Content-Disposition": f"attachment; filename=\"{meta.filename}\"; filename*=UTF-8''{safe}",
            "Content-Length": str(meta.size_bytes),
        },
    )
