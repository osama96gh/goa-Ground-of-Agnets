"""End-to-end HTTP coverage for `POST /tasks/{id}/close` (§8).

Service-level tests in `tests/unit/test_task_close.py` already cover
the orchestration; this file proves the wire round-trip: route
registration, 200 envelope, `invalid_state` 409 on post-close append,
external_ref slot release through `POST /tasks/upsert`, and the
`parent_closed` system-event fan-out on a live SSE stream.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest

from goa.config import Settings
from goa.main import create_app

from tests.integration._helpers import (
    SseFrame,
    consume,
    drain_until_event_type,
    wait_for_subscriber,
)
from tests.integration._live_server import live_server


pytestmark = pytest.mark.asyncio


async def _register(client: httpx.AsyncClient, name: str) -> tuple[str, UUID]:
    resp = await client.post("/participants", json={"type": "agent", "name": name})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["api_key"], UUID(body["participant"]["id"])


async def _create_task(
    client: httpx.AsyncClient,
    key: str,
    *,
    external_ref: str | None = None,
    parent_task_id: str | None = None,
) -> UUID:
    body: dict = {"subject": "", "metadata": {}}
    if external_ref is not None:
        body["external_ref"] = external_ref
    if parent_task_id is not None:
        body["parent_task_id"] = parent_task_id
    resp = await client.post(
        "/tasks", headers={"Authorization": f"Bearer {key}"}, json=body,
    )
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["task"]["id"])


async def test_close_endpoint_full_http_round_trip() -> None:
    """Register, create, close, observe `status='closed'`, then attempt
    append → 409 `invalid_state`."""
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            alice_key, _alice_id = await _register(client, "alice")
            _bob_key, bob_id = await _register(client, "bob")
            task_id = await _create_task(client, alice_key)

            close = await client.post(
                f"/tasks/{task_id}/close",
                headers={"Authorization": f"Bearer {alice_key}"},
            )
            assert close.status_code == 200, close.text
            assert close.json()["task"]["status"] == "closed"

            # Subsequent question append must be rejected.
            q = await client.post(
                f"/tasks/{task_id}/events",
                headers={"Authorization": f"Bearer {alice_key}"},
                json={
                    "event_type": "question",
                    "payload": {"to": [str(bob_id)]},
                    "content": {"text": "ping?"},
                    "in_reply_to": None,
                    "metadata": {},
                },
            )
            assert q.status_code == 409, q.text
            assert q.json()["error"]["code"] == "invalid_state"


async def test_close_is_idempotent_over_http() -> None:
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            alice_key, _ = await _register(client, "alice")
            task_id = await _create_task(client, alice_key)

            first = await client.post(
                f"/tasks/{task_id}/close",
                headers={"Authorization": f"Bearer {alice_key}"},
            )
            second = await client.post(
                f"/tasks/{task_id}/close",
                headers={"Authorization": f"Bearer {alice_key}"},
            )
            assert first.status_code == 200
            assert second.status_code == 200
            assert second.json()["task"]["id"] == first.json()["task"]["id"]
            assert second.json()["task"]["status"] == "closed"
            assert second.json()["task"]["updated_at"] == first.json()["task"]["updated_at"]


async def test_non_initiator_close_forbidden_over_http() -> None:
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            alice_key, _ = await _register(client, "alice")
            bob_key, _ = await _register(client, "bob")
            task_id = await _create_task(client, alice_key)

            resp = await client.post(
                f"/tasks/{task_id}/close",
                headers={"Authorization": f"Bearer {bob_key}"},
            )
            assert resp.status_code == 403, resp.text
            assert resp.json()["error"]["code"] == "forbidden_role"


async def test_external_ref_slot_released_through_upsert() -> None:
    """After close, `POST /tasks/upsert` with the same external_ref returns
    a new task (created=true), not the closed one."""
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            alice_key, _ = await _register(client, "alice")

            first_id = await _create_task(client, alice_key, external_ref="thread-x")
            await client.post(
                f"/tasks/{first_id}/close",
                headers={"Authorization": f"Bearer {alice_key}"},
            )

            upsert = await client.post(
                "/tasks/upsert",
                headers={"Authorization": f"Bearer {alice_key}"},
                json={
                    "external_ref": "thread-x",
                    "on_create": {"subject": "", "metadata": {}},
                },
            )
            assert upsert.status_code == 201, upsert.text
            body = upsert.json()
            assert body["created"] is True
            assert body["task"]["id"] != str(first_id)


async def test_parent_closed_fanout_on_sse() -> None:
    """Subscribe a child participant to the SSE stream, close the parent,
    and observe the `parent_closed` frame land in the child's stream."""
    app = create_app(Settings.for_tests())
    hub = app.state.ctx.hub
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            alice_key, alice_id = await _register(client, "alice")

            parent_id = await _create_task(client, alice_key)
            child_id = await _create_task(client, alice_key, parent_task_id=str(parent_id))

            q: asyncio.Queue[SseFrame] = asyncio.Queue()
            started = asyncio.Event()
            consumer = asyncio.create_task(consume(base_url, alice_key, q, started))
            try:
                await asyncio.wait_for(started.wait(), timeout=5.0)
                await wait_for_subscriber(hub, alice_id)

                close = await client.post(
                    f"/tasks/{parent_id}/close",
                    headers={"Authorization": f"Bearer {alice_key}"},
                )
                assert close.status_code == 200

                # alice is in both parent and child (she created them) — she
                # receives streams from both. Filter for the `parent_closed`
                # event landing in the child task.
                frame = await drain_until_event_type(q, "parent_closed", timeout=5.0)
                assert frame.data["task_id"] == str(child_id)
                assert frame.data["event"]["payload"]["task_id"] == str(parent_id)
                # `from` is null on system events.
                assert frame.data["event"]["from"] is None
            finally:
                consumer.cancel()
                try:
                    await consumer
                except (asyncio.CancelledError, Exception):
                    pass
