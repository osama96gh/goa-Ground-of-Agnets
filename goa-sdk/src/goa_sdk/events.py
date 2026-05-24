from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Attachment(BaseModel):
    """SDK mirror of the core `Attachment` (§6.5). Returned by
    `Goa.upload_blob` and embedded in `Content.attachments` to reference
    bytes stored in the hub. The wire shape is identical to the core
    model; `extra="ignore"` keeps the SDK forward-compatible if the hub
    adds new metadata fields later."""

    model_config = ConfigDict(extra="ignore")

    blob_id: UUID
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str


class Content(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str | None = None
    data: dict | None = None
    attachments: list[Attachment] = Field(default_factory=list)


class QuestionPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    to: list[UUID] = Field(min_length=1)


class AnswerPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    answering: list[UUID] = Field(min_length=1)


class InfoPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CancelQuestionPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    retracts: list[UUID] = Field(min_length=1)


class CancelAllQuestionsPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ParticipantJoinedPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    participant_id: UUID


class ChildTaskCreatedPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    task_id: UUID
    spawned_by: UUID
    subject: str | None = None


class ParentClosedPayload(BaseModel):
    """Reserved per spec §6.3 — emitted into a child when its parent closes.
    No emitter in v2; the type exists so SDK clients accept it forward."""

    model_config = ConfigDict(extra="ignore")

    task_id: UUID


class _EventBase(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: UUID
    task_id: UUID
    from_: UUID | None = Field(default=None, alias="from")
    content: Content = Field(default_factory=Content)
    in_reply_to: UUID | None = None
    metadata: dict = Field(default_factory=dict)
    # Caller-supplied idempotency key (§6.3). Echoed back on the persisted
    # event so SDK clients can correlate a retried append to the original.
    client_event_id: UUID | None = None
    created_at: datetime


class QuestionEvent(_EventBase):
    event_type: Literal["question"]
    payload: QuestionPayload


class AnswerEvent(_EventBase):
    event_type: Literal["answer"]
    payload: AnswerPayload


class InfoEvent(_EventBase):
    event_type: Literal["info"]
    payload: InfoPayload = Field(default_factory=InfoPayload)


class CancelQuestionEvent(_EventBase):
    event_type: Literal["cancel_question"]
    payload: CancelQuestionPayload


class CancelAllQuestionsEvent(_EventBase):
    event_type: Literal["cancel_all_questions"]
    payload: CancelAllQuestionsPayload = Field(default_factory=CancelAllQuestionsPayload)


class ParticipantJoinedEvent(_EventBase):
    event_type: Literal["participant_joined"]
    payload: ParticipantJoinedPayload


class ChildTaskCreatedEvent(_EventBase):
    event_type: Literal["child_task_created"]
    payload: ChildTaskCreatedPayload


class ParentClosedEvent(_EventBase):
    event_type: Literal["parent_closed"]
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
# Outbound (request bodies)
# ---------------------------------------------------------------------------

class _OutboundEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Content = Field(default_factory=Content)
    in_reply_to: UUID | None = None
    metadata: dict = Field(default_factory=dict)
    # Optional idempotency key. Pass a stable UUID to make the append
    # idempotent against transport retries — two requests with the same
    # `(task, caller, client_event_id)` resolve to a single persisted
    # event. The SDK does not auto-generate; callers own the keyspace.
    client_event_id: UUID | None = None


class OutboundQuestion(_OutboundEventBase):
    event_type: Literal["question"] = "question"
    payload: QuestionPayload


class OutboundAnswer(_OutboundEventBase):
    event_type: Literal["answer"] = "answer"
    payload: AnswerPayload


class OutboundInfo(_OutboundEventBase):
    event_type: Literal["info"] = "info"
    payload: InfoPayload = Field(default_factory=InfoPayload)


class OutboundCancelQuestion(_OutboundEventBase):
    event_type: Literal["cancel_question"] = "cancel_question"
    payload: CancelQuestionPayload


class OutboundCancelAllQuestions(_OutboundEventBase):
    event_type: Literal["cancel_all_questions"] = "cancel_all_questions"
    payload: CancelAllQuestionsPayload = Field(default_factory=CancelAllQuestionsPayload)


OutboundEvent = Union[
    OutboundQuestion,
    OutboundAnswer,
    OutboundInfo,
    OutboundCancelQuestion,
    OutboundCancelAllQuestions,
]
