"""Service-level tests for idempotent event append (§8 Design Decisions).

Wire-level / Protocol-level coverage lives in `test_task_log_contract.py`,
which exercises `find_event_by_client_id` against both `InMemoryTaskLog`
and `SqliteAdapter`. This file proves that `TaskService.append_event`
honors the dedup short-circuit for every event-type handler and that
the side effects of a retry (pending state, fanout) are also one-shot.
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
    QuestionEvent,
    QuestionPayload,
)
from goa.repos.memory import (
    InMemoryBlobStore,
    InMemoryParticipantStore,
    InMemoryTaskLog,
)
from goa.services.tasks import TaskService
from goa.stream.hub import InMemoryStreamHub


async def _bootstrap(n_extra: int = 0) -> tuple[TaskService, list[Participant], InMemoryStreamHub]:
    """Mirrors the helper in `test_event_grammar.py` but also returns the
    hub so fanout-counting tests can inspect delivery."""
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
# Happy path — every handler short-circuits on a repeat key
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_repeat_question_returns_same_event() -> None:
    """Retried `question` with the same key returns the original event —
    same id, seq, created_at."""
    service, (alice, bob), _ = await _bootstrap()
    task = await service.create_task(alice, CreateTaskBody())
    key = uuid4()
    body = InboundQuestion(
        payload=QuestionPayload(to=[bob.id]),
        client_event_id=key,
    )

    first = await service.append_event(alice, task.id, body)
    second = await service.append_event(alice, task.id, body)

    assert first.id == second.id
    assert first.seq == second.seq
    assert first.created_at == second.created_at
    assert first.client_event_id == key


@pytest.mark.asyncio
async def test_repeat_answer_returns_same_event() -> None:
    service, (alice, bob), _ = await _bootstrap()
    _, question = await _start_with_question(service, alice, [bob.id])
    key = uuid4()
    body = InboundAnswer(
        payload=AnswerPayload(answering=[question.id]),
        client_event_id=key,
    )

    first = await service.append_event(bob, question.task_id, body)
    second = await service.append_event(bob, question.task_id, body)

    assert first.id == second.id


@pytest.mark.asyncio
async def test_repeat_info_returns_same_event() -> None:
    service, (alice, bob), _ = await _bootstrap()
    task, _ = await _start_with_question(service, alice, [bob.id])
    key = uuid4()
    body = InboundInfo(content=Content(text="still checking"), client_event_id=key)

    first = await service.append_event(bob, task.id, body)
    second = await service.append_event(bob, task.id, body)

    assert first.id == second.id


@pytest.mark.asyncio
async def test_repeat_cancel_question_returns_same_event() -> None:
    service, (alice, bob), _ = await _bootstrap()
    _, question = await _start_with_question(service, alice, [bob.id])
    key = uuid4()
    body = InboundCancelQuestion(
        payload=CancelQuestionPayload(retracts=[question.id]),
        client_event_id=key,
    )

    first = await service.append_event(alice, question.task_id, body)
    second = await service.append_event(alice, question.task_id, body)

    assert first.id == second.id


@pytest.mark.asyncio
async def test_repeat_cancel_all_questions_returns_same_event() -> None:
    service, (alice, bob), _ = await _bootstrap()
    task, _ = await _start_with_question(service, alice, [bob.id])
    key = uuid4()
    body = InboundCancelAllQuestions(
        payload=CancelAllQuestionsPayload(),
        client_event_id=key,
    )

    first = await service.append_event(alice, task.id, body)
    second = await service.append_event(alice, task.id, body)

    assert first.id == second.id


# ---------------------------------------------------------------------------
# Back-compat — no key means no dedup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_key_means_distinct_events() -> None:
    """Two appends without `client_event_id` produce two distinct events.
    Opting out of idempotency is the default."""
    service, (alice, bob), _ = await _bootstrap()
    task = await service.create_task(alice, CreateTaskBody())
    body = InboundQuestion(payload=QuestionPayload(to=[bob.id]))

    first = await service.append_event(alice, task.id, body)
    second = await service.append_event(alice, task.id, body)

    assert first.id != second.id


@pytest.mark.asyncio
async def test_different_keys_means_distinct_events() -> None:
    service, (alice, bob), _ = await _bootstrap()
    task = await service.create_task(alice, CreateTaskBody())
    first_body = InboundQuestion(
        payload=QuestionPayload(to=[bob.id]),
        client_event_id=uuid4(),
    )
    second_body = InboundQuestion(
        payload=QuestionPayload(to=[bob.id]),
        client_event_id=uuid4(),
    )

    first = await service.append_event(alice, task.id, first_body)
    second = await service.append_event(alice, task.id, second_body)

    assert first.id != second.id


# ---------------------------------------------------------------------------
# Side-effect dedup — pending state and fanout must be one-shot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_repeat_question_does_not_double_push_pending() -> None:
    """A duplicate `question` append must not push the same pending pair
    twice — otherwise an answer would have to be applied twice to close
    the question."""
    service, (alice, bob), _ = await _bootstrap()
    task = await service.create_task(alice, CreateTaskBody())
    key = uuid4()
    body = InboundQuestion(
        payload=QuestionPayload(to=[bob.id]),
        client_event_id=key,
    )

    first = await service.append_event(alice, task.id, body)
    await service.append_event(alice, task.id, body)

    pending = await service.get_pending(task.id)
    assert pending == [(first.id, bob.id)]


@pytest.mark.asyncio
async def test_repeat_question_does_not_double_fanout() -> None:
    """Retry must not deliver a duplicate frame to live SSE subscribers.
    We count the events delivered to bob's per-participant replay buffer."""
    service, (alice, bob), hub = await _bootstrap()
    task = await service.create_task(alice, CreateTaskBody())
    key = uuid4()
    body = InboundQuestion(
        payload=QuestionPayload(to=[bob.id]),
        client_event_id=key,
    )

    await service.append_event(alice, task.id, body)
    await service.append_event(alice, task.id, body)

    # The hub keeps a per-participant replay buffer. Count `event` frames
    # delivered to bob — should be 2: one `participant_joined` (auto-join
    # on first call) and one `question`. The retry must add nothing.
    buf = hub.buffer_snapshot(bob.id)
    event_frames = [f for f in buf if f.event == "event"]
    assert len(event_frames) == 2
    types = [f.data["event"]["event_type"] for f in event_frames]
    assert types == ["participant_joined", "question"]


@pytest.mark.asyncio
async def test_repeat_question_does_not_double_log() -> None:
    """The append-only log must not gain a second copy of the event on
    retry. One row in, one row out (plus the auto-join system event)."""
    service, (alice, bob), _ = await _bootstrap()
    task = await service.create_task(alice, CreateTaskBody())
    key = uuid4()
    body = InboundQuestion(
        payload=QuestionPayload(to=[bob.id]),
        client_event_id=key,
    )

    await service.append_event(alice, task.id, body)
    await service.append_event(alice, task.id, body)

    log = await service._log.list_events_for_task(task.id)
    types = [ev.event_type for ev in log]
    assert types == ["participant_joined", "question"]
