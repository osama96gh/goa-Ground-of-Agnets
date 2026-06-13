from __future__ import annotations

import mimetypes
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import httpx
from pydantic import TypeAdapter

from goa_sdk._http import make_client
from goa_sdk._stream import SseFrame, iter_sse
from goa_sdk.errors import raise_for_response
from goa_sdk.events import Attachment, Content, Event, OutboundEvent
from goa_sdk.models import MemoryEntry, Participant, Task, TaskListItem, TaskSummary


_EVENT_ADAPTER = TypeAdapter(Event)


@dataclass
class StreamFrame:
    """SDK-level wrapper over an SSE frame.

    `event_name` is the SSE event name (`event`, `ping`, `stream.gap`); when
    `event_name == "event"` the `event`/`task_id`/`task` fields are populated.

    When `event_name == "stream.gap"` the application MUST treat affected
    tasks as out-of-sync and recover state via `Goa.get_task(...)` per spec
    §9.3. The SDK surfaces the synthetic frame rather than auto-refetching
    because it does not know which tasks the application cares about.
    """

    event_name: str
    last_event_id: str | None
    raw: Any
    task_id: UUID | None = None
    event: Event | None = None
    task: TaskSummary | None = None


@dataclass
class GetTaskResult:
    """`pending_questions` is a derived view returned alongside the persisted
    Task, not nested inside it."""

    task: Task
    pending_questions: list[tuple[UUID, UUID]]
    events: list[Event]


@dataclass
class PendingRow:
    """A single open `(task_id, question_event_id)` pair targeting the caller,
    matching the `GET /pending` row shape (§9.2)."""

    task_id: UUID
    question_event_id: UUID
    from_: UUID | None
    created_at: datetime


class Goa:
    """SDK entrypoint. `Goa(api_key, base_url)` constructs an authed client;
    `Goa.register_participant(...)` is the bootstrap classmethod that returns a
    pre-authed client and the one-shot api key.

    Event variants supported on `OutboundEvent`: `question`, `answer`, `info`,
    `cancel_question`, `cancel_all_questions`. Inbound `Event` decoding also
    handles the `participant_joined` system event."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = 30.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._transport = transport
        self._client = make_client(
            base_url, api_key=api_key, transport=transport, timeout=timeout
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "Goa":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------
    @classmethod
    async def register_participant(
        cls,
        base_url: str,
        *,
        type: str,
        name: str,
        description: str = "",
        capabilities: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float | None = 30.0,
    ) -> tuple["Goa", str, Participant]:
        """Bootstrap: register a participant and return `(client, api_key, participant)`.
        The api key is shown once — callers persist it themselves.

        `description` and `capabilities` participate in `GET /participants`
        discovery (§11). `access_policy` is reserved (§6.1) and not settable
        until v3 enforcement lands."""
        body: dict[str, Any] = {"type": type, "name": name}
        if description:
            body["description"] = description
        if capabilities:
            body["capabilities"] = list(capabilities)
        async with make_client(base_url, transport=transport, timeout=timeout) as bootstrap:
            response = await bootstrap.post("/participants", json=body)
            raise_for_response(response)
            decoded = response.json()
        api_key = decoded["api_key"]
        participant = Participant.model_validate(decoded["participant"])
        client = cls(api_key, base_url, transport=transport, timeout=timeout)
        return client, api_key, participant

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    async def search_participants(
        self,
        *,
        capability: list[str] | None = None,
        q: str | None = None,
        type: Literal["agent", "service"] | None = None,
    ) -> list[Participant]:
        """`GET /participants` (§9.1, §11). `capability` is repeatable and
        AND-ed; `q` is case-insensitive substring on name+description; `type`
        filters by participant type."""
        params: list[tuple[str, str]] = []
        for cap in capability or ():
            params.append(("capability", cap))
        if q is not None:
            params.append(("q", q))
        if type is not None:
            params.append(("type", type))
        response = await self._client.get("/participants", params=params)
        raise_for_response(response)
        decoded = response.json()
        return [Participant.model_validate(p) for p in decoded["participants"]]

    async def get_participant(self, participant_id: UUID) -> Participant:
        """`GET /participants/{id}` (§9.1). Raises `GoaSdkError(404, not_found)`
        if the participant does not exist."""
        response = await self._client.get(f"/participants/{participant_id}")
        raise_for_response(response)
        return Participant.model_validate(response.json())

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    async def create_task(
        self,
        *,
        subject: str = "",
        parent_task_id: UUID | None = None,
        external_ref: str | None = None,
        metadata: dict | None = None,
    ) -> Task:
        """`POST /tasks` — create a task header. The task is created empty
        (no events). Send the first message via `append_event(task.id, ...)`.
        For the common `create + first event` flow, prefer `start_task(...)`."""
        body: dict[str, Any] = {
            "subject": subject,
            "metadata": dict(metadata or {}),
        }
        if parent_task_id is not None:
            body["parent_task_id"] = str(parent_task_id)
        if external_ref is not None:
            body["external_ref"] = external_ref
        response = await self._client.post("/tasks", json=body)
        raise_for_response(response)
        decoded = response.json()
        return Task.model_validate(decoded["task"])

    async def upsert_task(
        self,
        *,
        external_ref: str,
        subject: str = "",
        parent_task_id: UUID | None = None,
        metadata: dict | None = None,
    ) -> tuple[Task, bool]:
        """§9.2 `POST /tasks/upsert` — find-or-create keyed on
        `(caller, external_ref)`. Returns `(task, created)`. No event is
        emitted; use `upsert_and_send(...)` for the common
        find-or-create-then-send flow.

        The kwargs flatten the spec's nested `on_create:{...}` body into
        Pythonic keywords; the SDK still serializes them into `on_create`
        on the wire."""
        on_create: dict[str, Any] = {
            "subject": subject,
            "metadata": dict(metadata or {}),
        }
        if parent_task_id is not None:
            on_create["parent_task_id"] = str(parent_task_id)
        body = {"external_ref": external_ref, "on_create": on_create}
        response = await self._client.post("/tasks/upsert", json=body)
        raise_for_response(response)
        decoded = response.json()
        return Task.model_validate(decoded["task"]), bool(decoded["created"])

    async def start_task(
        self,
        *,
        first_event: OutboundEvent,
        subject: str = "",
        parent_task_id: UUID | None = None,
        external_ref: str | None = None,
        metadata: dict | None = None,
    ) -> tuple[Task, Event]:
        """Sugar for the common `create_task + append_event` pair. Creates an
        empty task then appends `first_event` to it; returns the persisted
        task plus the appended event."""
        task = await self.create_task(
            subject=subject,
            parent_task_id=parent_task_id,
            external_ref=external_ref,
            metadata=metadata,
        )
        event = await self.append_event(task.id, first_event)
        return task, event

    async def upsert_and_send(
        self,
        *,
        external_ref: str,
        event: OutboundEvent,
        subject: str = "",
        parent_task_id: UUID | None = None,
        metadata: dict | None = None,
    ) -> tuple[Task, bool, Event]:
        """Sugar for the common `upsert_task + append_event` pair. Finds or
        creates the task by `(caller, external_ref)` and appends `event` to
        it; returns `(task, created, appended_event)`."""
        task, created = await self.upsert_task(
            external_ref=external_ref,
            subject=subject,
            parent_task_id=parent_task_id,
            metadata=metadata,
        )
        appended = await self.append_event(task.id, event)
        return task, created, appended

    async def close_task(self, task_id: UUID) -> Task:
        """`POST /tasks/{id}/close` — mark a task closed. Initiator only.

        Idempotent: closing an already-closed task returns the existing
        closed task without error. After close, subsequent
        `append_event` calls on this task raise `InvalidState` (409).
        The task's `external_ref` slot, if any, is released — re-upserting
        with the same external_ref creates a fresh task. Open child tasks
        receive a `parent_closed` system event (no cascade)."""
        response = await self._client.post(f"/tasks/{task_id}/close")
        raise_for_response(response)
        return Task.model_validate(response.json()["task"])

    async def append_event(
        self,
        task_id: UUID,
        event: OutboundEvent,
    ) -> Event:
        """`POST /tasks/{id}/events` — append an event to a task. Server
        returns the persisted `Event` so callers don't need a follow-up read
        for server-set fields like `id` and `created_at`.

        **Idempotency.** Set `event.client_event_id` to a stable UUID to
        make the append idempotent against transport retries. Two appends
        with the same `(task, caller, client_event_id)` resolve to a
        single persisted event; the second returns the original. The SDK
        does not auto-generate the key — you own the keyspace (using the
        same key for a different intent is a client bug).
        """
        response = await self._client.post(
            f"/tasks/{task_id}/events",
            json=event.model_dump(mode="json"),
        )
        raise_for_response(response)
        return _EVENT_ADAPTER.validate_python(response.json()["event"])

    # ------------------------------------------------------------------
    # Blobs (§6.5) — multi-modal attachments
    # ------------------------------------------------------------------
    async def upload_blob(
        self,
        task_id: UUID,
        source: str | Path | bytes,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
    ) -> Attachment:
        """Upload a file/bytes blob bound to `task_id`. Each blob carries
        exactly one task_id, set at upload time and immutable. Cross-task
        references in event content are rejected with `BlobForbidden 403`.
        Streams from disk for `Path` sources so large files do not have to
        be buffered in RAM."""
        path: Path | None = None
        body: bytes | None = None
        if isinstance(source, (str, Path)):
            path = Path(source)
            chosen_name = filename or path.name
            chosen_mime = mime_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            files = {"file": (chosen_name, path.open("rb"), chosen_mime)}
        else:
            body = source
            chosen_name = filename or "upload.bin"
            chosen_mime = mime_type or "application/octet-stream"
            files = {"file": (chosen_name, body, chosen_mime)}
        try:
            response = await self._client.post(f"/tasks/{task_id}/blobs", files=files)
        finally:
            f = files["file"][1]
            if hasattr(f, "close"):
                f.close()
        raise_for_response(response)
        return Attachment.model_validate(response.json())

    async def get_blob_meta(self, blob_id: UUID) -> Attachment:
        response = await self._client.get(f"/blobs/{blob_id}/meta")
        raise_for_response(response)
        return Attachment.model_validate(response.json())

    async def download_blob(self, blob_id: UUID) -> bytes:
        response = await self._client.get(f"/blobs/{blob_id}")
        raise_for_response(response)
        return response.content

    @asynccontextmanager
    async def open_blob(self, blob_id: UUID) -> AsyncIterator[AsyncIterator[bytes]]:
        """Stream a blob in chunks. Use for large files where
        `download_blob` would buffer the whole body in RAM."""
        async with self._client.stream("GET", f"/blobs/{blob_id}") as response:
            raise_for_response(response)
            yield response.aiter_bytes()

    async def get_task(self, task_id: UUID) -> GetTaskResult:
        response = await self._client.get(f"/tasks/{task_id}")
        raise_for_response(response)
        decoded = response.json()
        return GetTaskResult(
            task=Task.model_validate(decoded["task"]),
            pending_questions=[
                (UUID(qid), UUID(tid))
                for (qid, tid) in decoded.get("pending_questions", [])
            ],
            events=[_EVENT_ADAPTER.validate_python(ev) for ev in decoded["events"]],
        )

    async def list_children(self, task_id: UUID) -> list[TaskListItem]:
        """Children of `task_id` that the caller participates in. Children
        the caller is not in are filtered out by Goa, regardless of whether
        the caller is in the parent.

        Each item is `{task, pending_questions}` — pending lives alongside
        the persisted Task, not nested inside it."""
        response = await self._client.get(f"/tasks/{task_id}/children")
        raise_for_response(response)
        decoded = response.json()
        return [TaskListItem.model_validate(item) for item in decoded["children"]]

    async def list_tasks(
        self,
        *,
        external_ref: str | None = None,
        role: Literal["initiator", "member"] | None = None,
        has_pending: bool | None = None,
        parent_id: UUID | None = None,
        include_top_level: bool | None = None,
    ) -> list[TaskListItem]:
        """`GET /tasks` (§9.2). Filters:
        - `external_ref`: caller-scoped lookup against the
          `(initiator_id, external_ref)` index. Returns 0 or 1 task.
        - `role`: `"initiator"` for tasks the caller initiated; `"member"`
          for ones they answer in.
        - `has_pending`: `True`/`False` to require pending pairs or none.
        - `parent_id`: list children of that parent the caller is in.
        - `include_top_level`: when `True` and `parent_id` is unset, sends
          `?parent_id=null` explicitly. Defaults to omitted (server default
          is top-level only, per spec).

        Returns `list[TaskListItem]` where each item is `{task,
        pending_questions}` — pending lives alongside the persisted Task,
        not nested inside it."""
        params: dict[str, str] = {}
        if external_ref is not None:
            params["external_ref"] = external_ref
        if role is not None:
            params["role"] = role
        if has_pending is not None:
            params["has_pending"] = "true" if has_pending else "false"
        if parent_id is not None:
            params["parent_id"] = str(parent_id)
        elif include_top_level is True:
            params["parent_id"] = "null"
        response = await self._client.get("/tasks", params=params)
        raise_for_response(response)
        decoded = response.json()
        return [TaskListItem.model_validate(item) for item in decoded["tasks"]]

    async def pending(self) -> list[PendingRow]:
        """`GET /pending` (§9.2 / §9.3) — caller's open pending pairs across
        all tasks. The unit of "what is in-flight" for this participant."""
        response = await self._client.get("/pending")
        raise_for_response(response)
        decoded = response.json()
        return [
            PendingRow(
                task_id=UUID(row["task_id"]),
                question_event_id=UUID(row["question_event_id"]),
                from_=UUID(row["from"]) if row.get("from") else None,
                created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
            )
            for row in decoded
        ]

    # ------------------------------------------------------------------
    # Memory — agent-private, cross-task key→document store
    # ------------------------------------------------------------------
    async def remember(
        self,
        key: str,
        value: Any = None,
        *,
        tags: list[str] | None = None,
    ) -> MemoryEntry:
        """`POST /memory` — store (or overwrite) a memory entry owned by this
        participant. `value` is any JSON-serializable value. Overwriting an
        existing `key` preserves its `created_at`. Memory is private to the
        caller and persists across tasks and sessions."""
        body: dict[str, Any] = {"key": key, "value": value, "tags": list(tags or [])}
        response = await self._client.post("/memory", json=body)
        raise_for_response(response)
        return MemoryEntry.model_validate(response.json())

    async def recall(self, key: str) -> MemoryEntry | None:
        """`GET /memory?key=` — fetch one entry by exact key, or `None` if the
        caller has no entry under that key."""
        response = await self._client.get("/memory", params={"key": key})
        raise_for_response(response)
        entries = response.json()["entries"]
        return MemoryEntry.model_validate(entries[0]) if entries else None

    async def recall_all(
        self,
        *,
        prefix: str | None = None,
        tags: list[str] | None = None,
    ) -> list[MemoryEntry]:
        """`GET /memory` — list the caller's entries, optionally filtered by
        key `prefix` (exact prefix, not a LIKE pattern) and/or `tags` (AND-ed).
        With no filters, returns all of the caller's memory, ordered by key."""
        params: list[tuple[str, str]] = []
        if prefix is not None:
            params.append(("prefix", prefix))
        for t in tags or ():
            params.append(("tag", t))
        response = await self._client.get("/memory", params=params)
        raise_for_response(response)
        return [MemoryEntry.model_validate(e) for e in response.json()["entries"]]

    async def forget(
        self,
        *,
        key: str | None = None,
        prefix: str | None = None,
    ) -> int:
        """`DELETE /memory` — delete one entry by exact `key`, or forget every
        entry under `prefix`. Exactly one is required. Returns the number of
        entries removed."""
        params: dict[str, str] = {}
        if key is not None:
            params["key"] = key
        if prefix is not None:
            params["prefix"] = prefix
        response = await self._client.delete("/memory", params=params)
        raise_for_response(response)
        return int(response.json()["deleted"])

    # ------------------------------------------------------------------
    # Stream
    # ------------------------------------------------------------------
    @asynccontextmanager
    async def stream(
        self, *, last_event_id: str | int | None = None,
    ) -> AsyncIterator[AsyncIterator[StreamFrame]]:
        """Open SSE stream; yields an async iterator of `StreamFrame`. Closes
        the underlying response when the context exits.

        `last_event_id` (§9.3 reconnection): if supplied, sent as the
        `Last-Event-ID` header. The hub replays any events with stream ids
        greater than `last_event_id` from its per-participant buffer; if the
        requested id predates the buffer, a `stream.gap` synthetic event is
        emitted before the replay so the caller knows to refetch via REST.
        Callers track the most recent `StreamFrame.last_event_id` they observed
        and pass it on reconnect."""
        headers: dict[str, str] = {}
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        async with self._client.stream("GET", "/stream", headers=headers) as response:
            raise_for_response(response)
            yield self._iter_frames(response)

    async def _iter_frames(self, response: httpx.Response) -> AsyncIterator[StreamFrame]:
        async for raw in iter_sse(response):
            yield self._wrap_frame(raw)

    def _wrap_frame(self, raw: SseFrame) -> StreamFrame:
        if raw.event != "event":
            return StreamFrame(
                event_name=raw.event,
                last_event_id=raw.id,
                raw=raw.data,
            )
        data = raw.data if isinstance(raw.data, dict) else {}
        return StreamFrame(
            event_name=raw.event,
            last_event_id=raw.id,
            raw=raw.data,
            task_id=UUID(data["task_id"]) if "task_id" in data else None,
            event=_EVENT_ADAPTER.validate_python(data["event"]) if "event" in data else None,
            task=TaskSummary.model_validate(data["task"]) if "task" in data else None,
        )
