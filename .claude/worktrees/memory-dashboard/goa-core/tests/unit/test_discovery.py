"""Unit tests for `ParticipantStore.search` capability+q+type filtering and
`TaskLog.list_tasks_for_participant` role / parent filters.

The `has_pending` filter is served at the service layer by `PendingProjection`,
not the repo. Service-level coverage lives in
`tests/integration/test_discovery_e2e.py::test_list_tasks_has_pending_*`."""

from __future__ import annotations

import pytest

from goa.domain.models import Participant, Task
from goa.repos.memory import InMemoryParticipantStore, InMemoryTaskLog


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# ParticipantStore.search — §11
# ---------------------------------------------------------------------------

async def _seed_participants(repo: InMemoryParticipantStore) -> dict[str, Participant]:
    a = Participant(
        type="agent",
        name="legal-summarizer",
        description="summarizes legal contracts",
        capabilities=["summarize", "legal"],
        api_key_hash="h-a",
    )
    b = Participant(
        type="agent",
        name="bullet-summarizer",
        description="summarizes any text",
        capabilities=["summarize"],
        api_key_hash="h-b",
    )
    c = Participant(
        type="service",
        name="slack-bridge",
        description="bridges slack threads",
        capabilities=["chat"],
        api_key_hash="h-c",
    )
    for p in (a, b, c):
        await repo.create(p)
    return {"a": a, "b": b, "c": c}


async def test_search_capability_single() -> None:
    repo = InMemoryParticipantStore()
    seeded = await _seed_participants(repo)
    results = await repo.search(capabilities=["summarize"])
    assert {p.id for p in results} == {seeded["a"].id, seeded["b"].id}


async def test_search_capability_anded() -> None:
    repo = InMemoryParticipantStore()
    seeded = await _seed_participants(repo)
    # Only `a` carries both `summarize` AND `legal`.
    results = await repo.search(capabilities=["summarize", "legal"])
    assert [p.id for p in results] == [seeded["a"].id]


async def test_search_capability_anded_no_match() -> None:
    repo = InMemoryParticipantStore()
    await _seed_participants(repo)
    results = await repo.search(capabilities=["summarize", "nonexistent"])
    assert results == []


async def test_search_q_case_insensitive_substring_on_name_and_description() -> None:
    repo = InMemoryParticipantStore()
    seeded = await _seed_participants(repo)
    # `q="LEGAL"` matches `legal-summarizer` (in name and description) only;
    # case-insensitive.
    results = await repo.search(q="LEGAL")
    assert [p.id for p in results] == [seeded["a"].id]
    # `q="bridge"` matches the description of `slack-bridge`.
    results = await repo.search(q="bridge")
    assert [p.id for p in results] == [seeded["c"].id]


async def test_search_type_filter() -> None:
    repo = InMemoryParticipantStore()
    seeded = await _seed_participants(repo)
    results = await repo.search(type="service")
    assert [p.id for p in results] == [seeded["c"].id]
    results = await repo.search(type="agent")
    assert {p.id for p in results} == {seeded["a"].id, seeded["b"].id}


async def test_search_filters_anded_together() -> None:
    repo = InMemoryParticipantStore()
    seeded = await _seed_participants(repo)
    # `agent` + `summarize` cap + q=`bullet` → only `b`.
    results = await repo.search(
        type="agent", capabilities=["summarize"], q="bullet",
    )
    assert [p.id for p in results] == [seeded["b"].id]


# ---------------------------------------------------------------------------
# TaskLog.list_tasks_for_participant — §9.2
# ---------------------------------------------------------------------------

def _make_task(initiator: Participant, *, parent: Task | None = None,
               extra_participants: list[Participant] | None = None) -> Task:
    participants = [initiator.id]
    if extra_participants:
        participants.extend(p.id for p in extra_participants)
    return Task(
        initiator_id=initiator.id,
        parent_task_id=parent.id if parent else None,
        participants=participants,
    )


async def test_list_for_participant_role_filters() -> None:
    parts = InMemoryParticipantStore()
    a = Participant(type="agent", name="a", api_key_hash="ha")
    b = Participant(type="agent", name="b", api_key_hash="hb")
    await parts.create(a)
    await parts.create(b)

    tasks = InMemoryTaskLog()
    t_a_init = _make_task(a, extra_participants=[b])
    t_b_init = _make_task(b, extra_participants=[a])
    await tasks.create_task(t_a_init)
    await tasks.create_task(t_b_init)

    initiator_for_a = await tasks.list_tasks_for_participant(a.id, role="initiator")
    assert [t.id for t in initiator_for_a] == [t_a_init.id]

    member_for_a = await tasks.list_tasks_for_participant(a.id, role="member")
    assert [t.id for t in member_for_a] == [t_b_init.id]

    all_for_a = await tasks.list_tasks_for_participant(a.id)
    assert {t.id for t in all_for_a} == {t_a_init.id, t_b_init.id}


# `has_pending` is not a repo-level concern — pending is a derived view
# served by `PendingProjection`. Service-level coverage of the filter lives
# in `tests/integration/test_discovery_e2e.py`.


async def test_list_for_participant_top_level_default_skips_children() -> None:
    a = Participant(type="agent", name="a", api_key_hash="ha")

    tasks = InMemoryTaskLog()
    parent = _make_task(a)
    child = _make_task(a, parent=parent)
    await tasks.create_task(parent)
    await tasks.create_task(child)

    # Default: top-level only — child is filtered out even though caller is in it.
    top = await tasks.list_tasks_for_participant(a.id)
    assert [t.id for t in top] == [parent.id]

    # parent_id supplied: returns only that parent's children.
    children = await tasks.list_tasks_for_participant(
        a.id, parent_id=parent.id, top_level_only=False,
    )
    assert [t.id for t in children] == [child.id]
