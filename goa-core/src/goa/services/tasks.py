from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from goa.domain.models import (
    AnswerEvent,
    CancelAllQuestionsEvent,
    CancelQuestionEvent,
    ChildTaskCreatedEvent,
    ChildTaskCreatedPayload,
    CreateTaskBody,
    Event,
    InboundAnswer,
    InboundCancelAllQuestions,
    InboundCancelQuestion,
    InboundEvent,
    InboundInfo,
    InboundQuestion,
    InfoEvent,
    Participant,
    ParentClosedEvent,
    ParentClosedPayload,
    ParticipantJoinedEvent,
    ParticipantJoinedPayload,
    PendingPair,
    QuestionEvent,
    Task,
    TaskSummary,
    UpsertTaskBody,
)
from goa.errors import (
    BlobForbidden,
    BlobNotFound,
    ExternalRefInUse,
    ForbiddenRole,
    InvalidEventShape,
    InvalidState,
    NotAParticipant,
    NotATarget,
    ParentTaskNotVisible,
    ParticipantUnknown,
    TaskNotFound,
)
from goa.repos.protocols import BlobStore, ParticipantStore, TaskLog
from goa.services.pending_projection import PendingProjection
from goa.stream.hub import StreamHub


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class TaskService:
    """Service surface — `create_task` + `append_event` covering the full
    §6.3 / §7 event grammar.

    Event types: `question`, `answer`, `info`, `cancel_question`,
    `cancel_all_questions` (initiator-only for question / cancel_*; any
    participant for answer / info; system-emitted for `participant_joined`).

    Owns auth-role checks, target validation, auto-join + `participant_joined`
    synthesis, cross-task `in_reply_to` validation, atomic push/pop of
    `pending_questions` under the per-task lock, and fan-out via the StreamHub.
    """

    def __init__(
        self,
        participant_store: ParticipantStore,
        task_log: TaskLog,
        blob_store: BlobStore,
        hub: StreamHub,
    ) -> None:
        self._participants = participant_store
        self._log = task_log
        self._blob_store = blob_store
        self._hub = hub
        # Stages 2+3: pending_questions is a derived view, not persisted.
        # The projection is the single in-process source of truth.
        self._pending = PendingProjection(task_log)

    async def get_pending(self, task_id: UUID) -> list[PendingPair]:
        """Public read of the pending-questions projection for a task.
        Used by read endpoints (`GET /tasks/{id}`, `GET /pending`,
        `GET /tasks?has_pending=...`)."""
        return await self._pending.get(task_id)

    async def list_tasks_for_participant(
        self,
        participant_id: UUID,
        *,
        role: str | None = None,
        has_pending: bool | None = None,
        parent_id: UUID | None = None,
        top_level_only: bool = True,
    ) -> list[tuple[Task, list[PendingPair]]]:
        """§9.2 list — fetches tasks from the log, hydrates pending pairs from
        the projection, and post-filters by `has_pending` (which is no longer
        a repo concern after Stages 2+3)."""
        tasks = await self._log.list_tasks_for_participant(
            participant_id,
            role=role,
            parent_id=parent_id,
            top_level_only=top_level_only,
        )
        items: list[tuple[Task, list[PendingPair]]] = []
        for t in tasks:
            pending = await self._pending.get(t.id)
            if has_pending is True and not pending:
                continue
            if has_pending is False and pending:
                continue
            items.append((t, pending))
        return items

    async def list_tasks(
        self,
        *,
        has_pending: bool | None = None,
        parent_id: UUID | None = None,
        top_level_only: bool = True,
    ) -> list[tuple[Task, list[PendingPair]]]:
        """Admin-scoped variant — every task in the store, no participant gate."""
        tasks = await self._log.list_tasks(
            parent_id=parent_id, top_level_only=top_level_only,
        )
        items: list[tuple[Task, list[PendingPair]]] = []
        for t in tasks:
            pending = await self._pending.get(t.id)
            if has_pending is True and not pending:
                continue
            if has_pending is False and pending:
                continue
            items.append((t, pending))
        return items

    # ------------------------------------------------------------------
    # POST /tasks
    # ------------------------------------------------------------------
    async def create_task(self, caller: Participant, body: CreateTaskBody) -> Task:
        """Create a task header. Task creation is decoupled from any event:
        the first event flows through `POST /tasks/{id}/events` like every
        subsequent event. An empty task is a valid state, visible only to
        the initiator until the first question event auto-joins targets."""
        # Resolve the parent up-front: existence + caller membership both
        # collapse into a single 403 so we never leak whether the id was
        # wrong or the caller simply wasn't in it.
        parent: Task | None = None
        if body.parent_task_id is not None:
            parent = await self._log.get_task(body.parent_task_id)
            if parent is None or caller.id not in parent.participants:
                raise ParentTaskNotVisible()

        task = Task(
            initiator_id=caller.id,
            parent_task_id=body.parent_task_id,
            participants=[caller.id],
            subject=body.subject,
            external_ref=body.external_ref,
            metadata=dict(body.metadata),
        )

        # Create the task and reserve the external_ref slot atomically.
        # A collision raises ExternalRefInUse and no task is persisted.
        await self._log.create_task(task, external_ref=body.external_ref)

        # Parent-side `child_task_created` fires the moment the child exists.
        # Under the new ordering a participant in both tasks learns "a child
        # appeared" before the child has any content; the child's first
        # event arrives later via /tasks/{id}/events and fans out separately.
        await self._emit_child_task_created(parent, task, caller, body.subject)

        return task

    # ------------------------------------------------------------------
    # POST /tasks/upsert
    # ------------------------------------------------------------------
    async def upsert_task(
        self, caller: Participant, body: UpsertTaskBody,
    ) -> tuple[Task, bool]:
        """Find-or-create keyed on `(caller.id, external_ref)` per §9.2.

        On hit returns `(existing_task, False)`; on miss delegates to
        `create_task` and returns `(new_task, True)`. No event is emitted
        in either branch.

        Concurrency: two upserts with the same `(caller.id, external_ref)`
        both read None from the index, both attempt `create_task` — one wins,
        the other catches `ExternalRefInUse` and re-reads the index to
        return the winner's task. The retry path is bounded (one extra
        lookup) and observably identical to the spec's serialized model."""
        existing_id = await self._log.get_task_by_external_ref(caller.id, body.external_ref)
        if existing_id is not None:
            existing = await self._log.get_task(existing_id)
            # The index never points at a missing task in v2 (no close,
            # no GC). If we ever see this, the index is corrupt.
            assert existing is not None, "external_ref index points at missing task"
            return existing, False

        create_body = CreateTaskBody(
            subject=body.on_create.subject,
            parent_task_id=body.on_create.parent_task_id,
            external_ref=body.external_ref,
            metadata=body.on_create.metadata,
        )
        try:
            task = await self.create_task(caller, create_body)
        except ExternalRefInUse:
            # Concurrent upsert won the race — re-read and return the winner.
            existing_id = await self._log.get_task_by_external_ref(caller.id, body.external_ref)
            assert existing_id is not None, "ExternalRefInUse but no index entry"
            existing = await self._log.get_task(existing_id)
            assert existing is not None, "external_ref index points at missing task"
            return existing, False
        return task, True

    # ------------------------------------------------------------------
    # POST /tasks/{id}/events
    # ------------------------------------------------------------------
    async def append_event(
        self,
        caller: Participant,
        task_id: UUID,
        body: InboundEvent,
    ) -> Event:
        task = await self._log.get_task(task_id)
        if task is None:
            raise TaskNotFound()
        if caller.id not in task.participants:
            # 403 not_a_participant on writes (404 is the read-side rule).
            raise NotAParticipant()

        # Attachments must already be bound to this task at upload time.
        # No linkage step — binding is immutable in the blob row.
        await self._validate_attachments_for_task(task.id, body)

        if isinstance(body, InboundQuestion):
            return await self._append_question(caller, task, body)
        if isinstance(body, InboundAnswer):
            return await self._append_answer(caller, task, body)
        if isinstance(body, InboundInfo):
            return await self._append_info(caller, task, body)
        if isinstance(body, InboundCancelQuestion):
            return await self._append_cancel_question(caller, task, body)
        if isinstance(body, InboundCancelAllQuestions):
            return await self._append_cancel_all_questions(caller, task, body)
        # Defense-in-depth — the InboundEvent discriminated union should be
        # exhaustive, but if a new variant lands without a handler we fail loud.
        raise InvalidEventShape("unsupported event_type")

    # ------------------------------------------------------------------
    # POST /tasks/{id}/close
    # ------------------------------------------------------------------
    async def close_task(self, caller: Participant, task_id: UUID) -> Task:
        """Initiator-only close (§8). Transitions the task to `closed`,
        releases its `external_ref` slot, and emits a `parent_closed`
        system event into every still-open child. Idempotent: a second
        close call returns the existing closed task.

        Lock discipline: the parent lock is held only across the status
        flip. We snapshot the child list before the lock and emit
        `parent_closed` into each one *after* releasing the parent lock,
        each under its own child lock. This avoids a nested-lock
        deadlock with concurrent grandchild creation, which holds the
        child lock and reaches up to the parent for `child_task_created`.
        """
        task = await self._log.get_task(task_id)
        if task is None:
            raise TaskNotFound()
        if caller.id != task.initiator_id:
            raise ForbiddenRole()
        return await self._close_task(task)

    async def admin_close_task(self, task_id: UUID) -> Task:
        """Operator close — same lifecycle as `close_task` (status flip,
        external_ref release, `parent_closed` fan-out, idempotency) but
        **without** the initiator role check. Backs `POST /admin/tasks/{id}/close`,
        where the deployment admin token already grants authority to act on
        any task. Event writes still preserve the §6.3 `from` invariant —
        `parent_closed` is a system event (`from_=None`), so no impersonation.
        """
        task = await self._log.get_task(task_id)
        if task is None:
            raise TaskNotFound()
        return await self._close_task(task)

    async def _close_task(self, task: Task) -> Task:
        # Shared close mechanics for the initiator and admin entry points.
        # Snapshot children *before* the parent lock. We don't need a
        # frozen view — a child created concurrently with this close
        # will observe the parent's `status='closed'` via `GET /tasks/{id}`
        # whenever it cares. `parent_closed` is a best-effort live signal.
        children = await self._log.list_children(task.id)

        async with self._log.lock(task.id):
            if task.status == "closed":
                # Re-read in case status flipped between get_task and lock.
                fresh = await self._log.get_task(task.id)
                return fresh if fresh is not None else task
            task = await self._log.close_task(task.id)

        # Parent lock released — fan out to open children one at a time.
        for child in children:
            if child.status != "open":
                continue
            await self._emit_parent_closed(child, parent_id=task.id)

        return task

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    async def _assert_open(self, task_id: UUID) -> None:
        """Re-read the task under the caller's per-task lock and raise
        `InvalidState` (409) if it has been closed. Called inside each
        `_append_*` handler's lock block so the close-vs-append race
        commits at most one side — without the in-lock re-check, a
        racing close could let one event slip through after the status
        flip."""
        fresh = await self._log.get_task(task_id)
        if fresh is not None and fresh.status == "closed":
            raise InvalidState("cannot append to a closed task")

    async def _idempotent_replay(
        self,
        task_id: UUID,
        caller_id: UUID,
        body: InboundEvent,
    ) -> Event | None:
        """Idempotency short-circuit (§13). Returns the previously-persisted
        event for `body.client_event_id`, or `None` if no key was supplied
        or no prior event exists.

        **Caller contract:** must be invoked inside `self._log.lock(task_id)`
        — that lock is what makes the check-and-insert atomic. The dedup key
        is `(task_id, caller_id, body.client_event_id)`; clients own that
        keyspace (no body-hash conflict detection in v2)."""
        if body.client_event_id is None:
            return None
        return await self._log.find_event_by_client_id(
            task_id, caller_id, body.client_event_id,
        )

    async def _append_question(
        self,
        caller: Participant,
        task: Task,
        body: InboundQuestion,
    ) -> Event:
        if caller.id != task.initiator_id:
            raise ForbiddenRole()

        await self._validate_in_reply_to(task, body.in_reply_to)
        targets = list(body.payload.to)
        await self._assert_targets_known(targets)

        async with self._log.lock(task.id):
            # Idempotency: if this `(task, caller, client_event_id)` already
            # has a persisted event, return it without re-running pending
            # mutation, auto-join, or fanout. Pre-lock work above (role check,
            # in_reply_to validation, target resolution) is duplicated on retry
            # — that's the price of keeping the check-and-insert atomic.
            prior = await self._idempotent_replay(task.id, caller.id, body)
            if prior is not None:
                return prior
            await self._assert_open(task.id)

            # `_auto_join` mutates `task.participants` in-place — that's
            # what makes the upcoming `_fanout` TaskSummary correct without
            # a re-fetch. `add_participants` then persists the same growth
            # to the underlying store; in-memory backends no-op (already
            # mutated via reference), persistent backends INSERT the rows.
            # Same dual-write pattern below for `last_activity_at`.
            joined_events = self._auto_join(task, targets)
            for ev in joined_events:
                await self._log.append_event(ev)
                await self._pending.apply(ev)  # no-op; keeps cache warm
            if joined_events:
                await self._log.add_participants(
                    task.id, [ev.payload.participant_id for ev in joined_events],
                )

            question = QuestionEvent(
                task_id=task.id,
                from_=caller.id,
                content=body.content,
                in_reply_to=body.in_reply_to,
                metadata=dict(body.metadata),
                payload=body.payload,
                client_event_id=body.client_event_id,
            )
            await self._log.append_event(question)
            await self._pending.apply(question)  # pushes (question.id, t) per target

            now = _now()
            task.last_activity_at = now
            task.updated_at = now
            await self._log.touch_task(task.id, now)

            await self._fanout(task, [*joined_events, question])

        return question

    async def _append_answer(
        self,
        caller: Participant,
        task: Task,
        body: InboundAnswer,
    ) -> Event:
        await self._validate_in_reply_to(task, body.in_reply_to)
        # Validate every referenced question exists, is in this task, and
        # targets the sender. No partial application — first miss aborts.
        log = await self._log.list_events_for_task(task.id)
        by_id = {ev.id: ev for ev in log}
        for qid in body.payload.answering:
            ev = by_id.get(qid)
            if ev is None or not isinstance(ev, QuestionEvent):
                raise NotATarget("answer references a non-question or unknown event id")
            if caller.id not in ev.payload.to:
                raise NotATarget()

        async with self._log.lock(task.id):
            prior = await self._idempotent_replay(task.id, caller.id, body)
            if prior is not None:
                return prior
            await self._assert_open(task.id)

            answer = AnswerEvent(
                task_id=task.id,
                from_=caller.id,
                content=body.content,
                in_reply_to=body.in_reply_to,
                metadata=dict(body.metadata),
                payload=body.payload,
                client_event_id=body.client_event_id,
            )
            await self._log.append_event(answer)
            # First-answer-wins per target: PendingProjection.apply drops
            # only (qid, caller.id) pairs for qids in payload.answering.
            await self._pending.apply(answer)

            now = _now()
            task.last_activity_at = now
            task.updated_at = now
            await self._log.touch_task(task.id, now)

            await self._fanout(task, [answer])

        return answer

    async def _append_info(
        self,
        caller: Participant,
        task: Task,
        body: InboundInfo,
    ) -> Event:
        # Any participant may emit. No role check, no target validation,
        # no pending-state mutation — info events are pure log entries.
        await self._validate_in_reply_to(task, body.in_reply_to)

        async with self._log.lock(task.id):
            prior = await self._idempotent_replay(task.id, caller.id, body)
            if prior is not None:
                return prior
            await self._assert_open(task.id)

            info = InfoEvent(
                task_id=task.id,
                from_=caller.id,
                content=body.content,
                in_reply_to=body.in_reply_to,
                metadata=dict(body.metadata),
                payload=body.payload,
                client_event_id=body.client_event_id,
            )
            await self._log.append_event(info)
            # apply() is a no-op on info, but it primes the projection cache
            # so _fanout's get() never hits the cold-rebuild path inside the lock.
            await self._pending.apply(info)

            now = _now()
            task.last_activity_at = now
            task.updated_at = now
            await self._log.touch_task(task.id, now)

            await self._fanout(task, [info])

        return info

    async def _append_cancel_question(
        self,
        caller: Participant,
        task: Task,
        body: InboundCancelQuestion,
    ) -> Event:
        if caller.id != task.initiator_id:
            raise ForbiddenRole()

        await self._validate_in_reply_to(task, body.in_reply_to)
        # Each retracted id must reference a `question` event in *this* task.
        log = await self._log.list_events_for_task(task.id)
        by_id = {ev.id: ev for ev in log}
        for qid in body.payload.retracts:
            ev = by_id.get(qid)
            if ev is None or not isinstance(ev, QuestionEvent):
                raise InvalidEventShape(
                    "cancel_question.retracts references a non-question or unknown event id",
                )
            if ev.task_id != task.id:
                raise InvalidEventShape(
                    "cancel_question.retracts references an event in a different task",
                )

        async with self._log.lock(task.id):
            prior = await self._idempotent_replay(task.id, caller.id, body)
            if prior is not None:
                return prior
            await self._assert_open(task.id)

            cancel = CancelQuestionEvent(
                task_id=task.id,
                from_=caller.id,
                content=body.content,
                in_reply_to=body.in_reply_to,
                metadata=dict(body.metadata),
                payload=body.payload,
                client_event_id=body.client_event_id,
            )
            await self._log.append_event(cancel)
            # PendingProjection.apply drops every (qid, *) pair for each retracted
            # question id — task-wide per question (not target-scoped).
            await self._pending.apply(cancel)

            now = _now()
            task.last_activity_at = now
            task.updated_at = now
            await self._log.touch_task(task.id, now)

            await self._fanout(task, [cancel])

        return cancel

    async def _append_cancel_all_questions(
        self,
        caller: Participant,
        task: Task,
        body: InboundCancelAllQuestions,
    ) -> Event:
        if caller.id != task.initiator_id:
            raise ForbiddenRole()

        await self._validate_in_reply_to(task, body.in_reply_to)

        async with self._log.lock(task.id):
            prior = await self._idempotent_replay(task.id, caller.id, body)
            if prior is not None:
                return prior
            await self._assert_open(task.id)

            cancel_all = CancelAllQuestionsEvent(
                task_id=task.id,
                from_=caller.id,
                content=body.content,
                in_reply_to=body.in_reply_to,
                metadata=dict(body.metadata),
                payload=body.payload,
                client_event_id=body.client_event_id,
            )
            await self._log.append_event(cancel_all)
            # PendingProjection.apply atomically clears pending for this task.
            await self._pending.apply(cancel_all)

            now = _now()
            task.last_activity_at = now
            task.updated_at = now
            await self._log.touch_task(task.id, now)

            await self._fanout(task, [cancel_all])

        return cancel_all

    async def _validate_attachments_for_task(
        self,
        task_id: UUID,
        body: InboundEvent,
    ) -> None:
        """Each attachment must reference a blob bound to `task_id`.

        - Unknown blob_id → `BlobNotFound` (404).
        - Blob bound to a different task → `BlobForbidden` (403) — cross-task
          reuse is forbidden by spec §6.5.

        Runs before the lock + append so an invalid reference aborts cleanly
        with no side effects on the event log."""
        for att in body.content.attachments:
            bound = await self._blob_store.get_task_id(att.blob_id)
            if bound is None:
                raise BlobNotFound(
                    f"attachment references unknown blob {att.blob_id}",
                )
            if bound != task_id:
                raise BlobForbidden(
                    f"blob {att.blob_id} is bound to a different task",
                )

    async def is_blob_visible(self, caller: Participant, blob_id: UUID) -> bool:
        """Authorize a blob download. Visible iff caller is a participant
        of the blob's bound task. One column read; no link table. The
        uploader retains access by virtue of being a participant of the
        bound task — there is no separate owner-access path."""
        task_id = await self._blob_store.get_task_id(blob_id)
        if task_id is None:
            return False
        task = await self._log.get_task(task_id)
        return task is not None and caller.id in task.participants

    async def _validate_in_reply_to(self, task: Task, in_reply_to: UUID | None) -> None:
        if in_reply_to is None:
            return
        log = await self._log.list_events_for_task(task.id)
        for ev in log:
            if ev.id == in_reply_to:
                # list_events_for_task is task-scoped, so existence implies same-task.
                return
        raise InvalidEventShape("in_reply_to references an event not in this task")

    def _auto_join(self, task: Task, targets: list[UUID]) -> list[ParticipantJoinedEvent]:
        joined: list[ParticipantJoinedEvent] = []
        seen: set[UUID] = set()
        for t in targets:
            if t in task.participants or t in seen:
                continue
            seen.add(t)
            task.participants.append(t)
            ev = ParticipantJoinedEvent(
                task_id=task.id,
                from_=None,
                payload=ParticipantJoinedPayload(participant_id=t),
            )
            joined.append(ev)
        return joined

    async def _assert_targets_known(self, targets: list[UUID]) -> None:
        for pid in targets:
            if await self._participants.get(pid) is None:
                raise ParticipantUnknown(f"participant {pid} is not registered")

    async def _emit_child_task_created(
        self,
        parent: Task | None,
        child: Task,
        caller: Participant,
        subject: str,
    ) -> None:
        # Only spawned children produce a parent-side system event; root
        # tasks (parent is None) are no-ops here. The child has no events
        # at this point — `child_task_created` is the signal that the child
        # exists, full stop. Child-side events arrive later via
        # /tasks/{id}/events and fan out on the child stream.
        if parent is None:
            return
        async with self._log.lock(parent.id):
            evt = ChildTaskCreatedEvent(
                task_id=parent.id,
                from_=None,
                payload=ChildTaskCreatedPayload(
                    task_id=child.id,
                    spawned_by=caller.id,
                    # Spec §6.3 lists `subject?:`; map empty/unset to None
                    # so the wire shape distinguishes "no subject" from "".
                    subject=subject or None,
                ),
            )
            await self._log.append_event(evt)
            # apply() is a no-op on child_task_created, but warms the
            # parent's projection cache so _fanout's get() stays inside cache.
            await self._pending.apply(evt)
            now = _now()
            parent.last_activity_at = now
            parent.updated_at = now
            await self._log.touch_task(parent.id, now)
            await self._fanout(parent, [evt])

    async def _emit_parent_closed(self, child: Task, *, parent_id: UUID) -> None:
        # Mirror of `_emit_child_task_created` for the opposite direction:
        # we emit a system event into a related task whose lock we don't
        # already hold. We acquire the child's lock, re-read under it to
        # avoid emitting into a child that closed concurrently, append,
        # apply (no-op for parent_closed; warms cache), touch, and fan
        # out — same five steps in the same order.
        async with self._log.lock(child.id):
            fresh = await self._log.get_task(child.id)
            if fresh is None or fresh.status != "open":
                return
            evt = ParentClosedEvent(
                task_id=child.id,
                from_=None,
                payload=ParentClosedPayload(task_id=parent_id),
            )
            await self._log.append_event(evt)
            await self._pending.apply(evt)
            now = _now()
            fresh.last_activity_at = now
            fresh.updated_at = now
            await self._log.touch_task(child.id, now)
            await self._fanout(fresh, [evt])

    async def _fanout(self, task: Task, events: list[Event]) -> None:
        # Pending is derived (Stages 2+3). The projection cache is warm here
        # because every event-emit path calls self._pending.apply() inside
        # this same lock immediately before fanout — get() returns from cache
        # without trying to re-acquire the lock.
        pending = await self._pending.get(task.id)
        summary = TaskSummary.from_state(task, pending)
        for ev in events:
            frame = {
                "task_id": str(task.id),
                "event": ev.model_dump(mode="json", by_alias=True),
                "task": summary.model_dump(mode="json"),
            }
            for participant_id in task.participants:
                await self._hub.publish(participant_id, "event", frame)
