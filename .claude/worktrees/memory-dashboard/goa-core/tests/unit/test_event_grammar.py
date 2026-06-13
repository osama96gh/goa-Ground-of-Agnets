from __future__ import annotations

import asyncio
from typing import cast
from uuid import UUID, uuid4

import pytest

from goa.domain.models import (
    AnswerEvent,
    AnswerPayload,
    CancelAllQuestionsEvent,
    CancelQuestionEvent,
    CancelQuestionPayload,
    Content,
    CreateTaskBody,
    Event,
    InboundAnswer,
    InboundCancelAllQuestions,
    InboundCancelQuestion,
    InboundInfo,
    InboundQuestion,
    InfoEvent,
    InfoPayload,
    Participant,
    QuestionEvent,
    QuestionPayload,
)
from goa.errors import (
    ForbiddenRole,
    InvalidEventShape,
    NotATarget,
)
from goa.repos.memory import (
    InMemoryBlobStore,
    InMemoryParticipantStore,
    InMemoryTaskLog,
)
from goa.services.tasks import TaskService
from goa.stream.hub import InMemoryStreamHub


async def _bootstrap(n_extra: int = 0) -> tuple[TaskService, list[Participant]]:
    """Spin up a fresh in-memory service with `2 + n_extra` participants.

    Returned list is `[alice (initiator), bob, carol, dan, ...]`.
    """
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
    return service, out


async def _start_with_question(
    service: TaskService,
    initiator: Participant,
    targets: list[UUID],
) -> tuple[object, QuestionEvent]:
    """Helper — create an empty task then append a first question."""
    task = await service.create_task(initiator, CreateTaskBody())
    question = cast(
        QuestionEvent,
        await service.append_event(
            initiator, task.id, InboundQuestion(payload=QuestionPayload(to=targets)),
        ),
    )
    return task, question


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_info_any_participant_no_pending_change() -> None:
    service, (alice, bob) = await _bootstrap()
    task, question = await _start_with_question(service, alice, [bob.id])

    # bob (non-initiator, but a participant) emits info — allowed.
    info = InboundInfo(content=Content(text="checking another specialist"))
    ev = await service.append_event(bob, task.id, info)

    assert ev.event_type == "info"
    # pending unchanged
    assert await service.get_pending(task.id) == [(question.id, bob.id)]


@pytest.mark.asyncio
async def test_info_in_reply_to_same_task_accepted() -> None:
    service, (alice, bob) = await _bootstrap()
    task, question = await _start_with_question(service, alice, [bob.id])

    # bob emits info threading off the question — accepted.
    info = InboundInfo(in_reply_to=question.id, content=Content(text="re: your q"))
    ev = await service.append_event(bob, task.id, info)
    assert ev.in_reply_to == question.id


@pytest.mark.asyncio
async def test_info_in_reply_to_unknown_event_rejected() -> None:
    service, (alice, bob) = await _bootstrap()
    task, _ = await _start_with_question(service, alice, [bob.id])

    info = InboundInfo(in_reply_to=uuid4())
    with pytest.raises(InvalidEventShape):
        await service.append_event(bob, task.id, info)


# ---------------------------------------------------------------------------
# cancel_question
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_question_happy_path_pops_one_keeps_others() -> None:
    service, (alice, bob, carol) = await _bootstrap(n_extra=1)
    # alice opens two separate questions, one to bob and one to carol.
    task, q1 = await _start_with_question(service, alice, [bob.id])
    q2_evt = await service.append_event(
        alice, task.id, InboundQuestion(payload=QuestionPayload(to=[carol.id])),
    )
    q2 = cast(QuestionEvent, q2_evt)

    # Cancel q1 only — q2's pair stays.
    cancel = InboundCancelQuestion(payload=CancelQuestionPayload(retracts=[q1.id]))
    await service.append_event(alice, task.id, cancel)
    assert await service.get_pending(task.id) == [(q2.id, carol.id)]


@pytest.mark.asyncio
async def test_cancel_question_is_task_wide_per_question_id() -> None:
    """Multi-target question + cancel_question on that question id pops every pair."""
    service, (alice, bob, carol) = await _bootstrap(n_extra=1)
    task, q = await _start_with_question(service, alice, [bob.id, carol.id])
    assert len(await service.get_pending(task.id)) == 2

    await service.append_event(
        alice,
        task.id,
        InboundCancelQuestion(payload=CancelQuestionPayload(retracts=[q.id])),
    )
    # Both (q, bob) and (q, carol) gone in one event.
    assert await service.get_pending(task.id) == []


@pytest.mark.asyncio
async def test_cancel_question_non_initiator_forbidden() -> None:
    service, (alice, bob) = await _bootstrap()
    task, q = await _start_with_question(service, alice, [bob.id])

    cancel = InboundCancelQuestion(payload=CancelQuestionPayload(retracts=[q.id]))
    with pytest.raises(ForbiddenRole):
        await service.append_event(bob, task.id, cancel)


@pytest.mark.asyncio
async def test_cancel_question_unknown_id_rejected() -> None:
    service, (alice, bob) = await _bootstrap()
    task, _ = await _start_with_question(service, alice, [bob.id])
    cancel = InboundCancelQuestion(payload=CancelQuestionPayload(retracts=[uuid4()]))
    with pytest.raises(InvalidEventShape):
        await service.append_event(alice, task.id, cancel)


@pytest.mark.asyncio
async def test_cancel_question_non_question_event_rejected() -> None:
    service, (alice, bob) = await _bootstrap()
    task, q = await _start_with_question(service, alice, [bob.id])
    # bob emits an info event; alice tries to cancel-question it.
    info = await service.append_event(bob, task.id, InboundInfo())
    cancel = InboundCancelQuestion(payload=CancelQuestionPayload(retracts=[info.id]))
    with pytest.raises(InvalidEventShape):
        await service.append_event(alice, task.id, cancel)
    # And the original pair is still open.
    assert await service.get_pending(task.id) == [(q.id, bob.id)]


@pytest.mark.asyncio
async def test_cancel_question_cross_task_event_id_rejected() -> None:
    service, (alice, bob) = await _bootstrap()
    # task A
    task_a, _ = await _start_with_question(service, alice, [bob.id])
    # task B (alice initiator again, separate task)
    task_b, q_b = await _start_with_question(service, alice, [bob.id])

    # alice tries to cancel q_b inside task_a
    cancel = InboundCancelQuestion(payload=CancelQuestionPayload(retracts=[q_b.id]))
    with pytest.raises(InvalidEventShape):
        await service.append_event(alice, task_a.id, cancel)


@pytest.mark.asyncio
async def test_cancel_question_already_closed_is_noop_append() -> None:
    service, (alice, bob) = await _bootstrap()
    task, q = await _start_with_question(service, alice, [bob.id])
    # bob answers — pair closes.
    await service.append_event(bob, task.id, InboundAnswer(payload=AnswerPayload(answering=[q.id])))
    assert await service.get_pending(task.id) == []

    # alice cancels the (now-closed) question — appended, no-op pop, no error.
    cancel = InboundCancelQuestion(payload=CancelQuestionPayload(retracts=[q.id]))
    ev = await service.append_event(alice, task.id, cancel)
    assert ev.event_type == "cancel_question"
    assert await service.get_pending(task.id) == []


# ---------------------------------------------------------------------------
# cancel_all_questions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_all_questions_clears_all() -> None:
    service, (alice, bob, carol) = await _bootstrap(n_extra=1)
    task, _ = await _start_with_question(service, alice, [bob.id, carol.id])
    await service.append_event(
        alice, task.id, InboundQuestion(payload=QuestionPayload(to=[bob.id])),
    )
    assert len(await service.get_pending(task.id)) == 3

    await service.append_event(alice, task.id, InboundCancelAllQuestions())
    assert await service.get_pending(task.id) == []


@pytest.mark.asyncio
async def test_cancel_all_questions_empty_pending_is_noop() -> None:
    service, (alice, _bob) = await _bootstrap()
    # Empty task — no pending pairs at all.
    task = await service.create_task(alice, CreateTaskBody())
    # Append an info event first so the task has some history.
    await service.append_event(alice, task.id, InboundInfo(content=Content(text="announce")))

    ev = await service.append_event(alice, task.id, InboundCancelAllQuestions())
    assert ev.event_type == "cancel_all_questions"
    assert await service.get_pending(task.id) == []


@pytest.mark.asyncio
async def test_cancel_all_questions_non_initiator_forbidden() -> None:
    service, (alice, bob) = await _bootstrap()
    task, _ = await _start_with_question(service, alice, [bob.id])
    with pytest.raises(ForbiddenRole):
        await service.append_event(bob, task.id, InboundCancelAllQuestions())


# ---------------------------------------------------------------------------
# multi-target question (per-recipient close)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multi_target_question_close_per_recipient() -> None:
    service, (alice, bob, carol) = await _bootstrap(n_extra=1)
    task, q = await _start_with_question(service, alice, [bob.id, carol.id])
    assert await service.get_pending(task.id) == [(q.id, bob.id), (q.id, carol.id)]

    # bob answers — only (q, bob) pops.
    await service.append_event(bob, task.id, InboundAnswer(payload=AnswerPayload(answering=[q.id])))
    assert await service.get_pending(task.id) == [(q.id, carol.id)]

    # carol answers — pair gone too.
    await service.append_event(carol, task.id, InboundAnswer(payload=AnswerPayload(answering=[q.id])))
    assert await service.get_pending(task.id) == []


# ---------------------------------------------------------------------------
# in_reply_to cross-task validation (uniform across event types)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_in_reply_to_cross_task_rejected_uniformly() -> None:
    service, (alice, bob) = await _bootstrap()
    # Task A and task B, both with alice as initiator and bob as target.
    task_a, q_a = await _start_with_question(service, alice, [bob.id])
    task_b, q_b = await _start_with_question(service, alice, [bob.id])

    cross_id = q_b.id  # event from task B used as in_reply_to inside task A

    # question
    with pytest.raises(InvalidEventShape):
        await service.append_event(
            alice, task_a.id,
            InboundQuestion(payload=QuestionPayload(to=[bob.id]), in_reply_to=cross_id),
        )
    # answer
    with pytest.raises(InvalidEventShape):
        await service.append_event(
            bob, task_a.id,
            InboundAnswer(payload=AnswerPayload(answering=[q_a.id]), in_reply_to=cross_id),
        )
    # info
    with pytest.raises(InvalidEventShape):
        await service.append_event(
            bob, task_a.id, InboundInfo(in_reply_to=cross_id),
        )
    # cancel_question
    with pytest.raises(InvalidEventShape):
        await service.append_event(
            alice, task_a.id,
            InboundCancelQuestion(payload=CancelQuestionPayload(retracts=[q_a.id]), in_reply_to=cross_id),
        )
    # cancel_all_questions
    with pytest.raises(InvalidEventShape):
        await service.append_event(
            alice, task_a.id, InboundCancelAllQuestions(in_reply_to=cross_id),
        )


# ---------------------------------------------------------------------------
# empty-task + first-event edges
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_task_has_no_events() -> None:
    """An empty task is a valid state. Visible only to its initiator until
    the first question event auto-joins targets."""
    service, (alice, _bob) = await _bootstrap()
    task = await service.create_task(alice, CreateTaskBody())

    assert task.participants == [alice.id]
    assert await service.get_pending(task.id) == []
    assert await service._log.list_events_for_task(task.id) == []


@pytest.mark.asyncio
async def test_first_question_auto_joins_targets() -> None:
    """The first question on an empty task auto-joins its targets atomically
    with the question append (same path as any subsequent question — there is
    no "opening event" special case)."""
    service, (alice, bob, carol) = await _bootstrap(n_extra=1)
    task = await service.create_task(alice, CreateTaskBody())
    q = await service.append_event(
        alice, task.id, InboundQuestion(payload=QuestionPayload(to=[bob.id, carol.id])),
    )

    assert task.participants == [alice.id, bob.id, carol.id]
    log = await service._log.list_events_for_task(task.id)
    # participant_joined for each new target, then the question itself.
    assert [e.event_type for e in log] == [
        "participant_joined", "participant_joined", "question",
    ]
    assert await service.get_pending(task.id) == [(q.id, bob.id), (q.id, carol.id)]


# ---------------------------------------------------------------------------
# concurrency — first-answer-wins under per-task lock
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_answer_wins_under_concurrency() -> None:
    service, (alice, bob) = await _bootstrap()
    task, q = await _start_with_question(service, alice, [bob.id])

    a1 = InboundAnswer(payload=AnswerPayload(answering=[q.id]), content=Content(text="first"))
    a2 = InboundAnswer(payload=AnswerPayload(answering=[q.id]), content=Content(text="second"))
    results = await asyncio.gather(
        service.append_event(bob, task.id, a1),
        service.append_event(bob, task.id, a2),
    )
    # Both events are in the log under the per-task lock.
    assert all(ev.event_type == "answer" for ev in results)
    log = await service._log.list_events_for_task(task.id)
    answer_events = [e for e in log if e.event_type == "answer"]
    assert len(answer_events) == 2
    # Only one pop happened — pair is gone, but we never re-opened on the second.
    assert await service.get_pending(task.id) == []


# ---------------------------------------------------------------------------
# §7 rebuildability invariant
# ---------------------------------------------------------------------------

def _rebuild_pending_from_log(log: list[Event]) -> list[tuple[UUID, UUID]]:
    """From-scratch reducer over an event log. Mirrors the §7 SQL exactly:
    a pair (Q, P) is open iff a `question` event opened it, AND no `answer`
    from P referencing Q closed it, AND no `cancel_question` referencing Q
    closed it, AND no `cancel_all_questions` after Q's open closed it.
    """
    pending: list[tuple[UUID, UUID]] = []
    for ev in log:
        if isinstance(ev, QuestionEvent):
            for t in ev.payload.to:
                pending.append((ev.id, t))
        elif isinstance(ev, AnswerEvent):
            answering = set(ev.payload.answering)
            sender = ev.from_
            pending = [(q, t) for (q, t) in pending if not (q in answering and t == sender)]
        elif isinstance(ev, CancelQuestionEvent):
            retracts = set(ev.payload.retracts)
            pending = [(q, t) for (q, t) in pending if q not in retracts]
        elif isinstance(ev, CancelAllQuestionsEvent):
            pending = []
        # info / participant_joined have no effect
    return pending


@pytest.mark.asyncio
async def test_pending_questions_matches_rebuilt_view() -> None:
    service, (alice, bob, carol) = await _bootstrap(n_extra=1)
    # Drive a multi-step flow.
    task, q1 = await _start_with_question(service, alice, [bob.id, carol.id])
    q2_ev = await service.append_event(
        alice, task.id, InboundQuestion(payload=QuestionPayload(to=[carol.id])),
    )
    q2 = cast(QuestionEvent, q2_ev)
    await service.append_event(bob, task.id, InboundAnswer(payload=AnswerPayload(answering=[q1.id])))
    await service.append_event(bob, task.id, InboundInfo(content=Content(text="ack")))
    await service.append_event(
        alice, task.id,
        InboundCancelQuestion(payload=CancelQuestionPayload(retracts=[q2.id])),
    )
    q3_ev = await service.append_event(
        alice, task.id, InboundQuestion(payload=QuestionPayload(to=[bob.id])),
    )
    q3 = cast(QuestionEvent, q3_ev)
    await service.append_event(bob, task.id, InboundAnswer(payload=AnswerPayload(answering=[q3.id])))

    # At this point only (q1, carol) should remain open.
    log = await service._log.list_events_for_task(task.id)
    assert _rebuild_pending_from_log(log) == [(q1.id, carol.id)]
    assert await service.get_pending(task.id) == [(q1.id, carol.id)]

    # Now alice clears everything mid-flight.
    await service.append_event(alice, task.id, InboundCancelAllQuestions())
    log = await service._log.list_events_for_task(task.id)
    assert _rebuild_pending_from_log(log) == []
    assert await service.get_pending(task.id) == []
