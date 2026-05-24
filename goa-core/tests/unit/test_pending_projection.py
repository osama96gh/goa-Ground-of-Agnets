"""Tests for `PendingProjection` — the derived view of pending questions
rebuilt from the event log.

Covers:
1. Pure-function `_apply_event` per §7 variant + no-op cases.
2. Cold-rebuild from a populated TaskLog.
3. Convergence: real `TaskService` driving real event flows; the projection
   matches the §7 push/pop semantics after each event.
4. Deadlock regression — `_fanout` runs inside the task lock and calls
   `get()`; if `apply()` were not called on non-pending event types, the
   first event of a brand-new task (e.g. `info`) would leave the cache cold
   and `get()` would re-acquire the lock and hang. The whole flow must
   complete inside a short timeout.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from goa.domain.models import (
    AnswerEvent,
    AnswerPayload,
    CancelAllQuestionsEvent,
    CancelAllQuestionsPayload,
    CancelQuestionEvent,
    CancelQuestionPayload,
    ChildTaskCreatedEvent,
    ChildTaskCreatedPayload,
    Content,
    CreateTaskBody,
    InboundAnswer,
    InboundCancelAllQuestions,
    InboundCancelQuestion,
    InboundInfo,
    InboundQuestion,
    InfoEvent,
    InfoPayload,
    Participant,
    ParticipantJoinedEvent,
    ParticipantJoinedPayload,
    QuestionEvent,
    QuestionPayload,
)
from goa.repos.memory import (
    InMemoryBlobStore,
    InMemoryParticipantStore,
    InMemoryTaskLog,
)
from goa.services.pending_projection import PendingProjection
from goa.services.tasks import TaskService
from goa.stream.hub import InMemoryStreamHub


async def _bootstrap(n_extra: int = 0) -> tuple[TaskService, list[Participant]]:
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


# ---------------------------------------------------------------------------
# 1. Pure-function _apply_event per §7
# ---------------------------------------------------------------------------

def _question(*, task_id, from_, to):
    return QuestionEvent(
        task_id=task_id,
        from_=from_,
        payload=QuestionPayload(to=list(to)),
    )


def _answer(*, task_id, from_, answering):
    return AnswerEvent(
        task_id=task_id,
        from_=from_,
        payload=AnswerPayload(answering=list(answering)),
    )


def _cancel_q(*, task_id, from_, retracts):
    return CancelQuestionEvent(
        task_id=task_id,
        from_=from_,
        payload=CancelQuestionPayload(retracts=list(retracts)),
    )


def _cancel_all(*, task_id, from_):
    return CancelAllQuestionsEvent(
        task_id=task_id,
        from_=from_,
        payload=CancelAllQuestionsPayload(),
    )


def test_apply_question_single_target() -> None:
    tid, alice, bob = uuid4(), uuid4(), uuid4()
    q = _question(task_id=tid, from_=alice, to=[bob])
    assert PendingProjection._apply_event([], q) == [(q.id, bob)]


def test_apply_question_two_targets_preserves_order() -> None:
    tid, alice, bob, carol = uuid4(), uuid4(), uuid4(), uuid4()
    q = _question(task_id=tid, from_=alice, to=[bob, carol])
    assert PendingProjection._apply_event([], q) == [
        (q.id, bob),
        (q.id, carol),
    ]


def test_apply_answer_first_answer_wins_per_target() -> None:
    """The answerer's pair drops; sibling targets remain pending."""
    tid, alice, bob, carol = uuid4(), uuid4(), uuid4(), uuid4()
    q = _question(task_id=tid, from_=alice, to=[bob, carol])
    state = PendingProjection._apply_event([], q)
    a = _answer(task_id=tid, from_=bob, answering=[q.id])
    assert PendingProjection._apply_event(state, a) == [(q.id, carol)]


def test_apply_answer_unrelated_caller_is_noop() -> None:
    """Caller who never had a pending pair for this qid — state unchanged."""
    tid, alice, bob, carol, dan = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    q = _question(task_id=tid, from_=alice, to=[bob])
    state = PendingProjection._apply_event([], q)
    a = _answer(task_id=tid, from_=dan, answering=[q.id])
    assert PendingProjection._apply_event(state, a) == [(q.id, bob)]


def test_apply_cancel_question_drops_all_pairs_for_qid() -> None:
    tid, alice, bob, carol = uuid4(), uuid4(), uuid4(), uuid4()
    q = _question(task_id=tid, from_=alice, to=[bob, carol])
    state = PendingProjection._apply_event([], q)
    c = _cancel_q(task_id=tid, from_=alice, retracts=[q.id])
    assert PendingProjection._apply_event(state, c) == []


def test_apply_cancel_all_clears() -> None:
    tid, alice, bob, carol = uuid4(), uuid4(), uuid4(), uuid4()
    q1 = _question(task_id=tid, from_=alice, to=[bob])
    q2 = _question(task_id=tid, from_=alice, to=[carol])
    state = PendingProjection._apply_event([], q1)
    state = PendingProjection._apply_event(state, q2)
    assert len(state) == 2
    c = _cancel_all(task_id=tid, from_=alice)
    assert PendingProjection._apply_event(state, c) == []


def test_apply_info_is_noop() -> None:
    tid, alice, bob = uuid4(), uuid4(), uuid4()
    q = _question(task_id=tid, from_=alice, to=[bob])
    state = PendingProjection._apply_event([], q)
    info = InfoEvent(task_id=tid, from_=alice, payload=InfoPayload())
    assert PendingProjection._apply_event(state, info) == state


def test_apply_participant_joined_is_noop() -> None:
    tid, bob = uuid4(), uuid4()
    state = [(uuid4(), bob)]
    pj = ParticipantJoinedEvent(
        task_id=tid, from_=None,
        payload=ParticipantJoinedPayload(participant_id=bob),
    )
    assert PendingProjection._apply_event(state, pj) == state


def test_apply_child_task_created_is_noop() -> None:
    tid, child_tid, alice = uuid4(), uuid4(), uuid4()
    state = [(uuid4(), uuid4())]
    ctc = ChildTaskCreatedEvent(
        task_id=tid, from_=None,
        payload=ChildTaskCreatedPayload(task_id=child_tid, spawned_by=alice),
    )
    assert PendingProjection._apply_event(state, ctc) == state


# ---------------------------------------------------------------------------
# 2. Cold-rebuild from a populated TaskLog
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cold_rebuild_replays_log() -> None:
    """First get() rebuilds from the log; second returns from cache."""
    service, (alice, bob, carol) = await _bootstrap(n_extra=1)
    task = await service.create_task(alice, CreateTaskBody())
    question = await service.append_event(
        alice, task.id, InboundQuestion(payload=QuestionPayload(to=[bob.id, carol.id])),
    )

    # Drive a couple more events through the real service to populate the log.
    await service.append_event(
        bob, task.id, InboundAnswer(payload=AnswerPayload(answering=[question.id])),
    )

    # Spin up a FRESH projection bound to the same log — simulates a process
    # restart with a persistent backend.
    fresh = PendingProjection(service._log)
    rebuilt = await fresh.get(task.id)
    assert rebuilt == [(question.id, carol.id)]  # bob's pair dropped

    # Second call returns the same result; cache is now populated.
    again = await fresh.get(task.id)
    assert again == rebuilt
    assert task.id in fresh._state


# ---------------------------------------------------------------------------
# 3. Convergence: real service drives projection through §7 grammar
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_convergence_question_answer_cancel_cancel_all() -> None:
    """Run question(2 targets) → answer → cancel_question → cancel_all and
    assert the projection matches §7 at each step."""
    service, (alice, bob, carol) = await _bootstrap(n_extra=1)
    task = await service.create_task(alice, CreateTaskBody())
    q1 = await service.append_event(
        alice, task.id, InboundQuestion(payload=QuestionPayload(to=[bob.id, carol.id])),
    )
    assert await service.get_pending(task.id) == [(q1.id, bob.id), (q1.id, carol.id)]

    # bob answers → his pair drops; carol's remains
    await service.append_event(
        bob, task.id, InboundAnswer(payload=AnswerPayload(answering=[q1.id])),
    )
    assert await service.get_pending(task.id) == [(q1.id, carol.id)]

    # alice asks a second question to carol → grows by one
    q2_body = InboundQuestion(payload=QuestionPayload(to=[carol.id]))
    q2 = await service.append_event(alice, task.id, q2_body)
    assert await service.get_pending(task.id) == [
        (q1.id, carol.id),
        (q2.id, carol.id),
    ]

    # alice cancels q2 specifically → only the q2-pair drops
    await service.append_event(
        alice,
        task.id,
        InboundCancelQuestion(payload=CancelQuestionPayload(retracts=[q2.id])),
    )
    assert await service.get_pending(task.id) == [(q1.id, carol.id)]

    # alice cancels all → clear
    await service.append_event(
        alice,
        task.id,
        InboundCancelAllQuestions(payload=CancelAllQuestionsPayload()),
    )
    assert await service.get_pending(task.id) == []


# ---------------------------------------------------------------------------
# 4. Deadlock regression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_first_event_info_does_not_deadlock_fanout() -> None:
    """Append `info` as the FIRST event of a freshly-created (empty) task.

    `_fanout` runs inside the per-task lock and calls `_pending.get(task.id)`.
    If `apply()` weren't called on non-pending event types, the cache would
    be cold here, `get()` would try to re-acquire the lock, and the call
    would hang forever (asyncio.Lock is not reentrant).

    `create_task` does not emit any event; the FIRST event flows through
    `append_event`, so this regression lives entirely on the append-event
    paths.

    `asyncio.wait_for` with a short timeout catches the regression: a
    deadlock would raise TimeoutError, while the correct implementation
    completes in milliseconds.
    """
    service, (alice, _bob) = await _bootstrap()
    task = await service.create_task(alice, CreateTaskBody())
    info = await asyncio.wait_for(
        service.append_event(alice, task.id, InboundInfo(content=Content(text="hello"))),
        timeout=2.0,
    )
    assert info.event_type == "info"
    # info doesn't push anything to pending; convergence: empty.
    assert await service.get_pending(task.id) == []


@pytest.mark.asyncio
async def test_append_info_to_empty_task_does_not_deadlock() -> None:
    """Variant of the deadlock regression that exercises `_append_info` with
    a warm-but-empty projection cache. Two consecutive `info` appends on an
    empty task: the first warms the cache to `[]`; the second exercises the
    same warm-empty path through `_append_info` specifically.
    """
    service, (alice, _bob) = await _bootstrap()
    task = await service.create_task(alice, CreateTaskBody())
    await asyncio.wait_for(
        service.append_event(alice, task.id, InboundInfo(content=Content(text="open"))),
        timeout=2.0,
    )

    info = await asyncio.wait_for(
        service.append_event(alice, task.id, InboundInfo(content=Content(text="follow-up"))),
        timeout=2.0,
    )
    assert info.event_type == "info"
    assert await service.get_pending(task.id) == []
