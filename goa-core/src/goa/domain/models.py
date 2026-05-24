from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Union
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Participant
# ---------------------------------------------------------------------------

ParticipantType = Literal["agent", "service"]
AccessPolicy = Literal["public", "private"]
TaskStatus = Literal["open", "closed"]


class Participant(BaseModel):
    """`access_policy` is **reserved** in v2 (§6.1, §13): the field exists
    on the registry schema for forward-compat with v3 enforcement, default
    `"public"`, and there is no per-participant ACL gate yet."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    type: ParticipantType
    name: str
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    # `access_policy` accepts both `"public"` and `"private"` on the registry
    # row for forward-compat with v3 enforcement. The wire body
    # `CreateParticipantBody` (api/participants.py) is intentionally stricter
    # — it accepts only `"public"` until v3 turns on per-participant ACLs.
    access_policy: AccessPolicy = "public"
    api_key_hash: str
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------

class Attachment(BaseModel):
    """A typed reference to a binary blob stored in the hub. Bytes never
    travel inside `Content`; senders upload via `POST /blobs` and embed the
    returned `Attachment` here. Receivers fetch via `GET /blobs/{blob_id}`."""

    model_config = ConfigDict(extra="forbid")

    blob_id: UUID
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Content(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    data: dict | None = None
    attachments: list[Attachment] = Field(default_factory=list)


class QuestionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: list[UUID] = Field(min_length=1)


class AnswerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answering: list[UUID] = Field(min_length=1)


class InfoPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CancelQuestionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retracts: list[UUID] = Field(min_length=1)


class CancelAllQuestionsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ParticipantJoinedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    participant_id: UUID


class ChildTaskCreatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    spawned_by: UUID
    subject: str | None = None


class ParentClosedPayload(BaseModel):
    """Emitted by Goa into every open child task when the parent closes
    via `TaskService.close_task`. `task_id` is the parent's id (not the
    child's — `task_id` on the envelope already identifies the child).
    Children stay open after a `parent_closed`; cascade-close is not a
    feature of v2 (§7 sub-task independence)."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID


class _EventBase(BaseModel):
    """Common envelope (§6.3). `from_` aliases to JSON `from`.

    `seq` is the per-task monotonic ordinal — assigned by `TaskLog.append_event`
    under `lock(task_id)`, not by the constructor. The 0 default is a
    placeholder so service code can build an event before persistence
    decides its seq; do not rely on it for ordering."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    seq: int = 0
    from_: UUID | None = Field(default=None, alias="from")
    content: Content = Field(default_factory=Content)
    in_reply_to: UUID | None = None
    metadata: dict = Field(default_factory=dict)
    # `client_event_id` — optional, caller-supplied idempotency key (§6.3).
    # Opaque to Goa beyond uniqueness within `(task_id, from_, client_event_id)`.
    # System events (`from_ is None`) always leave this `None`; only inbound,
    # client-submitted events carry a value.
    client_event_id: UUID | None = None
    created_at: datetime = Field(default_factory=_now)


class QuestionEvent(_EventBase):
    event_type: Literal["question"] = "question"
    payload: QuestionPayload


class AnswerEvent(_EventBase):
    event_type: Literal["answer"] = "answer"
    payload: AnswerPayload


class InfoEvent(_EventBase):
    event_type: Literal["info"] = "info"
    payload: InfoPayload = Field(default_factory=InfoPayload)


class CancelQuestionEvent(_EventBase):
    event_type: Literal["cancel_question"] = "cancel_question"
    payload: CancelQuestionPayload


class CancelAllQuestionsEvent(_EventBase):
    event_type: Literal["cancel_all_questions"] = "cancel_all_questions"
    payload: CancelAllQuestionsPayload = Field(default_factory=CancelAllQuestionsPayload)


class ParticipantJoinedEvent(_EventBase):
    event_type: Literal["participant_joined"] = "participant_joined"
    payload: ParticipantJoinedPayload


class ChildTaskCreatedEvent(_EventBase):
    event_type: Literal["child_task_created"] = "child_task_created"
    payload: ChildTaskCreatedPayload


class ParentClosedEvent(_EventBase):
    """Goa-emitted into every open child of a task that the initiator
    just closed via `POST /tasks/{id}/close`. Best-effort live signal —
    a child created concurrently with the close may miss the event but
    can observe the parent's `status='closed'` via `GET /tasks/{id}`."""

    event_type: Literal["parent_closed"] = "parent_closed"
    payload: ParentClosedPayload


Event = Annotated[
    Union[
        QuestionEvent,
        AnswerEvent,
        InfoEvent,
        CancelQuestionEvent,
        CancelAllQuestionsEvent,
        ParticipantJoinedEvent,
        ChildTaskCreatedEvent,
        ParentClosedEvent,
    ],
    Field(discriminator="event_type"),
]


# ---------------------------------------------------------------------------
# Inbound event request shapes (no id / task_id / from / created_at — server-set)
# ---------------------------------------------------------------------------

class _InboundEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Content = Field(default_factory=Content)
    in_reply_to: UUID | None = None
    metadata: dict = Field(default_factory=dict)
    # Optional idempotency key (§6.3, §13). When set, two appends with the
    # same `(task_id, caller_id, client_event_id)` return the first persisted
    # event without mutating state. Clients own the keyspace — reusing a key
    # for a different intent is a client bug (no body-hash conflict detection
    # in v2).
    client_event_id: UUID | None = None


class InboundQuestion(_InboundEventBase):
    event_type: Literal["question"] = "question"
    payload: QuestionPayload


class InboundAnswer(_InboundEventBase):
    event_type: Literal["answer"] = "answer"
    payload: AnswerPayload


class InboundInfo(_InboundEventBase):
    event_type: Literal["info"] = "info"
    payload: InfoPayload = Field(default_factory=InfoPayload)


class InboundCancelQuestion(_InboundEventBase):
    event_type: Literal["cancel_question"] = "cancel_question"
    payload: CancelQuestionPayload


class InboundCancelAllQuestions(_InboundEventBase):
    event_type: Literal["cancel_all_questions"] = "cancel_all_questions"
    payload: CancelAllQuestionsPayload = Field(default_factory=CancelAllQuestionsPayload)


InboundEvent = Annotated[
    Union[
        InboundQuestion,
        InboundAnswer,
        InboundInfo,
        InboundCancelQuestion,
        InboundCancelAllQuestions,
    ],
    Field(discriminator="event_type"),
]


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------

PendingPair = tuple[UUID, UUID]
"""(question_event_id, target_id) — materialized §7."""


class Task(BaseModel):
    """Persisted task header. `pending_questions` is not on this model — it's
    a derived view rebuildable from the event log per §7, served via
    `PendingProjection`. On read endpoints it's returned alongside `Task`
    (see §9.2); on SSE frames it remains on `TaskSummary` (§9.3)."""

    model_config = ConfigDict(extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    initiator_id: UUID
    parent_task_id: UUID | None = None
    # Lifecycle dimension (§8). `open` accepts appends; `closed` rejects
    # them with 409 `invalid_state`. Closed tasks remain readable and
    # release their `(initiator_id, external_ref)` slot for re-upsert.
    # Orthogonal to `pending_questions` — a closed task can still have
    # unanswered questions (initiator gave up); an open task can have
    # zero pending pairs (between turns).
    status: TaskStatus = "open"
    participants: list[UUID]
    subject: str = ""
    external_ref: str | None = None
    metadata: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    last_activity_at: datetime = Field(default_factory=_now)


class TaskSummary(BaseModel):
    """Stream-frame `task` payload — §9.3."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    subject: str
    participants: list[UUID]
    parent_task_id: UUID | None = None
    pending_questions: list[PendingPair]
    last_activity_at: datetime

    @classmethod
    def from_state(cls, task: Task, pending: list[PendingPair]) -> "TaskSummary":
        """Build the stream-frame projection. `pending` is fetched from
        `PendingProjection.get(task.id)` by the caller (typically inside the
        per-task lock so it's read-after-apply consistent)."""
        return cls(
            id=task.id,
            subject=task.subject,
            participants=list(task.participants),
            parent_task_id=task.parent_task_id,
            pending_questions=list(pending),
            last_activity_at=task.last_activity_at,
        )


# ---------------------------------------------------------------------------
# Inbound task creation
# ---------------------------------------------------------------------------

class CreateTaskBody(BaseModel):
    """`POST /tasks` produces a task header only — no `opening_event`. The
    first event flows through `POST /tasks/{id}/events` like every
    subsequent event."""

    model_config = ConfigDict(extra="forbid")

    subject: str = ""
    parent_task_id: UUID | None = None
    external_ref: str | None = Field(default=None, min_length=1)
    metadata: dict = Field(default_factory=dict)


class UpsertTaskOnCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = ""
    parent_task_id: UUID | None = None
    metadata: dict = Field(default_factory=dict)


class UpsertTaskBody(BaseModel):
    """§9.2 `POST /tasks/upsert` — find-or-create keyed on
    `(initiator_id, external_ref)`. The caller is the initiator."""

    model_config = ConfigDict(extra="forbid")

    external_ref: str = Field(min_length=1)
    on_create: UpsertTaskOnCreate
