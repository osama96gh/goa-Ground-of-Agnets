"""Admin firehose + admin-scoped read endpoints.

Auth: a single `GOA_ADMIN_TOKEN` shared deployment secret, distinct from
participant API keys (which are HMAC-hashed and scoped to a single
`participant_id`). Admin token is compared in constant time.

Writes still require participant keys — admins can read everything but
cannot post events as someone else, preserving the §6.3 invariant that
every event's `from` is the authenticated caller.

This router is included by `goa.main:create_app` only when
`Settings.admin_token` is set; otherwise the routes simply do not exist
and any request to `/admin/*` returns 404 from FastAPI's default router.
"""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import AsyncIterator
from typing import Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from goa.auth import _parse_bearer, generate_api_key, hash_api_key
from goa.deps import AppContext, get_ctx
from goa.domain.models import Attachment, Event, Participant, PendingPair, Task
from goa.errors import BlobNotFound, TaskNotFound, Unauthorized


router = APIRouter()


def _require_admin(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    token = _parse_bearer(authorization)
    expected = request.app.state.ctx.settings.admin_token
    # `expected is None` is checked at router registration; defensively recheck.
    if not expected or not hmac.compare_digest(token, expected):
        raise Unauthorized()


class TaskListItem(BaseModel):
    """Admin list-endpoint composite — pending is a derived view (Stages 2+3)."""

    model_config = ConfigDict(extra="forbid")

    task: Task
    pending_questions: list[PendingPair]


class ListTasksResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[TaskListItem]


class GetTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: Task
    pending_questions: list[PendingPair]
    events: list[Event]


class ListParticipantsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participants: list[Participant]


class AdminCreateParticipantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["agent", "service"]
    name: str = Field(min_length=1)
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)


class AdminCreateParticipantResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participant: Participant
    api_key: str


class AdminUpdateParticipantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    capabilities: list[str] | None = None


@router.get("/admin/stream")
async def admin_stream(
    request: Request,
    _admin: None = Depends(_require_admin),
    last_event_id_header: str | None = Header(default=None, alias="Last-Event-ID"),
) -> EventSourceResponse:
    """SSE firehose. Sees every event published into any task. Frame shape
    matches `/stream` (event/ping/stream.gap)."""
    ctx: AppContext = request.app.state.ctx

    last_event_id: int | None = None
    if last_event_id_header:
        try:
            last_event_id = int(last_event_id_header)
        except ValueError:
            last_event_id = None

    async def gen() -> AsyncIterator[dict[str, str | int]]:
        sub = await ctx.hub.subscribe_admin(last_event_id=last_event_id)
        try:
            for ev in sub.replay:
                yield {
                    "event": ev.event,
                    "id": str(ev.stream_event_id),
                    "data": json.dumps(ev.data),
                }
            ping_interval = ctx.settings.ping_interval_seconds
            while True:
                if await request.is_disconnected() or sub.closed.is_set():
                    return
                try:
                    ev = await asyncio.wait_for(sub.queue.get(), timeout=ping_interval)
                except asyncio.TimeoutError:
                    ping_id = await ctx.hub.allocate_admin_id()
                    yield {
                        "event": "ping",
                        "id": str(ping_id),
                        "data": "{}",
                    }
                    continue
                yield {
                    "event": ev.event,
                    "id": str(ev.stream_event_id),
                    "data": json.dumps(ev.data),
                }
        finally:
            sub.close()

    return EventSourceResponse(gen())


@router.get("/admin/tasks", response_model=ListTasksResponse)
async def admin_list_tasks(
    has_pending: bool | None = None,
    parent_id: str | None = Query(default=None),
    _admin: None = Depends(_require_admin),
    ctx: AppContext = Depends(get_ctx),
) -> ListTasksResponse:
    parent_uuid: UUID | None = None
    top_level_only = True
    if parent_id is not None and parent_id != "null":
        try:
            parent_uuid = UUID(parent_id)
        except ValueError:
            from goa.errors import InvalidEventShape
            raise InvalidEventShape("parent_id must be a uuid or the literal 'null'")
        top_level_only = False

    items = await ctx.service.list_tasks(
        has_pending=has_pending,
        parent_id=parent_uuid,
        top_level_only=top_level_only,
    )
    return ListTasksResponse(
        tasks=[TaskListItem(task=t, pending_questions=p) for (t, p) in items],
    )


@router.get("/admin/tasks/{task_id}", response_model=GetTaskResponse)
async def admin_get_task(
    task_id: UUID,
    _admin: None = Depends(_require_admin),
    ctx: AppContext = Depends(get_ctx),
) -> GetTaskResponse:
    task = await ctx.task_log.get_task(task_id)
    if task is None:
        raise TaskNotFound()
    events = await ctx.task_log.list_events_for_task(task_id)
    pending = await ctx.service.get_pending(task_id)
    return GetTaskResponse(task=task, pending_questions=pending, events=events)


@router.get("/admin/blobs/{blob_id}/meta", response_model=Attachment)
async def admin_get_blob_meta(
    blob_id: UUID,
    _admin: None = Depends(_require_admin),
    ctx: AppContext = Depends(get_ctx),
) -> Attachment:
    meta = await ctx.blob_store.get_meta(blob_id)
    if meta is None:
        raise BlobNotFound()
    return meta


@router.get("/admin/blobs/{blob_id}")
async def admin_download_blob(
    blob_id: UUID,
    _admin: None = Depends(_require_admin),
    ctx: AppContext = Depends(get_ctx),
) -> StreamingResponse:
    """Admin download — bypasses participant authz. Same shape as the
    participant-scoped `GET /blobs/{id}` so the dashboard can render
    previews via the existing admin token."""
    meta = await ctx.blob_store.get_meta(blob_id)
    if meta is None:
        raise BlobNotFound()
    safe = quote(meta.filename, safe="")
    return StreamingResponse(
        ctx.blob_store.open(blob_id),
        media_type=meta.mime_type,
        headers={
            "Content-Disposition": f"inline; filename=\"{meta.filename}\"; filename*=UTF-8''{safe}",
            "Content-Length": str(meta.size_bytes),
        },
    )


@router.get("/admin/participants", response_model=ListParticipantsResponse)
async def admin_list_participants(
    capability: list[str] = Query(default_factory=list),
    q: str | None = None,
    type: Literal["agent", "service"] | None = None,
    _admin: None = Depends(_require_admin),
    ctx: AppContext = Depends(get_ctx),
) -> ListParticipantsResponse:
    if q is not None and not q.strip():
        q = None
    results = await ctx.participant_store.search(capabilities=capability, q=q, type=type)
    return ListParticipantsResponse(participants=results)


@router.post(
    "/admin/participants",
    status_code=201,
    response_model=AdminCreateParticipantResponse,
)
async def admin_create_participant(
    body: AdminCreateParticipantBody,
    _admin: None = Depends(_require_admin),
    ctx: AppContext = Depends(get_ctx),
) -> AdminCreateParticipantResponse:
    """Admin-authed participant creation. Returns the raw API key once."""
    api_key = generate_api_key()
    digest = hash_api_key(ctx.settings.server_pepper, api_key)
    participant = Participant(
        type=body.type,
        name=body.name,
        description=body.description,
        capabilities=list(body.capabilities),
        access_policy="public",
        api_key_hash=digest,
    )
    await ctx.participant_store.create(participant)
    return AdminCreateParticipantResponse(participant=participant, api_key=api_key)


@router.delete("/admin/participants/{participant_id}", status_code=204)
async def admin_delete_participant(
    participant_id: UUID,
    _admin: None = Depends(_require_admin),
    ctx: AppContext = Depends(get_ctx),
) -> None:
    """Hard-delete a participant. Idempotent — deleting a non-existent id returns 204."""
    await ctx.participant_store.delete(participant_id)


@router.patch("/admin/participants/{participant_id}", response_model=Participant)
async def admin_update_participant(
    participant_id: UUID,
    body: AdminUpdateParticipantBody,
    _admin: None = Depends(_require_admin),
    ctx: AppContext = Depends(get_ctx),
) -> Participant:
    """Partial update of name, description, and/or capabilities."""
    participant = await ctx.participant_store.get(participant_id)
    if participant is None:
        raise HTTPException(status_code=404, detail="participant not found")
    updated = participant.model_copy(
        update={
            k: v
            for k, v in {
                "name": body.name,
                "description": body.description,
                "capabilities": body.capabilities,
            }.items()
            if v is not None
        }
    )
    return await ctx.participant_store.update(updated)
