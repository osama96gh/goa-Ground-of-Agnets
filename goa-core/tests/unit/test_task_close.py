"""Service-level tests for explicit task close (§8).

Wire-level (HTTP) coverage lives in `tests/integration/test_task_close_e2e.py`;
Protocol-level (in-memory + SQLite) `close_task` semantics live in
`tests/unit/test_task_log_contract.py`. This file proves the
`TaskService` orchestration: initiator-only enforcement, idempotency,
the closed-task append rejection, the external_ref slot release path
through `upsert_task`, and the `parent_closed` fan-out into children.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID, uuid4

import pytest

from goa.domain.models import (
    AnswerPayload,
    CancelAllQuestionsPayload,
    CancelQuestionPayload,
    Content,
    CreateTaskBody,
    InboundAnswer,
    InboundCancelAllQuestions,
    InboundCancelQuestion,
    InboundInfo,
    InboundQuestion,
    Participant,
    ParentClosedEvent,
    QuestionEvent,
    QuestionPayload,
    UpsertTaskBody,
    UpsertTaskOnCreate,
)
from goa.errors import ForbiddenRole, InvalidState, TaskNotFound
from goa.repos.memory import (
    InMemoryBlobStore,
    InMemoryParticipantStore,
    InMemoryTaskLog,
)
from goa.services.tasks import TaskService
from goa.stream.hub import InMemoryStreamHub


async def _bootstrap(n_extra: int = 0) -> tuple[TaskService, list[Participant], InMemoryStreamHub]:
    participants = InMemoryParticipantStore()
    task_log = InMemoryTaskLog()
    hub = InMemoryStreamHub(replay_buffer_size=100, queue_size=100)
    service = TaskService(participants, task_log, InMemoryBlobStore(), hub)

    names = ["alice", "bob", "carol", "dan", "eve"]
    out: list[Participant] = []
    for i in range(2 + n_extra):
        p = Participant(type="agent", name=names[i], api_key_hash=f"h{i}")
        await participants.create(p)
        out.append(p)
    return service, out, hub


async def _start_with_question(
    service: TaskService,
    initiator: Participant,
    targets: list[UUID],
) -> tuple[object, QuestionEvent]:
    task = await service.create_task(initiator, CreateTaskBody())
    question = cast(
        QuestionEvent,
        await service.append_event(
            initiator, task.id, InboundQuestion(payload=QuestionPayload(to=targets)),
        ),
    )
    return task, question


# ---------------------------------------------------------------------------
# Authorization + plumbing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_initiator_can_close_a_task() -> None:
    service, (alice, _bob), _ = await _bootstrap()
    task = await service.create_task(alice, CreateTaskBody())
    closed = await service.close_task(alice, task.id)
    assert closed.status == "closed"
    assert closed.id == task.id


@pytest.mark.asyncio
async def test_non_initiator_cannot_close() -> None:
    service, (alice, bob), _ = await _bootstrap()
    task = await service.create_task(alice, CreateTaskBody())
    with pytest.raises(ForbiddenRole):
        await service.close_task(bob, task.id)


@pytest.mark.asyncio
async def test_close_missing_task_raises_task_not_found() -> None:
    service, (alice, _bob), _ = await _bootstrap()
    with pytest.raises(TaskNotFound):
        await service.close_task(alice, uuid4())


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    service, (alice, _bob), _ = await _bootstrap()
    task = await service.create_task(alice, CreateTaskBody())
    first = await service.close_task(alice, task.id)
    second = await service.close_task(alice, task.id)
    assert first.id == second.id
    assert first.status == second.status == "closed"
    # updated_at on the second call is whatever the first close set —
    # the second close is a no-op, not a re-flip.
    assert first.updated_at == second.updated_at


# ---------------------------------------------------------------------------
# Closed-task append rejection — every event_type
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_append_question_rejected_after_close() -> None:
    service, (alice, bob), _ = await _bootstrap()
    task = await service.create_task(alice, CreateTaskBody())
    await service.close_task(alice, task.id)
    with pytest.raises(InvalidState):
        await service.append_event(
            alice, task.id, InboundQuestion(payload=QuestionPayload(to=[bob.id])),
        )


@pytest.mark.asyncio
async def test_append_answer_rejected_after_close() -> None:
    service, (alice, bob), _ = await _bootstrap()
    _, question = await _start_with_question(service, alice, [bob.id])
    await service.close_task(alice, question.task_id)
    with pytest.raises(InvalidState):
        await service.append_event(
            bob,
            question.task_id,
            InboundAnswer(payload=AnswerPayload(answering=[question.id])),
        )


@pytest.mark.asyncio
async def test_append_info_rejected_after_close() -> None:
    service, (alice, bob), _ = await _bootstrap()
    task, _ = await _start_with_question(service, alice, [bob.id])
    await service.close_task(alice, task.id)
    with pytest.raises(InvalidState):
        await service.append_event(
            bob, task.id, InboundInfo(content=Content(text="late")),
        )


@pytest.mark.asyncio
async def test_append_cancel_question_rejected_after_close() -> None:
    service, (alice, bob), _ = await _bootstrap()
    _, question = await _start_with_question(service, alice, [bob.id])
    await service.close_task(alice, question.task_id)
    with pytest.raises(InvalidState):
        await service.append_event(
            alice,
            question.task_id,
            InboundCancelQuestion(payload=CancelQuestionPayload(retracts=[question.id])),
        )


@pytest.mark.asyncio
async def test_append_cancel_all_questions_rejected_after_close() -> None:
    service, (alice, bob), _ = await _bootstrap()
    task, _ = await _start_with_question(service, alice, [bob.id])
    await service.close_task(alice, task.id)
    with pytest.raises(InvalidState):
        await service.append_event(
            alice,
            task.id,
            InboundCancelAllQuestions(payload=CancelAllQuestionsPayload()),
        )


# ---------------------------------------------------------------------------
# external_ref slot release
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_external_ref_slot_released_on_close() -> None:
    """Closing a task with `external_ref` frees the slot; subsequent
    upsert with the same string creates a NEW task with a different id."""
    service, (alice, _bob), _ = await _bootstrap()
    upsert_body = UpsertTaskBody(
        external_ref="thread-1",
        on_create=UpsertTaskOnCreate(),
    )
    first, created_1 = await service.upsert_task(alice, upsert_body)
    assert created_1 is True

    await service.close_task(alice, first.id)

    second, created_2 = await service.upsert_task(alice, upsert_body)
    assert created_2 is True
    assert second.id != first.id


@pytest.mark.asyncio
async def test_closed_task_still_readable() -> None:
    """Slot release must not delete or hide the closed task — `get_task`
    still returns it for audit. The closed-task row keeps its
    `external_ref` set; only the index entry was dropped."""
    service, (alice, _bob), _ = await _bootstrap()
    upsert_body = UpsertTaskBody(
        external_ref="thread-1",
        on_create=UpsertTaskOnCreate(),
    )
    task, _ = await service.upsert_task(alice, upsert_body)
    await service.close_task(alice, task.id)
    closed = await service._log.get_task(task.id)
    assert closed is not None
    assert closed.status == "closed"
    assert closed.external_ref == "thread-1"


# ---------------------------------------------------------------------------
# parent_closed fan-out into children
# ---------------------------------------------------------------------------

async def _create_child(
    service: TaskService,
    caller: Participant,
    parent_id: UUID,
) -> object:
    return await service.create_task(
        caller,
        CreateTaskBody(parent_task_id=parent_id),
    )


@pytest.mark.asyncio
async def test_open_children_receive_parent_closed() -> None:
    """Closing a parent emits a `parent_closed` event into every still-open
    child task whose participants include the closer. The closing task
    itself does not get a parent_closed event (it's not a child)."""
    service, (alice, _bob), _ = await _bootstrap()
    parent = await service.create_task(alice, CreateTaskBody())
    child_a = await _create_child(service, alice, parent.id)
    child_b = await _create_child(service, alice, parent.id)

    await service.close_task(alice, parent.id)

    log_a = await service._log.list_events_for_task(child_a.id)
    log_b = await service._log.list_events_for_task(child_b.id)
    assert any(isinstance(e, ParentClosedEvent) for e in log_a)
    assert any(isinstance(e, ParentClosedEvent) for e in log_b)
    # Payload references the parent.
    pc_a = next(e for e in log_a if isinstance(e, ParentClosedEvent))
    assert pc_a.payload.task_id == parent.id
    # The parent's own log doesn't gain a parent_closed (it's not anyone's child).
    parent_log = await service._log.list_events_for_task(parent.id)
    assert not any(isinstance(e, ParentClosedEvent) for e in parent_log)


@pytest.mark.asyncio
async def test_closed_children_are_skipped() -> None:
    """If a child is already closed when the parent closes, it does NOT
    receive a `parent_closed` event — the helper re-reads under the
    child's lock and skips non-open children."""
    service, (alice, _bob), _ = await _bootstrap()
    parent = await service.create_task(alice, CreateTaskBody())
    open_child = await _create_child(service, alice, parent.id)
    pre_closed_child = await _create_child(service, alice, parent.id)
    await service.close_task(alice, pre_closed_child.id)

    await service.close_task(alice, parent.id)

    log_open = await service._log.list_events_for_task(open_child.id)
    log_closed = await service._log.list_events_for_task(pre_closed_child.id)
    assert any(isinstance(e, ParentClosedEvent) for e in log_open)
    assert not any(isinstance(e, ParentClosedEvent) for e in log_closed)


@pytest.mark.asyncio
async def test_children_are_not_cascade_closed() -> None:
    """Children remain `status='open'` after parent close and continue
    to accept appends. The `parent_closed` signal is informational; the
    cascade is opt-in (currently not exposed)."""
    service, (alice, bob), _ = await _bootstrap()
    parent = await service.create_task(alice, CreateTaskBody())
    child = await _create_child(service, alice, parent.id)

    await service.close_task(alice, parent.id)

    fresh_child = await service._log.get_task(child.id)
    assert fresh_child is not None
    assert fresh_child.status == "open"

    # Append into the still-open child works — the close was scoped to
    # the parent.
    ev = await service.append_event(
        alice,
        child.id,
        InboundQuestion(payload=QuestionPayload(to=[bob.id])),
    )
    assert ev.event_type == "question"
