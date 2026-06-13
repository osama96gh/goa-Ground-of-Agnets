from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


ParticipantType = Literal["agent", "service"]
AccessPolicy = Literal["public", "private"]
TaskStatus = Literal["open", "closed"]


class Participant(BaseModel):
    """Client-side mirror of the server `Participant` (§6.1)."""

    model_config = ConfigDict(extra="ignore")

    id: UUID
    type: ParticipantType
    name: str
    description: str = ""
    capabilities: list[str] = []
    access_policy: AccessPolicy = "public"
    api_key_hash: str
    created_at: datetime


class TaskSummary(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: UUID
    subject: str
    participants: list[UUID]
    parent_task_id: UUID | None = None
    pending_questions: list[tuple[UUID, UUID]]
    last_activity_at: datetime


class Task(BaseModel):
    """`pending_questions` is not on `Task`. Read it from
    `GetTaskResponse.pending_questions` (for `GET /tasks/{id}`) or
    `TaskListItem.pending_questions` (for `GET /tasks`).
    `TaskSummary.pending_questions` still rides on SSE stream frames."""

    model_config = ConfigDict(extra="ignore")

    id: UUID
    initiator_id: UUID
    parent_task_id: UUID | None = None
    # Lifecycle (§8). `"open"` accepts appends; `"closed"` rejects them
    # with `InvalidState` (409). Default `"open"` for forward-compat
    # with hubs that haven't deployed the close feature yet.
    status: TaskStatus = "open"
    participants: list[UUID]
    subject: str
    external_ref: str | None = None
    metadata: dict
    created_at: datetime
    updated_at: datetime
    last_activity_at: datetime


class TaskListItem(BaseModel):
    """List-endpoint composite — `GET /tasks` and `GET /admin/tasks`
    return `tasks: list[TaskListItem]`."""

    model_config = ConfigDict(extra="ignore")

    task: Task
    pending_questions: list[tuple[UUID, UUID]]


class MemoryEntry(BaseModel):
    """Client-side mirror of a server `MemoryEntry` — agent-private,
    cross-task memory owned by the caller. `value` is any JSON value."""

    model_config = ConfigDict(extra="ignore")

    id: UUID
    owner_id: UUID
    key: str
    value: Any = None
    tags: list[str] = []
    created_at: datetime
    updated_at: datetime
