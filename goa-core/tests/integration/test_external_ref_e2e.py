"""E2e for `POST /tasks/upsert`, the `409 external_ref_in_use` path on direct
`POST /tasks`, and `GET /tasks?external_ref=` (§6.4 / §9.2).

`POST /tasks` and `POST /tasks/upsert` create empty task headers — no event
is emitted at creation. Tests that need an event use `POST /tasks/{id}/events`
afterwards."""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest

from goa.config import Settings
from goa.main import create_app

from tests.integration._live_server import live_server


pytestmark = pytest.mark.asyncio


async def _register(client: httpx.AsyncClient, name: str, type_: str = "agent") -> tuple[str, UUID]:
    resp = await client.post("/participants", json={"type": type_, "name": name})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["api_key"], UUID(body["participant"]["id"])


def _upsert_body(*, external_ref: str, subject: str = "", parent_task_id: str | None = None) -> dict:
    on_create: dict = {"subject": subject, "metadata": {}}
    if parent_task_id is not None:
        on_create["parent_task_id"] = parent_task_id
    return {"external_ref": external_ref, "on_create": on_create}


async def test_upsert_idempotent_same_initiator_same_ref() -> None:
    """Calling `POST /tasks/upsert` twice with the same `external_ref` returns
    the same task; the second call has `created=false` and no new event was
    appended."""
    app = create_app(Settings.for_tests())

    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            s_key, _ = await _register(client, "chat", "service")
            _c_key, c_id = await _register(client, "support")

            first = await client.post(
                "/tasks/upsert",
                headers={"Authorization": f"Bearer {s_key}"},
                json=_upsert_body(external_ref="slack-thread-abc123", subject="refund inquiry"),
            )
            assert first.status_code == 201, first.text
            first_decoded = first.json()
            assert first_decoded["created"] is True
            first_task_id = first_decoded["task"]["id"]

            # Append a first question on the task.
            q_resp = await client.post(
                f"/tasks/{first_task_id}/events",
                headers={"Authorization": f"Bearer {s_key}"},
                json={
                    "event_type": "question",
                    "payload": {"to": [str(c_id)]},
                    "content": {"text": "where's my refund?"},
                    "in_reply_to": None,
                    "metadata": {},
                },
            )
            assert q_resp.status_code == 201

            # Second upsert with the same ref must return the existing task,
            # created=False, and append nothing.
            second = await client.post(
                "/tasks/upsert",
                headers={"Authorization": f"Bearer {s_key}"},
                json=_upsert_body(external_ref="slack-thread-abc123"),
            )
            assert second.status_code == 200, second.text
            second_decoded = second.json()
            assert second_decoded["created"] is False
            assert second_decoded["task"]["id"] == first_task_id

            # The hit should not have appended a second event into the task.
            task_resp = await client.get(
                f"/tasks/{first_task_id}",
                headers={"Authorization": f"Bearer {s_key}"},
            )
            assert task_resp.status_code == 200, task_resp.text
            events = task_resp.json()["events"]
            assert [e["event_type"] for e in events] == ["participant_joined", "question"]
            assert task_resp.json()["task"]["external_ref"] == "slack-thread-abc123"


async def test_upsert_cross_initiator_different_tasks() -> None:
    """Two different initiators upsert the same `external_ref` string — each
    gets their own task. Index key is `(initiator_id, external_ref)`."""
    app = create_app(Settings.for_tests())

    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            s1_key, _ = await _register(client, "chat-a", "service")
            s2_key, _ = await _register(client, "chat-b", "service")
            _c_key, _c_id = await _register(client, "support")

            body = _upsert_body(external_ref="slack-thread-shared")
            t_a = await client.post(
                "/tasks/upsert", headers={"Authorization": f"Bearer {s1_key}"}, json=body,
            )
            t_b = await client.post(
                "/tasks/upsert", headers={"Authorization": f"Bearer {s2_key}"}, json=body,
            )
            assert t_a.status_code == 201 and t_b.status_code == 201
            assert t_a.json()["task"]["id"] != t_b.json()["task"]["id"]
            # Each side's lookup returns only its own task.
            list_a = await client.get(
                "/tasks", params={"external_ref": "slack-thread-shared"},
                headers={"Authorization": f"Bearer {s1_key}"},
            )
            list_b = await client.get(
                "/tasks", params={"external_ref": "slack-thread-shared"},
                headers={"Authorization": f"Bearer {s2_key}"},
            )
            assert list_a.status_code == 200 and list_b.status_code == 200
            a_ids = [item["task"]["id"] for item in list_a.json()["tasks"]]
            b_ids = [item["task"]["id"] for item in list_b.json()["tasks"]]
            assert a_ids == [t_a.json()["task"]["id"]]
            assert b_ids == [t_b.json()["task"]["id"]]


async def test_direct_create_collision_returns_409() -> None:
    """Direct `POST /tasks` after an upsert with the same `external_ref` →
    `409 external_ref_in_use` (§12 / §9.2)."""
    app = create_app(Settings.for_tests())

    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            s_key, _ = await _register(client, "chat", "service")

            await client.post(
                "/tasks/upsert",
                headers={"Authorization": f"Bearer {s_key}"},
                json=_upsert_body(external_ref="slack-thread-xyz"),
            )

            collision = await client.post(
                "/tasks",
                headers={"Authorization": f"Bearer {s_key}"},
                json={"subject": "", "external_ref": "slack-thread-xyz", "metadata": {}},
            )
            assert collision.status_code == 409, collision.text
            assert collision.json()["error"]["code"] == "external_ref_in_use"


async def test_subtask_can_share_ref_with_root_under_different_initiator() -> None:
    """§6.4 sub-task scoping: root and child can both carry `slack-thread-abc`
    when they have different initiators. `GET /tasks?external_ref=…` is
    caller-scoped — service `S` sees only the root, agent `C` sees only the
    child."""
    app = create_app(Settings.for_tests())

    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            s_key, _ = await _register(client, "chat", "service")
            c_key, c_id = await _register(client, "support")
            _p_key, _p_id = await _register(client, "payments")

            # S opens root T1 with the shared ref.
            t1 = await client.post(
                "/tasks/upsert",
                headers={"Authorization": f"Bearer {s_key}"},
                json=_upsert_body(external_ref="slack-thread-abc"),
            )
            assert t1.status_code == 201, t1.text
            t1_id = t1.json()["task"]["id"]

            # S must add C to root before C can spawn a child of it.
            await client.post(
                f"/tasks/{t1_id}/events",
                headers={"Authorization": f"Bearer {s_key}"},
                json={
                    "event_type": "question",
                    "payload": {"to": [str(c_id)]},
                    "content": {"text": "lookup"},
                    "in_reply_to": None,
                    "metadata": {},
                },
            )

            # C spawns a child of T1 with the same ref string.
            t2 = await client.post(
                "/tasks",
                headers={"Authorization": f"Bearer {c_key}"},
                json={
                    "subject": "internal",
                    "parent_task_id": t1_id,
                    "external_ref": "slack-thread-abc",
                    "metadata": {},
                },
            )
            assert t2.status_code == 201, t2.text
            t2_id = t2.json()["task"]["id"]

            # GET /tasks?external_ref=slack-thread-abc — caller-scoped lookup.
            s_list = await client.get(
                "/tasks", params={"external_ref": "slack-thread-abc"},
                headers={"Authorization": f"Bearer {s_key}"},
            )
            assert s_list.status_code == 200
            assert [item["task"]["id"] for item in s_list.json()["tasks"]] == [t1_id]

            c_list = await client.get(
                "/tasks", params={"external_ref": "slack-thread-abc"},
                headers={"Authorization": f"Bearer {c_key}"},
            )
            assert c_list.status_code == 200
            assert [item["task"]["id"] for item in c_list.json()["tasks"]] == [t2_id]


async def test_get_tasks_no_filter_returns_empty() -> None:
    """With no filter and no tasks, `GET /tasks` returns `{tasks: []}`."""
    app = create_app(Settings.for_tests())

    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
            key, _ = await _register(client, "alice")

            resp = await client.get(
                "/tasks",
                headers={"Authorization": f"Bearer {key}"},
            )
            assert resp.status_code == 200
            assert resp.json() == {"tasks": []}


async def test_get_tasks_unmatched_external_ref_returns_empty() -> None:
    app = create_app(Settings.for_tests())

    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
            key, _ = await _register(client, "alice")

            resp = await client.get(
                "/tasks", params={"external_ref": "never-mapped"},
                headers={"Authorization": f"Bearer {key}"},
            )
            assert resp.status_code == 200
            assert resp.json() == {"tasks": []}


async def test_upsert_empty_external_ref_is_invalid() -> None:
    """Empty string would otherwise become a magic collision key — reject as
    `invalid_event_shape` instead."""
    app = create_app(Settings.for_tests())

    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
            key, _ = await _register(client, "alice", "service")

            resp = await client.post(
                "/tasks/upsert",
                headers={"Authorization": f"Bearer {key}"},
                json={"external_ref": "", "on_create": {"metadata": {}}},
            )
            assert resp.status_code == 422
            assert resp.json()["error"]["code"] == "invalid_event_shape"
