"""Unit tests for `TaskService.upsert_task` and `external_ref` on
`create_task` (§6.4 / §9.2)."""

from __future__ import annotations

import pytest

from goa.domain.models import (
    CreateTaskBody,
    Participant,
    UpsertTaskBody,
    UpsertTaskOnCreate,
)
from goa.errors import ExternalRefInUse
from goa.repos.memory import (
    InMemoryBlobStore,
    InMemoryParticipantStore,
    InMemoryTaskLog,
)
from goa.services.tasks import TaskService
from goa.stream.hub import InMemoryStreamHub


pytestmark = pytest.mark.asyncio


async def _bootstrap() -> tuple[
    TaskService,
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
    s2 = Participant(type="service", name="chat-2", api_key_hash="hs2")
    c = Participant(type="agent", name="support", api_key_hash="hc")
    await participants.create(s)
    await participants.create(s2)
    await participants.create(c)
    return service, task_log, s, s2, c


def _create_body(ref: str | None = None) -> CreateTaskBody:
    return CreateTaskBody(external_ref=ref)


def _upsert_body(ref: str, parent_task_id=None) -> UpsertTaskBody:
    return UpsertTaskBody(
        external_ref=ref,
        on_create=UpsertTaskOnCreate(parent_task_id=parent_task_id),
    )


async def test_create_task_with_external_ref_indexes_it() -> None:
    service, task_log, s, _s2, _c = await _bootstrap()
    task = await service.create_task(s, _create_body("slack-thread-abc"))

    assert task.external_ref == "slack-thread-abc"
    assert await task_log.get_task_by_external_ref(s.id, "slack-thread-abc") == task.id


async def test_create_task_collision_raises_external_ref_in_use() -> None:
    service, _task_log, s, _s2, _c = await _bootstrap()
    await service.create_task(s, _create_body("slack-thread-abc"))

    with pytest.raises(ExternalRefInUse):
        await service.create_task(s, _create_body("slack-thread-abc"))


async def test_create_task_collision_does_not_persist_orphan() -> None:
    """`TaskLog.create_task` reserves the external_ref atomically with the
    task row — a collision must leave the store unchanged."""
    service, task_log, s, _s2, _c = await _bootstrap()
    first = await service.create_task(s, _create_body("slack-thread-abc"))
    before = len(task_log._tasks)  # one task

    with pytest.raises(ExternalRefInUse):
        await service.create_task(s, _create_body("slack-thread-abc"))

    assert len(task_log._tasks) == before
    assert task_log._tasks[first.id] is first


async def test_upsert_task_creates_when_unmapped() -> None:
    service, task_log, s, _s2, _c = await _bootstrap()
    task, created = await service.upsert_task(s, _upsert_body("slack-thread-abc"))

    assert created is True
    assert task.external_ref == "slack-thread-abc"
    assert await task_log.get_task_by_external_ref(s.id, "slack-thread-abc") == task.id


async def test_upsert_task_returns_existing_on_hit() -> None:
    service, _task_log, s, _s2, _c = await _bootstrap()
    first, _ = await service.upsert_task(s, _upsert_body("slack-thread-abc"))

    second, created = await service.upsert_task(s, _upsert_body("slack-thread-abc"))

    assert created is False
    assert second.id == first.id


async def test_upsert_cross_initiator_isolation() -> None:
    """§6.4: same `external_ref` under different initiators are different slots."""
    service, _task_log, s, s2, _c = await _bootstrap()
    a, _ = await service.upsert_task(s, _upsert_body("slack-thread-abc"))
    b, _ = await service.upsert_task(s2, _upsert_body("slack-thread-abc"))

    assert a.id != b.id
    assert a.initiator_id == s.id
    assert b.initiator_id == s2.id


async def test_upsert_subtask_namespace() -> None:
    """A child task can carry the same `external_ref` string as the root —
    different initiators, different slots."""
    from goa.domain.models import InboundQuestion, QuestionPayload

    service, _task_log, s, _s2, c = await _bootstrap()

    root, _ = await service.upsert_task(s, _upsert_body("slack-thread-abc"))
    # `c` becomes a participant of `root` via a question from `s` — the root
    # task is empty after upsert, so we need to add `c` explicitly before
    # `c` can spawn a child of it.
    await service.append_event(
        s, root.id, InboundQuestion(payload=QuestionPayload(to=[c.id])),
    )

    # Spawn a child of `root` initiated by `c`, with the same ref string.
    child, created = await service.upsert_task(
        c, _upsert_body("slack-thread-abc", parent_task_id=root.id),
    )

    assert created is True
    assert child.id != root.id
    assert child.parent_task_id == root.id
    assert child.external_ref == "slack-thread-abc"
