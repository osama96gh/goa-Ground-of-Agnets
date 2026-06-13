"""E2e for `GET /participants` discovery, expanded `GET /tasks`,
`GET /pending`."""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest

from goa.config import Settings
from goa.main import create_app

from tests.integration._helpers import create_task_with_question
from tests.integration._live_server import live_server


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _register(client: httpx.AsyncClient, **body) -> tuple[UUID, str]:
    resp = await client.post("/participants", json=body)
    resp.raise_for_status()
    decoded = resp.json()
    return UUID(decoded["participant"]["id"]), decoded["api_key"]


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# GET /participants
# ---------------------------------------------------------------------------

async def test_search_participants_capability_anded_q_and_type() -> None:
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            legal_id, key_legal = await _register(
                http, type="agent", name="legal-summarizer",
                description="summarizes legal contracts",
                capabilities=["summarize", "legal"],
            )
            bullet_id, _ = await _register(
                http, type="agent", name="bullet-summarizer",
                description="general summarizer",
                capabilities=["summarize"],
            )
            slack_id, _ = await _register(
                http, type="service", name="slack-bridge",
                description="bridges slack threads",
                capabilities=["chat"],
            )

            # `?capability=summarize` returns both summarizers.
            resp = await http.get(
                "/participants",
                params=[("capability", "summarize")],
                headers=_auth(key_legal),
            )
            resp.raise_for_status()
            ids = {UUID(p["id"]) for p in resp.json()["participants"]}
            assert ids == {legal_id, bullet_id}

            # AND-ed: `?capability=summarize&capability=legal` returns only legal.
            resp = await http.get(
                "/participants",
                params=[("capability", "summarize"), ("capability", "legal")],
                headers=_auth(key_legal),
            )
            assert [UUID(p["id"]) for p in resp.json()["participants"]] == [legal_id]

            # `?q=BRIDGE&type=service` (case-insensitive on description).
            resp = await http.get(
                "/participants",
                params={"q": "BRIDGE", "type": "service"},
                headers=_auth(key_legal),
            )
            assert [UUID(p["id"]) for p in resp.json()["participants"]] == [slack_id]


async def test_search_participants_requires_auth() -> None:
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            resp = await http.get("/participants")
            assert resp.status_code == 401


async def test_get_participant_by_id_404_on_unknown() -> None:
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            _, key = await _register(http, type="agent", name="a")
            resp = await http.get(
                f"/participants/{uuid4()}", headers=_auth(key),
            )
            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == "not_found"


async def test_create_participant_rejects_private_access_policy() -> None:
    """Spec §6.1: `access_policy` is reserved at default `public`. Wire-level
    setters for `private` are deferred to v3 — we reject the field early so a
    client can't ship code expecting ACL gating that isn't there."""
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            resp = await http.post(
                "/participants",
                json={"type": "agent", "name": "a", "access_policy": "private"},
            )
            assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /tasks expanded filters
# ---------------------------------------------------------------------------

async def test_list_tasks_role_initiator_vs_member() -> None:
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            init_id, key_init = await _register(http, type="agent", name="init")
            other_id, key_other = await _register(http, type="agent", name="other")

            # init creates a task targeting `other`.
            t1_id, _ = await create_task_with_question(
                http, key_init, targets=[str(other_id)], subject="t1",
            )
            # `other` creates a separate task targeting `init` — so each is
            # initiator on one, member on the other.
            t2_id, _ = await create_task_with_question(
                http, key_other, targets=[str(init_id)], subject="t2",
            )

            # init: role=initiator returns t1; role=member returns t2.
            resp = await http.get(
                "/tasks", params={"role": "initiator"}, headers=_auth(key_init),
            )
            assert [UUID(item["task"]["id"]) for item in resp.json()["tasks"]] == [t1_id]

            resp = await http.get(
                "/tasks", params={"role": "member"}, headers=_auth(key_init),
            )
            assert [UUID(item["task"]["id"]) for item in resp.json()["tasks"]] == [t2_id]


async def test_list_tasks_has_pending_reads_pending_questions_directly() -> None:
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            init_id, key_init = await _register(http, type="agent", name="init")
            other_id, key_other = await _register(http, type="agent", name="other")

            t_open_id, q_id = await create_task_with_question(
                http, key_init, targets=[str(other_id)],
            )

            # An info-only task keeps pending empty.
            create_resp = await http.post(
                "/tasks", headers=_auth(key_init), json={"subject": "", "metadata": {}},
            )
            create_resp.raise_for_status()
            t_closed_id = UUID(create_resp.json()["task"]["id"])
            info_resp = await http.post(
                f"/tasks/{t_closed_id}/events",
                headers=_auth(key_init),
                json={
                    "event_type": "info",
                    "payload": {},
                    "content": {"text": "fyi"},
                    "in_reply_to": None,
                    "metadata": {},
                },
            )
            info_resp.raise_for_status()

            resp = await http.get(
                "/tasks", params={"has_pending": "true"}, headers=_auth(key_init),
            )
            assert [UUID(item["task"]["id"]) for item in resp.json()["tasks"]] == [t_open_id]

            resp = await http.get(
                "/tasks", params={"has_pending": "false"}, headers=_auth(key_init),
            )
            assert [UUID(item["task"]["id"]) for item in resp.json()["tasks"]] == [t_closed_id]

            # Answer the pending — has_pending=true should now return nothing
            # for the initiator (only `other` was a target).
            await http.post(
                f"/tasks/{t_open_id}/events",
                headers=_auth(key_other),
                json={
                    "event_type": "answer",
                    "payload": {"answering": [str(q_id)]},
                    "content": {"text": "ok"},
                },
            )
            resp = await http.get(
                "/tasks", params={"has_pending": "true"}, headers=_auth(key_init),
            )
            assert resp.json()["tasks"] == []


async def test_list_tasks_parent_id_filters_to_children() -> None:
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            init_id, key_init = await _register(http, type="agent", name="init")
            specialist_id, key_specialist = await _register(
                http, type="agent", name="specialist",
            )

            # Parent task: init → specialist.
            parent_id, _ = await create_task_with_question(
                http, key_init, targets=[str(specialist_id)],
            )
            # Sub-task spawned by specialist (auto-joined into parent by the
            # question above). Child is empty until specialist appends a
            # question targeting init.
            child_id, _ = await create_task_with_question(
                http, key_specialist,
                targets=[str(init_id)],
                parent_task_id=str(parent_id),
                text="clarify?",
            )

            # Default `GET /tasks` for init: top-level only → only parent.
            resp = await http.get("/tasks", headers=_auth(key_init))
            assert [UUID(item["task"]["id"]) for item in resp.json()["tasks"]] == [parent_id]

            # `?parent_id={parent_id}` for init: returns the child (init was
            # targeted in the child's opening question, so they're a participant).
            resp = await http.get(
                "/tasks", params={"parent_id": str(parent_id)}, headers=_auth(key_init),
            )
            assert [UUID(item["task"]["id"]) for item in resp.json()["tasks"]] == [child_id]


# ---------------------------------------------------------------------------
# GET /pending
# ---------------------------------------------------------------------------

async def test_pending_drains_on_cancel_question() -> None:
    """`/pending` matches the §7 SQL definition: arm 2 — `cancel_question`
    from the initiator referencing the open question closes the row task-wide.
    Materialized projection must reflect the same outcome."""
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            init_id, key_init = await _register(http, type="agent", name="init")
            target_id, key_target = await _register(http, type="agent", name="target")

            task_id, q_id = await create_task_with_question(
                http, key_init, targets=[str(target_id)],
            )

            # Sanity — pending shows the row.
            resp = await http.get("/pending", headers=_auth(key_target))
            assert [UUID(row["question_event_id"]) for row in resp.json()] == [q_id]

            # Initiator retracts the question.
            await http.post(
                f"/tasks/{task_id}/events",
                headers=_auth(key_init),
                json={
                    "event_type": "cancel_question",
                    "payload": {"retracts": [str(q_id)]},
                    "content": {},
                },
            )

            resp = await http.get("/pending", headers=_auth(key_target))
            assert resp.json() == []


async def test_pending_drains_on_cancel_all_questions() -> None:
    """§7 SQL arm 3 — `cancel_all_questions` from the initiator clears every
    then-open pair atomically. `/pending` must report empty afterward,
    regardless of how many were open."""
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            init_id, key_init = await _register(http, type="agent", name="init")
            t1_target_id, _ = await _register(http, type="agent", name="t1")
            target_id, key_target = await _register(http, type="agent", name="target")

            # Two questions to `target` across two tasks.
            t1_id, _ = await create_task_with_question(
                http, key_init, targets=[str(target_id)], text="q1",
            )
            t2_id, _ = await create_task_with_question(
                http, key_init, targets=[str(target_id)], text="q2",
            )

            # Sanity — both rows visible.
            resp = await http.get("/pending", headers=_auth(key_target))
            assert len(resp.json()) == 2

            # Cancel all on t1 — only t1's pair drops; t2 remains.
            await http.post(
                f"/tasks/{t1_id}/events",
                headers=_auth(key_init),
                json={"event_type": "cancel_all_questions", "payload": {}, "content": {}},
            )
            resp = await http.get("/pending", headers=_auth(key_target))
            rows = resp.json()
            assert [UUID(r["task_id"]) for r in rows] == [t2_id]

            # Cancel all on t2 — pending fully drains.
            await http.post(
                f"/tasks/{t2_id}/events",
                headers=_auth(key_init),
                json={"event_type": "cancel_all_questions", "payload": {}, "content": {}},
            )
            resp = await http.get("/pending", headers=_auth(key_target))
            assert resp.json() == []


async def test_pending_returns_open_pairs_targeting_caller() -> None:
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            init_id, key_init = await _register(http, type="agent", name="init")
            target_id, key_target = await _register(http, type="agent", name="target")

            # Two questions to `target` across two tasks.
            t1_id, q1_id = await create_task_with_question(
                http, key_init, targets=[str(target_id)], subject="t1", text="q1?",
            )
            t2_id, q2_id = await create_task_with_question(
                http, key_init, targets=[str(target_id)], subject="t2", text="q2?",
            )

            # `target` sees both pending; `init` (only initiator, never targeted)
            # sees none.
            resp = await http.get("/pending", headers=_auth(key_target))
            assert resp.status_code == 200
            rows = resp.json()
            assert {(UUID(r["task_id"]), UUID(r["question_event_id"])) for r in rows} == {
                (t1_id, q1_id), (t2_id, q2_id),
            }
            for row in rows:
                assert UUID(row["from"]) == init_id

            resp = await http.get("/pending", headers=_auth(key_init))
            assert resp.json() == []

            # Answer q1 — target's pending shrinks to q2.
            await http.post(
                f"/tasks/{t1_id}/events",
                headers=_auth(key_target),
                json={
                    "event_type": "answer",
                    "payload": {"answering": [str(q1_id)]},
                    "content": {"text": "a1"},
                },
            )
            resp = await http.get("/pending", headers=_auth(key_target))
            rows = resp.json()
            assert [(UUID(r["task_id"]), UUID(r["question_event_id"])) for r in rows] == [
                (t2_id, q2_id),
            ]
