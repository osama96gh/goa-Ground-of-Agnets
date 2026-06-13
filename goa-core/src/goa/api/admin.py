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
from collections import Counter
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from goa.api.memory import ListMemoryResponse
from goa.auth import _parse_bearer, generate_api_key, hash_api_key
from goa.deps import AppContext, get_ctx
from goa.domain.models import Attachment, Event, Participant, PendingPair, Task
from goa.errors import (
    ADMIN_401,
    VALIDATION_422,
    BlobNotFound,
    TaskNotFound,
    Unauthorized,
    error_response,
)


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
    # Keyset cursor for the next page, or null when this is the last page.
    next_cursor: str | None = None


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


# --- /admin/stats response models -------------------------------------------


class StatsTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: int
    tasks_open: int
    tasks_closed: int
    participants: int
    participants_agent: int
    participants_service: int
    pending_questions: int
    events_total: int


class EventVolumeBucket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str  # YYYY-MM-DD (UTC)
    count: int


class TasksByStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open: int
    closed: int


class RecentActivityItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    subject: str
    status: Literal["open", "closed"]
    last_activity_at: datetime
    pending_count: int


class PendingBacklogItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    subject: str
    pending_count: int
    oldest_pending_at: datetime | None


class AdminStatsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    totals: StatsTotals
    events_today: int
    event_volume: list[EventVolumeBucket]
    tasks_by_status: TasksByStatus
    recent_activity: list[RecentActivityItem]
    pending_backlog: list[PendingBacklogItem]


@router.get(
    "/admin/stream",
    summary="Admin event firehose (SSE)",
    responses={
        200: {
            "description": (
                "A Server-Sent Events firehose (text/event-stream) of every "
                "event across all tasks; same frame shape as `/stream`."
            )
        },
        **ADMIN_401,
    },
)
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


@router.get(
    "/admin/tasks",
    response_model=ListTasksResponse,
    summary="List all tasks (admin)",
    responses={
        **ADMIN_401,
        422: error_response(
            "`invalid_event_shape` — `parent_id` is not a uuid or the "
            "literal 'null'."
        ),
    },
)
async def admin_list_tasks(
    has_pending: bool | None = None,
    parent_id: str | None = Query(default=None),
    status: Literal["open", "closed"] | None = Query(default=None),
    q: str | None = Query(default=None),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=200),
    cursor: str | None = Query(default=None),
    _admin: None = Depends(_require_admin),
    ctx: AppContext = Depends(get_ctx),
) -> ListTasksResponse:
    """List tasks with optional filters and keyset pagination.

    Filters (`status`, `q` subject substring, `since`/`until` on
    `last_activity_at`) and keyset pagination are applied in-process over the
    repo's already-sorted (`last_activity_at DESC`) result. `has_pending` is a
    derived-projection post-filter (it always was), so paging is over the
    filtered set — page sizes are exact for the returned items. At very large
    scale this should move into the repo layer; at current scale the in-process
    pass matches the cost profile of the rest of the admin surface.
    """
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

    # In-process filters over the sorted (last_activity_at DESC) list.
    if status is not None:
        items = [(t, p) for (t, p) in items if t.status == status]
    if q and q.strip():
        needle = q.strip().lower()
        items = [(t, p) for (t, p) in items if needle in t.subject.lower()]
    if since is not None:
        items = [(t, p) for (t, p) in items if t.last_activity_at >= since]
    if until is not None:
        items = [(t, p) for (t, p) in items if t.last_activity_at <= until]

    # Impose a deterministic total order (last_activity_at DESC, id DESC) so
    # the keyset cursor below is stable when timestamps tie — the repo only
    # guarantees the primary sort.
    items.sort(key=lambda tp: (tp[0].last_activity_at, _id_key(tp[0].id)), reverse=True)

    # Keyset pagination: the cursor encodes the last item of the previous page
    # as "<last_activity_at iso>|<task id>". Resume strictly after it in the
    # (last_activity_at DESC, id DESC) order the repo guarantees.
    if cursor:
        after = _decode_task_cursor(cursor)
        if after is not None:
            cur_ts, cur_id = after
            items = [
                (t, p)
                for (t, p) in items
                if (t.last_activity_at, _id_key(t.id)) < (cur_ts, _id_key(cur_id))
            ]

    next_cursor: str | None = None
    if limit is not None and len(items) > limit:
        last_task = items[limit - 1][0]
        next_cursor = f"{last_task.last_activity_at.isoformat()}|{last_task.id}"
        items = items[:limit]

    return ListTasksResponse(
        tasks=[TaskListItem(task=t, pending_questions=p) for (t, p) in items],
        next_cursor=next_cursor,
    )


def _id_key(task_id: UUID) -> str:
    return str(task_id)


def _decode_task_cursor(cursor: str) -> tuple[datetime, UUID] | None:
    """Decode "<iso>|<uuid>"; return None if malformed (treated as page 1)."""
    ts_raw, _, id_raw = cursor.rpartition("|")
    if not ts_raw or not id_raw:
        return None
    try:
        return datetime.fromisoformat(ts_raw), UUID(id_raw)
    except ValueError:
        return None


def _parse_window_days(window: str | None) -> int:
    """Parse a `?window=14d` value into a day count, clamped to [1, 90].
    Accepts a bare integer or an integer with a trailing 'd'."""
    if not window:
        return 14
    raw = window[:-1] if window.endswith("d") else window
    try:
        days = int(raw)
    except ValueError:
        return 14
    return max(1, min(90, days))


@router.get(
    "/admin/stats",
    response_model=AdminStatsResponse,
    summary="Aggregate dashboard metrics (admin)",
    responses={**ADMIN_401, **VALIDATION_422},
)
async def admin_stats(
    window: str | None = Query(default="14d"),
    recent_limit: int = Query(default=8, ge=1, le=50),
    _admin: None = Depends(_require_admin),
    ctx: AppContext = Depends(get_ctx),
) -> AdminStatsResponse:
    """Single aggregate-metrics endpoint backing the dashboard Overview.

    Computed from existing read paths (no bespoke aggregation queries):
    every task plus its derived `pending_questions`, every participant, and
    the per-task event logs. This is O(tasks + events) — fine at current
    scale, and the global pending total is the same O(tasks×events) cost the
    rest of the admin surface already pays for the derived projection. A
    deployment with very large logs should cache this response.
    """
    window_days = _parse_window_days(window)

    # All tasks (no participant gate, all levels) with their derived pending.
    items = await ctx.service.list_tasks(top_level_only=False)
    participants = await ctx.participant_store.search()

    # Day buckets for the trailing window, today last. Pre-seed every day to
    # 0 so the chart has a continuous x-axis even on quiet days.
    today = datetime.now(tz=timezone.utc).date()
    buckets: dict[str, int] = {
        (today - timedelta(days=i)).isoformat(): 0
        for i in range(window_days - 1, -1, -1)
    }
    today_iso = today.isoformat()

    tasks_open = tasks_closed = events_total = events_today = 0
    pending_total = 0
    backlog: list[PendingBacklogItem] = []

    for task, pending in items:
        if task.status == "open":
            tasks_open += 1
        else:
            tasks_closed += 1
        pending_total += len(pending)

        events = await ctx.task_log.list_events_for_task(task.id)
        events_total += len(events)
        created_by_id = {ev.id: ev.created_at for ev in events}
        for ev in events:
            day = ev.created_at.astimezone(timezone.utc).date().isoformat()
            if day == today_iso:
                events_today += 1
            if day in buckets:
                buckets[day] += 1

        if pending:
            # oldest_pending_at = earliest creation among the still-open
            # question events (pending pairs are (question_event_id, target)).
            stamps = [
                created_by_id[qid]
                for qid, _ in pending
                if qid in created_by_id
            ]
            backlog.append(
                PendingBacklogItem(
                    task_id=task.id,
                    subject=task.subject,
                    pending_count=len(pending),
                    oldest_pending_at=min(stamps) if stamps else None,
                )
            )

    type_counts = Counter(p.type for p in participants)

    # items is already sorted most-recently-active first by the repo layer.
    recent = [
        RecentActivityItem(
            task_id=task.id,
            subject=task.subject,
            status=task.status,
            last_activity_at=task.last_activity_at,
            pending_count=len(pending),
        )
        for task, pending in items[:recent_limit]
    ]

    # Backlog: most pending first, then oldest question first.
    backlog.sort(
        key=lambda b: (
            -b.pending_count,
            b.oldest_pending_at or datetime.max.replace(tzinfo=timezone.utc),
        )
    )

    return AdminStatsResponse(
        totals=StatsTotals(
            tasks=len(items),
            tasks_open=tasks_open,
            tasks_closed=tasks_closed,
            participants=len(participants),
            participants_agent=type_counts.get("agent", 0),
            participants_service=type_counts.get("service", 0),
            pending_questions=pending_total,
            events_total=events_total,
        ),
        events_today=events_today,
        event_volume=[
            EventVolumeBucket(date=d, count=c) for d, c in buckets.items()
        ],
        tasks_by_status=TasksByStatus(open=tasks_open, closed=tasks_closed),
        recent_activity=recent,
        pending_backlog=backlog[:recent_limit],
    )


@router.get(
    "/admin/tasks/{task_id}",
    response_model=GetTaskResponse,
    summary="Get any task (admin)",
    responses={
        **ADMIN_401,
        404: error_response("`task_not_found` — no task with that id."),
        **VALIDATION_422,
    },
)
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


class AdminCloseTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: Task


@router.post(
    "/admin/tasks/{task_id}/close",
    response_model=AdminCloseTaskResponse,
    summary="Close any task (admin)",
    responses={
        **ADMIN_401,
        404: error_response("`task_not_found` — no task with that id."),
        **VALIDATION_422,
    },
)
async def admin_close_task(
    task_id: UUID,
    _admin: None = Depends(_require_admin),
    ctx: AppContext = Depends(get_ctx),
) -> AdminCloseTaskResponse:
    """Operator close — transitions the task to `closed`, releases its
    `external_ref` slot, and fans `parent_closed` out to open children.
    Bypasses the initiator-only rule that gates `POST /tasks/{id}/close`
    (the admin token is authority enough). Idempotent: closing an
    already-closed task returns it unchanged."""
    task = await ctx.service.admin_close_task(task_id)
    return AdminCloseTaskResponse(task=task)


@router.get(
    "/admin/blobs/{blob_id}/meta",
    response_model=Attachment,
    summary="Get any blob's metadata (admin)",
    responses={
        **ADMIN_401,
        404: error_response("`blob_not_found` — no blob with that id."),
        **VALIDATION_422,
    },
)
async def admin_get_blob_meta(
    blob_id: UUID,
    _admin: None = Depends(_require_admin),
    ctx: AppContext = Depends(get_ctx),
) -> Attachment:
    meta = await ctx.blob_store.get_meta(blob_id)
    if meta is None:
        raise BlobNotFound()
    return meta


@router.get(
    "/admin/blobs/{blob_id}",
    summary="Download any blob (admin)",
    responses={
        200: {
            "description": "The raw blob bytes.",
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        },
        **ADMIN_401,
        404: error_response("`blob_not_found` — no blob with that id."),
        **VALIDATION_422,
    },
)
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


@router.get(
    "/admin/participants",
    response_model=ListParticipantsResponse,
    summary="Search participants (admin)",
    responses={**ADMIN_401, **VALIDATION_422},
)
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
    summary="Create a participant (admin)",
    responses={**ADMIN_401, **VALIDATION_422},
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


@router.delete(
    "/admin/participants/{participant_id}",
    status_code=204,
    summary="Delete a participant (admin)",
    responses={**ADMIN_401, **VALIDATION_422},
)
async def admin_delete_participant(
    participant_id: UUID,
    _admin: None = Depends(_require_admin),
    ctx: AppContext = Depends(get_ctx),
) -> None:
    """Hard-delete a participant. Idempotent — deleting a non-existent id returns 204.

    Also releases the participant's agent-private memory (§9): `owner_id` carries
    no foreign key, so the wipe is an explicit cross-store call rather than a cascade.
    """
    await ctx.participant_store.delete(participant_id)
    await ctx.memory_store.purge_owner(participant_id)


@router.patch(
    "/admin/participants/{participant_id}",
    response_model=Participant,
    summary="Update a participant (admin)",
    responses={
        **ADMIN_401,
        404: error_response("`not_found` — no participant with that id."),
        **VALIDATION_422,
    },
)
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


@router.get(
    "/admin/participants/{participant_id}/memory",
    response_model=ListMemoryResponse,
    summary="List a participant's memory (admin)",
    responses={
        **ADMIN_401,
        404: error_response("`not_found` — no participant with that id."),
        **VALIDATION_422,
    },
)
async def admin_list_participant_memory(
    participant_id: UUID,
    key: str | None = None,
    prefix: str | None = None,
    tag: list[str] = Query(default_factory=list),
    _admin: None = Depends(_require_admin),
    ctx: AppContext = Depends(get_ctx),
) -> ListMemoryResponse:
    """Read any participant's agent-private memory (read-only). The admin
    token bypasses the owner-scoped seal for *reads* only — there is no
    admin write/forget (a participant's memory must reflect what it
    observed). Same `key`/`prefix`/`tag` filters and `{entries: [...]}`
    shape as the participant-scoped `GET /memory`. `404` if the participant
    does not exist (so callers can tell "no entries" from "no such id")."""
    participant = await ctx.participant_store.get(participant_id)
    if participant is None:
        raise HTTPException(status_code=404, detail="participant not found")
    if key is not None:
        entry = await ctx.memory_store.get_memory(participant_id, key)
        return ListMemoryResponse(entries=[entry] if entry is not None else [])
    if prefix is not None and not prefix.strip():
        # Blank prefix is "no filter", mirroring the participant endpoint.
        prefix = None
    entries = await ctx.memory_store.list_memory(
        participant_id, key_prefix=prefix, tags=tag or None,
    )
    return ListMemoryResponse(entries=entries)
