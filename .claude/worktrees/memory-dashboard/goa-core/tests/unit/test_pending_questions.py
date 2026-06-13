from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest

from goa.domain.models import (
    AnswerPayload,
    Content,
    CreateTaskBody,
    InboundAnswer,
    InboundQuestion,
    Participant,
    QuestionEvent,
    QuestionPayload,
)
from goa.errors import (
    ForbiddenRole,
    NotATarget,
    ParticipantUnknown,
)
from goa.repos.memory import (
    InMemoryBlobStore,
    InMemoryParticipantStore,
    InMemoryTaskLog,
)
from goa.services.tasks import TaskService
from goa.stream.hub import InMemoryStreamHub


async def _bootstrap() -> tuple[TaskService, Participant, Participant]:
    participants = InMemoryParticipantStore()
    task_log = InMemoryTaskLog()
    hub = InMemoryStreamHub(replay_buffer_size=100, queue_size=100)
    service = TaskService(participants, task_log, InMemoryBlobStore(), hub)

    alice = Participant(type="agent", name="alice", api_key_hash="ha")
    bob = Participant(type="agent", name="bob", api_key_hash="hb")
    await participants.create(alice)
    await participants.create(bob)
    return service, alice, bob


async def _start_with_question(
    service: TaskService, initiator: Participant, targets: list,
) -> tuple[object, QuestionEvent]:
    task = await service.create_task(initiator, CreateTaskBody())
    question = cast(
        QuestionEvent,
        await service.append_event(
            initiator, task.id, InboundQuestion(payload=QuestionPayload(to=targets)),
        ),
    )
    return task, question


@pytest.mark.asyncio
async def test_question_pushes_pending_pair() -> None:
    service, alice, bob = await _bootstrap()
    task, question = await _start_with_question(service, alice, [bob.id])
    assert await service.get_pending(task.id) == [(question.id, bob.id)]
    assert bob.id in task.participants


@pytest.mark.asyncio
async def test_answer_pops_pending_pair() -> None:
    service, alice, bob = await _bootstrap()
    task, question = await _start_with_question(service, alice, [bob.id])

    answer = InboundAnswer(payload=AnswerPayload(answering=[question.id]))
    await service.append_event(bob, task.id, answer)
    assert await service.get_pending(task.id) == []


@pytest.mark.asyncio
async def test_first_answer_wins_second_does_not_reopen() -> None:
    service, alice, bob = await _bootstrap()
    task, question = await _start_with_question(service, alice, [bob.id])

    answer = InboundAnswer(payload=AnswerPayload(answering=[question.id]))
    await service.append_event(bob, task.id, answer)
    assert await service.get_pending(task.id) == []

    # Second answer references the same question — appended, but does not reopen.
    second = InboundAnswer(payload=AnswerPayload(answering=[question.id]))
    await service.append_event(bob, task.id, second)
    assert await service.get_pending(task.id) == []


@pytest.mark.asyncio
async def test_answer_referencing_non_target_raises_not_a_target() -> None:
    service, alice, bob = await _bootstrap()
    # alice creates a task targeting bob
    task, question = await _start_with_question(service, alice, [bob.id])

    # alice (not in payload.to) attempts to answer her own question
    answer = InboundAnswer(payload=AnswerPayload(answering=[question.id]))
    with pytest.raises(NotATarget):
        await service.append_event(alice, task.id, answer)

    # No state was changed.
    assert await service.get_pending(task.id) == [(question.id, bob.id)]


@pytest.mark.asyncio
async def test_non_initiator_cannot_emit_question() -> None:
    service, alice, bob = await _bootstrap()
    task, _ = await _start_with_question(service, alice, [bob.id])

    # bob (non-initiator) tries to emit a follow-up question
    follow_up = InboundQuestion(
        payload=QuestionPayload(to=[alice.id]),
        content=Content(text="hello back?"),
    )
    with pytest.raises(ForbiddenRole):
        await service.append_event(bob, task.id, follow_up)


@pytest.mark.asyncio
async def test_question_targeting_unknown_participant_raises() -> None:
    """Target validation happens on `append_event(question)`, not on
    `create_task`. The task is created (empty) first, then the bad question
    is appended and rejected."""
    service, alice, _ = await _bootstrap()
    ghost = uuid4()
    task = await service.create_task(alice, CreateTaskBody())
    with pytest.raises(ParticipantUnknown):
        await service.append_event(
            alice, task.id, InboundQuestion(payload=QuestionPayload(to=[ghost])),
        )


@pytest.mark.asyncio
async def test_auto_join_emits_participant_joined_before_question() -> None:
    service, alice, bob = await _bootstrap()
    task, question = await _start_with_question(service, alice, [bob.id])

    log = await service._log.list_events_for_task(task.id)
    types = [ev.event_type for ev in log]
    assert types == ["participant_joined", "question"]
    assert log[0].payload.participant_id == bob.id  # type: ignore[union-attr]
    assert log[1].id == question.id
    assert bob.id in task.participants
