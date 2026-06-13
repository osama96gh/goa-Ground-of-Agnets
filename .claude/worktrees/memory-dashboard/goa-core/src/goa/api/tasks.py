from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from goa.deps import AppContext, get_ctx, make_bearer_dependency
from goa.domain.models import (
    CreateTaskBody,
    Event,
    InboundEvent,
    Participant,
    PendingPair,
    QuestionEvent,
    Task,
    UpsertTaskBody,
)
from goa.errors import AUTH_401, InvalidEventShape, NotAParticipant, TaskNotFound, VALIDATION_422, error_response


router = APIRouter()
require_participant = make_bearer_dependency()
_INBOUND_EVENT = TypeAdapter(InboundEvent)
_CREATE_TASK = TypeAdapter(CreateTaskBody)
_UPSERT_TASK = TypeAdapter(UpsertTaskBody)


def _parse_inbound_event(raw: Any) -> InboundEvent:
    """Validate the body as an `InboundEvent`. Schema misses surface as
    `invalid_event_shape` per §12, not the generic `invalid_request`."""
    try:
        return _INBOUND_EVENT.validate_python(raw)
    except ValidationError as exc:
        raise InvalidEventShape(_first_pydantic_message(exc)) from exc


def _parse_create_task(raw: Any) -> CreateTaskBody:
    try:
        return _CREATE_TASK.validate_python(raw)
    except ValidationError as exc:
        raise InvalidEventShape(_first_pydantic_message(exc)) from exc


def _parse_upsert_task(raw: Any) -> UpsertTaskBody:
    try:
        return _UPSERT_TASK.validate_python(raw)
    except ValidationError as exc:
        raise InvalidEventShape(_first_pydantic_message(exc)) from exc


def _first_pydantic_message(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "invalid event shape"
    first = errors[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = first.get("msg", "invalid event shape")
    return f"{loc}: {msg}" if loc else msg


class CreateTaskResponse(BaseModel):
    """Response carries the full persisted `Task` (server-set timestamps,
    participants list, etc.) so SDK sugar wrappers can compose `(Task, Event)`
    without a follow-up GET."""

    model_config = ConfigDict(extra="forbid")

    task: Task


class UpsertTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: Task
    created: bool


class AppendEventResponse(BaseModel):
    """Returns the full persisted Event (server-set id/task_id/created_at)
    so SDK callers can compose with append_event without a follow-up GET."""

    model_config = ConfigDict(extra="forbid")

    event: Event


class TaskListItem(BaseModel):
    """List-endpoint composite — pending is a derived view, returned alongside
    the persisted Task."""

    model_config = ConfigDict(extra="forbid")

    task: Task
    pending_questions: list[PendingPair]


class GetTaskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: Task
    pending_questions: list[PendingPair]
    events: list[Event]


class ListChildrenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    children: list[TaskListItem]


class ListTasksResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[TaskListItem]


class PendingRow(BaseModel):
    """A single open `(question_event_id, target=caller)` pair surfaced from
    `task.pending_questions`, enriched with the question's `from`/`created_at`
    so the SDK doesn't need a second fetch to render the row.

    The spec response shape (§9.2) is `{task_id, question_event_id, from,
    created_at}`. `from_` aliases to JSON `from` so the wire matches."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    task_id: UUID
    question_event_id: UUID
    from_: UUID | None = Field(default=None, alias="from")
    created_at: datetime


@router.post(
    "/tasks",
    status_code=201,
    response_model=CreateTaskResponse,
    summary="Create a task",
    responses={
        **AUTH_401,
        403: error_response(
            "`parent_task_not_visible` — `parent_task_id` does not exist or "
            "you are not a participant of it."
        ),
        409: error_response(
            "`external_ref_in_use` — another open task already maps this "
            "`(initiator, external_ref)`."
        ),
        422: error_response(
            "`invalid_event_shape` / `participant_unknown` — malformed body "
            "or an unknown participant id."
        ),
    },
)
async def create_task(
    request: Request,
    caller: Participant = Depends(require_participant),
    ctx: AppContext = Depends(get_ctx),
) -> CreateTaskResponse:
    raw = await request.json()
    body = _parse_create_task(raw)
    task = await ctx.service.create_task(caller, body)
    return CreateTaskResponse(task=task)


@router.post(
    "/tasks/upsert",
    response_model=UpsertTaskResponse,
    summary="Find or create a task by external_ref",
    responses={
        201: {"model": UpsertTaskResponse, "description": "A new task was created."},
        200: {
            "model": UpsertTaskResponse,
            "description": "An existing open task with this external_ref was returned.",
        },
        **AUTH_401,
        403: error_response(
            "`parent_task_not_visible` — `parent_task_id` does not exist or "
            "you are not a participant of it."
        ),
        422: error_response("`invalid_event_shape` — malformed request body."),
    },
)
async def upsert_task(
    request: Request,
    response: Response,
    caller: Participant = Depends(require_participant),
    ctx: AppContext = Depends(get_ctx),
) -> UpsertTaskResponse:
    """§9.2 — find-or-create a task by `(caller, external_ref)`. 201 on
    create, 200 on hit. No event is emitted; the caller follows up with
    `POST /tasks/{id}/events` for the first message."""
    raw = await request.json()
    body = _parse_upsert_task(raw)
    task, created = await ctx.service.upsert_task(caller, body)
    response.status_code = 201 if created else 200
    return UpsertTaskResponse(task=task, created=created)


@router.post(
    "/tasks/{task_id}/close",
    response_model=CreateTaskResponse,
    summary="Close a task",
    responses={
        **AUTH_401,
        403: error_response("`forbidden_role` — only the task initiator may close it."),
        404: error_response(
            "`task_not_found` — the task does not exist or you are not a participant."
        ),
        **VALIDATION_422,
    },
)
async def close_task(
    task_id: UUID,
    caller: Participant = Depends(require_participant),
    ctx: AppContext = Depends(get_ctx),
) -> CreateTaskResponse:
    """§8 — explicit task close. Initiator-only. Idempotent (closing a
    closed task returns the existing closed task). After close, subsequent
    `POST /tasks/{id}/events` returns 409 `invalid_state`. The
    `(initiator_id, external_ref)` slot is released so the same external_ref
    can be re-`upsert`'d to a fresh task. Open child tasks receive a
    `parent_closed` system event (no cascade)."""
    task = await ctx.service.close_task(caller, task_id)
    return CreateTaskResponse(task=task)


@router.post(
    "/tasks/{task_id}/events",
    status_code=201,
    response_model=AppendEventResponse,
    summary="Append an event",
    responses={
        **AUTH_401,
        403: error_response(
            "`not_a_participant` / `forbidden_role` — you are not in the task, "
            "or the event type is restricted to the initiator."
        ),
        404: error_response(
            "`task_not_found` — the task does not exist or you are not a participant."
        ),
        409: error_response("`invalid_state` — the task is closed."),
        422: error_response(
            "`invalid_event_shape` / `not_a_target` / `participant_unknown` — "
            "malformed event, an answer referencing a question that doesn't "
            "target you, or an unknown participant id."
        ),
    },
)
async def append_event(
    task_id: UUID,
    request: Request,
    caller: Participant = Depends(require_participant),
    ctx: AppContext = Depends(get_ctx),
) -> AppendEventResponse:
    raw = await request.json()
    body = _parse_inbound_event(raw)
    event = await ctx.service.append_event(caller, task_id, body)
    return AppendEventResponse(event=event)


@router.get(
    "/tasks",
    response_model=ListTasksResponse,
    summary="List tasks",
    responses={
        **AUTH_401,
        422: error_response(
            "`invalid_event_shape` — `parent_id` is not a uuid or the literal 'null'."
        ),
    },
)
async def list_tasks(
    external_ref: str | None = None,
    role: Literal["initiator", "member"] | None = None,
    has_pending: bool | None = None,
    parent_id: str | None = Query(default=None),
    caller: Participant = Depends(require_participant),
    ctx: AppContext = Depends(get_ctx),
) -> ListTasksResponse:
    """§9.2 listing.

    - `?external_ref=<ref>`: caller-scoped lookup via the
      `(initiator_id, external_ref)` index. Returns 0 or 1 task. Mutually
      exclusive with the other filters (it short-circuits).
    - `?role=initiator|member`: narrow by caller's role on the task.
    - `?has_pending=true|false`: post-filter via the pending projection
      (derived from the event log).
    - `?parent_id=<uuid>`: list children of that parent the caller is in.
      Default (absent) returns top-level tasks only (`parent_task_id IS NULL`),
      per spec §9.2. The literal string `"null"` is accepted as an explicit
      synonym for top-level so clients that always send a value can opt in."""
    if external_ref is not None:
        task_id = await ctx.task_log.get_task_by_external_ref(caller.id, external_ref)
        if task_id is None:
            return ListTasksResponse(tasks=[])
        task = await ctx.task_log.get_task(task_id)
        if task is None:
            return ListTasksResponse(tasks=[])
        pending = await ctx.service.get_pending(task.id)
        return ListTasksResponse(tasks=[TaskListItem(task=task, pending_questions=pending)])

    parent_uuid: UUID | None = None
    top_level_only = True
    if parent_id is not None and parent_id != "null":
        try:
            parent_uuid = UUID(parent_id)
        except ValueError:
            raise InvalidEventShape("parent_id must be a uuid or the literal 'null'")
        top_level_only = False

    items = await ctx.service.list_tasks_for_participant(
        caller.id,
        role=role,
        has_pending=has_pending,
        parent_id=parent_uuid,
        top_level_only=top_level_only,
    )
    return ListTasksResponse(
        tasks=[TaskListItem(task=t, pending_questions=p) for (t, p) in items],
    )


@router.get(
    "/pending",
    response_model=list[PendingRow],
    response_model_by_alias=True,
    summary="List my pending questions",
    responses={**AUTH_401, **VALIDATION_422},
)
async def list_pending(
    caller: Participant = Depends(require_participant),
    ctx: AppContext = Depends(get_ctx),
) -> list[PendingRow]:
    """§9.2 / §9.3 — open `(task_id, question_event_id)` pairs targeting the
    caller, across all tasks the caller participates in. Reads from
    `PendingProjection`; the canonical SQL definition lives in §7."""
    items = await ctx.service.list_tasks_for_participant(
        caller.id,
        has_pending=True,
        top_level_only=False,
    )
    rows: list[PendingRow] = []
    for task, pending in items:
        for question_event_id, target_id in pending:
            if target_id != caller.id:
                continue
            ev = await ctx.task_log.get_event(question_event_id)
            # The projection is derived from the log so the question always
            # exists; defensively skip if it doesn't rather than 500ing a read.
            if ev is None or not isinstance(ev, QuestionEvent):
                continue
            rows.append(
                PendingRow(
                    task_id=task.id,
                    question_event_id=question_event_id,
                    from_=ev.from_,
                    created_at=ev.created_at,
                ),
            )
    rows.sort(key=lambda r: r.created_at)
    return rows


@router.get(
    "/tasks/{task_id}",
    response_model=GetTaskResponse,
    summary="Get a task with its events",
    responses={
        **AUTH_401,
        404: error_response(
            "`task_not_found` — the task does not exist or you are not a participant."
        ),
        **VALIDATION_422,
    },
)
async def get_task(
    task_id: UUID,
    caller: Participant = Depends(require_participant),
    ctx: AppContext = Depends(get_ctx),
) -> GetTaskResponse:
    task = await ctx.task_log.get_task(task_id)
    if task is None or caller.id not in task.participants:
        # 404 on read for non-participants — never leak existence.
        raise TaskNotFound()
    events = await ctx.task_log.list_events_for_task(task_id)
    pending = await ctx.service.get_pending(task_id)
    return GetTaskResponse(task=task, pending_questions=pending, events=events)


@router.get(
    "/tasks/{task_id}/children",
    response_model=ListChildrenResponse,
    summary="List a task's children",
    responses={**AUTH_401, **VALIDATION_422},
)
async def list_children(
    task_id: UUID,
    caller: Participant = Depends(require_participant),
    ctx: AppContext = Depends(get_ctx),
) -> ListChildrenResponse:
    # Authorization is implicit in the membership filter — non-participants
    # of the parent simply get an empty (or partial) list of children they
    # already happen to be in. Spec §9.2: "Equivalent to ?parent_id={id}".
    children = await ctx.task_log.list_children(task_id)
    visible: list[TaskListItem] = []
    for c in children:
        if caller.id not in c.participants:
            continue
        pending = await ctx.service.get_pending(c.id)
        visible.append(TaskListItem(task=c, pending_questions=pending))
    return ListChildrenResponse(children=visible)
