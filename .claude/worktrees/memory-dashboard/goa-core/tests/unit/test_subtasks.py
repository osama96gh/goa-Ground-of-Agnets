from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest

from goa.domain.models import (
    ChildTaskCreatedEvent,
    CreateTaskBody,
    InboundQuestion,
    Participant,
    QuestionEvent,
    QuestionPayload,
)
from goa.errors import ParentTaskNotVisible
from goa.repos.memory import (
    InMemoryBlobStore,
    InMemoryParticipantStore,
    InMemoryTaskLog,
)
from goa.services.tasks import TaskService
from goa.stream.hub import InMemoryStreamHub


async def _bootstrap() -> tuple[
    TaskService,
    InMemoryParticipantStore,
    InMemoryTaskLog,
    Participant,
    Participant,
    Participant,
]:
    participants = InMemoryParticipantStore()
    task_log = InMemoryTaskLog()
    hub = InMemoryStreamHub(replay_buffer_size=100, queue_size=100)
    service = TaskService(participants, task_log, InMemoryBlobStore(), hub)

    s = Participant(type="service", name="chat", api_key_hash="hs")
    c = Participant(type="agent", name="support", api_key_hash="hc")
    p = Participant(type="agent", name="payments", api_key_hash="hp")
    await participants.create(s)
    await participants.create(c)
    await participants.create(p)
    return service, participants, task_log, s, c, p


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
async def test_parent_task_not_found_raises() -> None:
    service, _, _, s, _, _ = await _bootstrap()
    body = CreateTaskBody(parent_task_id=uuid4())
    with pytest.raises(ParentTaskNotVisible):
        await service.create_task(s, body)


@pytest.mark.asyncio
async def test_caller_not_in_parent_raises() -> None:
    service, _, _, s, c, p = await _bootstrap()
    # s opens T1 targeting c. p is not in T1.
    parent, _ = await _start_with_question(service, s, [c.id])

    with pytest.raises(ParentTaskNotVisible):
        await service.create_task(p, CreateTaskBody(parent_task_id=parent.id))


@pytest.mark.asyncio
async def test_child_carries_parent_task_id() -> None:
    service, _, _, s, c, _p = await _bootstrap()
    parent, _ = await _start_with_question(service, s, [c.id])
    child = await service.create_task(
        c, CreateTaskBody(parent_task_id=parent.id, subject="payments lookup"),
    )
    assert child.parent_task_id == parent.id
    # parent stays a root.
    assert parent.parent_task_id is None


@pytest.mark.asyncio
async def test_child_task_created_lands_in_parent_log() -> None:
    service, _, task_log, s, c, _p = await _bootstrap()
    parent, _ = await _start_with_question(service, s, [c.id])
    child = await service.create_task(
        c, CreateTaskBody(parent_task_id=parent.id, subject="payments lookup"),
    )

    parent_log = await task_log.list_events_for_task(parent.id)
    matches = [ev for ev in parent_log if isinstance(ev, ChildTaskCreatedEvent)]
    assert len(matches) == 1
    evt = matches[0]
    assert evt.from_ is None
    assert evt.payload.task_id == child.id
    assert evt.payload.spawned_by == c.id
    assert evt.payload.subject == "payments lookup"

    # Mirror not in the child's own log.
    child_log = await task_log.list_events_for_task(child.id)
    assert all(not isinstance(ev, ChildTaskCreatedEvent) for ev in child_log)


@pytest.mark.asyncio
async def test_child_task_created_fires_before_any_child_event() -> None:
    """`child_task_created` fires at task creation, the moment the child
    exists, even if the child has zero events. A participant in both tasks
    sees "a child appeared" before seeing any content in the child."""
    service, _, task_log, s, c, _p = await _bootstrap()
    parent, _ = await _start_with_question(service, s, [c.id])

    # Child created with no follow-up events.
    child = await service.create_task(c, CreateTaskBody(parent_task_id=parent.id))

    # Parent receives child_task_created immediately, before the child has
    # any events of its own.
    parent_log = await task_log.list_events_for_task(parent.id)
    matches = [ev for ev in parent_log if isinstance(ev, ChildTaskCreatedEvent)]
    assert len(matches) == 1
    assert matches[0].payload.task_id == child.id

    # Child's own log is empty.
    child_log = await task_log.list_events_for_task(child.id)
    assert child_log == []


@pytest.mark.asyncio
async def test_child_task_created_subject_none_when_unset() -> None:
    service, _, task_log, s, c, _p = await _bootstrap()
    parent, _ = await _start_with_question(service, s, [c.id])
    await service.create_task(c, CreateTaskBody(parent_task_id=parent.id))
    parent_log = await task_log.list_events_for_task(parent.id)
    evt = next(ev for ev in parent_log if isinstance(ev, ChildTaskCreatedEvent))
    assert evt.payload.subject is None


@pytest.mark.asyncio
async def test_parent_pending_unchanged_by_subtask_creation() -> None:
    service, _, _, s, c, p = await _bootstrap()
    parent, parent_question = await _start_with_question(service, s, [c.id])
    pending_before = await service.get_pending(parent.id)
    assert pending_before == [(parent_question.id, c.id)]

    child = await service.create_task(c, CreateTaskBody(parent_task_id=parent.id))
    # Even after appending a question on the child, parent pending is unchanged.
    await service.append_event(
        c, child.id, InboundQuestion(payload=QuestionPayload(to=[p.id])),
    )
    assert await service.get_pending(parent.id) == pending_before


@pytest.mark.asyncio
async def test_child_visibility_sealed_from_parent_participants() -> None:
    service, _, _, s, c, p = await _bootstrap()
    parent, _ = await _start_with_question(service, s, [c.id])
    child = await service.create_task(c, CreateTaskBody(parent_task_id=parent.id))
    # The child only has c as a participant until its own question auto-joins more.
    await service.append_event(
        c, child.id, InboundQuestion(payload=QuestionPayload(to=[p.id])),
    )
    # s is in parent but never auto-joined into child.
    assert s.id not in child.participants
    # p is in child but never auto-joined into parent.
    assert p.id not in parent.participants


@pytest.mark.asyncio
async def test_list_children_returns_only_direct_children() -> None:
    service, _, task_log, s, c, _p = await _bootstrap()
    parent, _ = await _start_with_question(service, s, [c.id])
    child1 = await service.create_task(c, CreateTaskBody(parent_task_id=parent.id))
    child2 = await service.create_task(c, CreateTaskBody(parent_task_id=parent.id))
    # Unrelated root task by some other initiator.
    unrelated, _ = await _start_with_question(service, s, [c.id])

    children = await task_log.list_children(parent.id)
    child_ids = {t.id for t in children}
    assert child_ids == {child1.id, child2.id}
    assert unrelated.id not in child_ids


@pytest.mark.asyncio
async def test_root_task_no_parent_event_emitted() -> None:
    service, _, task_log, s, c, _ = await _bootstrap()
    parent, _ = await _start_with_question(service, s, [c.id])
    log = await task_log.list_events_for_task(parent.id)
    assert all(not isinstance(ev, ChildTaskCreatedEvent) for ev in log)
