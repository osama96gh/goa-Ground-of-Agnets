"""Stage A — admin firehose + admin-scoped reads.

Asserts:
- Admin routes exist only when `Settings.admin_token` is set; otherwise 404.
- Bearer mismatch returns 401.
- Admin firehose sees events from tasks the admin caller is not a participant of.
- `GET /admin/tasks` returns child tasks too when `parent_id` is supplied.
- `GET /admin/tasks/{id}` ignores participant gating (admins read every task).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import httpx
import pytest

from goa.config import Settings
from goa.main import create_app

from tests.integration._helpers import (
    create_task_with_question,
    iter_sse,
    SseFrame,
)
from tests.integration._live_server import live_server


pytestmark = pytest.mark.asyncio


ADMIN_TOKEN = "test-admin-token"


def _admin_settings() -> Settings:
    return Settings.for_tests(admin_token=ADMIN_TOKEN)


async def _register(http: httpx.AsyncClient, **body) -> tuple[UUID, str]:
    resp = await http.post("/participants", json=body)
    resp.raise_for_status()
    decoded = resp.json()
    return UUID(decoded["participant"]["id"]), decoded["api_key"]


def _admin_auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _participant_auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ---------------------------------------------------------------------------
# Router gating
# ---------------------------------------------------------------------------

async def test_admin_routes_404_when_token_unset() -> None:
    app = create_app(Settings.for_tests())  # no admin_token
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            resp = await http.get("/admin/tasks", headers=_admin_auth())
            assert resp.status_code == 404


async def test_admin_routes_reject_wrong_token() -> None:
    app = create_app(_admin_settings())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            resp = await http.get(
                "/admin/tasks",
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert resp.status_code == 401


async def test_admin_routes_reject_missing_bearer() -> None:
    app = create_app(_admin_settings())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            resp = await http.get("/admin/tasks")
            assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /admin/tasks + /admin/tasks/{id}
# ---------------------------------------------------------------------------

async def test_admin_list_tasks_sees_everything() -> None:
    app = create_app(_admin_settings())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            init_id, key_init = await _register(http, type="agent", name="init")
            target_id, _ = await _register(http, type="agent", name="target")

            parent_id, _ = await create_task_with_question(
                http, key_init, targets=[str(target_id)], subject="parent",
            )

            # Sub-task spawned by target.
            other_id, key_other = await _register(http, type="agent", name="other")
            await create_task_with_question(
                http, key_init, targets=[str(other_id)], subject="sibling",
            )

            # Default top-level only.
            resp = await http.get("/admin/tasks", headers=_admin_auth())
            resp.raise_for_status()
            top = {UUID(item["task"]["id"]) for item in resp.json()["tasks"]}
            assert parent_id in top  # admin sees the parent without being a participant

            # has_pending=true filter. Wire shape (Stages 2+3): list items are
            # {task, pending_questions}, with pending derived from the projection.
            resp = await http.get(
                "/admin/tasks",
                params={"has_pending": "true"},
                headers=_admin_auth(),
            )
            assert all(
                len(item["pending_questions"]) > 0 for item in resp.json()["tasks"]
            )


async def test_admin_get_task_ignores_participant_gating() -> None:
    """Regular `GET /tasks/{id}` returns 404 to non-participants. Admin route
    must read it without that gate."""
    app = create_app(_admin_settings())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            _, key_init = await _register(http, type="agent", name="init")
            target_id, _ = await _register(http, type="agent", name="target")

            task_id, _ = await create_task_with_question(
                http, key_init, targets=[str(target_id)], text="secret",
            )

            # Admin sees the task + events even though the admin token isn't a
            # participant.
            resp = await http.get(f"/admin/tasks/{task_id}", headers=_admin_auth())
            resp.raise_for_status()
            decoded = resp.json()
            assert decoded["task"]["id"] == str(task_id)
            assert any(
                ev["event_type"] == "question" for ev in decoded["events"]
            )


# ---------------------------------------------------------------------------
# /admin/participants
# ---------------------------------------------------------------------------

async def test_admin_list_participants_works_with_admin_token() -> None:
    app = create_app(_admin_settings())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            _, _ = await _register(
                http, type="agent", name="payments-agent",
                capabilities=["payments"],
            )
            resp = await http.get(
                "/admin/participants",
                params=[("capability", "payments")],
                headers=_admin_auth(),
            )
            resp.raise_for_status()
            assert any(
                p["name"] == "payments-agent"
                for p in resp.json()["participants"]
            )


async def test_admin_participant_crud_roundtrip() -> None:
    """Create → PATCH → DELETE through admin endpoints. Covers the happy
    path for all three write endpoints in one round-trip."""
    app = create_app(_admin_settings())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            # Create
            resp = await http.post(
                "/admin/participants",
                json={
                    "type": "agent",
                    "name": "crud-agent",
                    "description": "test",
                    "capabilities": ["foo", "bar"],
                },
                headers=_admin_auth(),
            )
            assert resp.status_code == 201
            created = resp.json()
            participant_id = created["participant"]["id"]
            assert created["api_key"]  # returned once
            assert created["participant"]["name"] == "crud-agent"

            # PATCH — partial update only touches supplied fields
            resp = await http.patch(
                f"/admin/participants/{participant_id}",
                json={"name": "crud-agent-v2", "capabilities": ["baz"]},
                headers=_admin_auth(),
            )
            assert resp.status_code == 200
            updated = resp.json()
            assert updated["name"] == "crud-agent-v2"
            assert updated["capabilities"] == ["baz"]
            assert updated["description"] == "test"  # untouched

            # PATCH on non-existent id → 404
            resp = await http.patch(
                "/admin/participants/00000000-0000-0000-0000-000000000000",
                json={"name": "ghost"},
                headers=_admin_auth(),
            )
            assert resp.status_code == 404

            # DELETE — first call removes, second is idempotent
            resp = await http.delete(
                f"/admin/participants/{participant_id}",
                headers=_admin_auth(),
            )
            assert resp.status_code == 204
            resp = await http.delete(
                f"/admin/participants/{participant_id}",
                headers=_admin_auth(),
            )
            assert resp.status_code == 204

            # Verify gone
            resp = await http.get("/admin/participants", headers=_admin_auth())
            resp.raise_for_status()
            assert not any(
                p["id"] == participant_id for p in resp.json()["participants"]
            )


async def test_admin_participant_write_endpoints_reject_wrong_token() -> None:
    """All three write endpoints must reject non-admin callers."""
    app = create_app(_admin_settings())
    bad = {"Authorization": "Bearer wrong-token"}
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            for resp in [
                await http.post(
                    "/admin/participants",
                    json={"type": "agent", "name": "x"},
                    headers=bad,
                ),
                await http.patch(
                    "/admin/participants/00000000-0000-0000-0000-000000000000",
                    json={"name": "x"},
                    headers=bad,
                ),
                await http.delete(
                    "/admin/participants/00000000-0000-0000-0000-000000000000",
                    headers=bad,
                ),
            ]:
                assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /admin/stream
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _stream_admin(
    base_url: str, *, last_event_id: str | None = None,
) -> AsyncIterator[AsyncIterator[SseFrame]]:
    headers = _admin_auth()
    if last_event_id is not None:
        headers["Last-Event-ID"] = last_event_id
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        async with client.stream("GET", "/admin/stream", headers=headers) as response:
            response.raise_for_status()
            yield iter_sse(response)


async def _next_event(frames: AsyncIterator[SseFrame], timeout: float = 5.0) -> SseFrame:
    async def _pull() -> SseFrame:
        async for frame in frames:
            if frame.event == "event":
                return frame
        raise AssertionError("stream closed before yielding an event")
    return await asyncio.wait_for(_pull(), timeout=timeout)


async def test_admin_stream_sees_every_task() -> None:
    """Admin firehose must see events for tasks the admin is not a participant of."""
    app = create_app(_admin_settings())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as http:
            _, key_init = await _register(http, type="agent", name="init")
            target_id, _ = await _register(http, type="agent", name="target")

            async with _stream_admin(base_url) as frames:
                # Wait for the admin subscription to register.
                await asyncio.sleep(0.1)

                _, question_id = await create_task_with_question(
                    http, key_init,
                    targets=[str(target_id)],
                    text="admin should see this",
                )

                # Drain frames; the question event must arrive on the firehose.
                seen: list[UUID] = []
                while question_id not in seen:
                    frame = await _next_event(frames)
                    seen.append(UUID(frame.data["event"]["id"]))


async def test_admin_stream_rejects_wrong_token() -> None:
    app = create_app(_admin_settings())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
            resp = await client.get(
                "/admin/stream",
                headers={"Authorization": "Bearer wrong"},
            )
            assert resp.status_code == 401
