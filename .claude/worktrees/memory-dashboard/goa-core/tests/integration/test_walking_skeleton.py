from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import pytest

from goa.config import Settings
from goa.main import create_app
from goa.stream.hub import InMemoryStreamHub

from tests.integration._live_server import live_server


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Minimal SSE parser local to the test.
# ---------------------------------------------------------------------------

@dataclass
class SseFrame:
    event: str
    id: str | None
    data: Any


async def _iter_sse(response: httpx.Response) -> AsyncIterator[SseFrame]:
    name = "message"
    eid: str | None = None
    parts: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if parts or name != "message":
                raw = "\n".join(parts)
                try:
                    data: Any = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    data = raw
                yield SseFrame(event=name, id=eid, data=data)
            name, eid, parts = "message", None, []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":") if ":" in line else (line, "", "")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            name = value
        elif field == "id":
            eid = value
        elif field == "data":
            parts.append(value)


# ---------------------------------------------------------------------------
# Stream-consumer helper task
# ---------------------------------------------------------------------------

async def _consume(
    base_url: str,
    api_key: str,
    queue: asyncio.Queue[SseFrame],
    started: asyncio.Event,
) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        async with client.stream(
            "GET",
            "/stream",
            headers={"Authorization": f"Bearer {api_key}"},
        ) as response:
            response.raise_for_status()
            started.set()
            async for frame in _iter_sse(response):
                await queue.put(frame)


async def _wait_for_subscriber(
    hub: InMemoryStreamHub, participant_id: UUID, timeout: float = 5.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if hub.has_subscriber(participant_id):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"subscriber for {participant_id} never registered")


async def _next_event_frame(queue: asyncio.Queue[SseFrame]) -> SseFrame:
    """Drain `ping` frames; return the first `event` frame."""
    while True:
        frame = await asyncio.wait_for(queue.get(), timeout=5.0)
        if frame.event == "event":
            return frame


# ---------------------------------------------------------------------------
# The walking-skeleton scenario
# ---------------------------------------------------------------------------

async def test_walking_skeleton_two_participants_one_q_one_a() -> None:
    app = create_app(Settings.for_tests())
    hub: InMemoryStreamHub = app.state.ctx.hub

    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            # Register both participants.
            alice_resp = await client.post(
                "/participants", json={"type": "agent", "name": "alice"}
            )
            assert alice_resp.status_code == 201, alice_resp.text
            alice = alice_resp.json()
            bob_resp = await client.post(
                "/participants", json={"type": "agent", "name": "bob"}
            )
            assert bob_resp.status_code == 201, bob_resp.text
            bob = bob_resp.json()

            alice_key = alice["api_key"]
            bob_key = bob["api_key"]
            alice_id = UUID(alice["participant"]["id"])
            bob_id = UUID(bob["participant"]["id"])

            # Bob opens his stream, then we barrier on `has_subscriber` before
            # Alice posts. The barrier is mandatory: sse-starlette's generator
            # calls hub.subscribe lazily on the first iteration, so a naive
            # ordering races and Alice's POST can fan out before Bob is
            # registered.
            bob_q: asyncio.Queue[SseFrame] = asyncio.Queue()
            bob_started = asyncio.Event()
            bob_task = asyncio.create_task(_consume(base_url, bob_key, bob_q, bob_started))
            try:
                await asyncio.wait_for(bob_started.wait(), timeout=5.0)
                await _wait_for_subscriber(hub, bob_id)

                # POST /tasks creates the empty task; first event follows
                # via POST /tasks/{id}/events.
                create_resp = await client.post(
                    "/tasks",
                    headers={"Authorization": f"Bearer {alice_key}"},
                    json={"subject": "what is the answer?", "metadata": {}},
                )
                assert create_resp.status_code == 201, create_resp.text
                created = create_resp.json()
                task_id = UUID(created["task"]["id"])

                question_resp = await client.post(
                    f"/tasks/{task_id}/events",
                    headers={"Authorization": f"Bearer {alice_key}"},
                    json={
                        "event_type": "question",
                        "payload": {"to": [str(bob_id)]},
                        "content": {"text": "ping?", "data": None},
                        "in_reply_to": None,
                        "metadata": {},
                    },
                )
                assert question_resp.status_code == 201, question_resp.text
                question_id = UUID(question_resp.json()["event"]["id"])

                # Bob's stream: `participant_joined`, then `question`.
                joined_frame = await _next_event_frame(bob_q)
                assert joined_frame.data["event"]["event_type"] == "participant_joined"
                assert UUID(joined_frame.data["event"]["payload"]["participant_id"]) == bob_id

                question_frame = await _next_event_frame(bob_q)
                assert question_frame.data["event"]["event_type"] == "question"
                assert UUID(question_frame.data["event"]["id"]) == question_id

                # Alice opens her stream now (so she sees Bob's answer fan-out).
                alice_q: asyncio.Queue[SseFrame] = asyncio.Queue()
                alice_started = asyncio.Event()
                alice_task = asyncio.create_task(
                    _consume(base_url, alice_key, alice_q, alice_started)
                )
                try:
                    await asyncio.wait_for(alice_started.wait(), timeout=5.0)
                    await _wait_for_subscriber(hub, alice_id)

                    answer_resp = await client.post(
                        f"/tasks/{task_id}/events",
                        headers={"Authorization": f"Bearer {bob_key}"},
                        json={
                            "event_type": "answer",
                            "payload": {"answering": [str(question_id)]},
                            "content": {"text": "pong", "data": None},
                            "in_reply_to": None,
                            "metadata": {},
                        },
                    )
                    assert answer_resp.status_code == 201, answer_resp.text

                    answer_frame = await _next_event_frame(alice_q)
                    assert answer_frame.data["event"]["event_type"] == "answer"

                    # Stream-frame `task` summary must carry every §9.3 field.
                    summary = answer_frame.data["task"]
                    assert summary["pending_questions"] == []
                    assert summary["subject"] == "what is the answer?"
                    assert sorted(summary["participants"]) == sorted(
                        [str(alice_id), str(bob_id)]
                    )
                    assert summary["parent_task_id"] is None
                    assert "last_activity_at" in summary
                    assert summary["id"] == str(task_id)

                    get_resp = await client.get(
                        f"/tasks/{task_id}",
                        headers={"Authorization": f"Bearer {alice_key}"},
                    )
                    assert get_resp.status_code == 200, get_resp.text
                    payload = get_resp.json()
                    # Stages 2+3: pending is a sibling of task on the response.
                    assert payload["pending_questions"] == []
                    types = [e["event_type"] for e in payload["events"]]
                    assert types == ["participant_joined", "question", "answer"]
                finally:
                    alice_task.cancel()
                    try:
                        await alice_task
                    except (asyncio.CancelledError, BaseException):
                        pass
            finally:
                bob_task.cancel()
                try:
                    await bob_task
                except (asyncio.CancelledError, BaseException):
                    pass


# ---------------------------------------------------------------------------
# empty-task validity edge — `POST /tasks` then `GET /tasks/{id}` returns
# `events: []`; the first event flows through `POST /events`.
# ---------------------------------------------------------------------------

async def test_empty_task_then_first_event() -> None:
    app = create_app(Settings.for_tests())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=5.0
    ) as client:
        alice = (await client.post("/participants", json={"type": "agent", "name": "alice"})).json()
        bob = (await client.post("/participants", json={"type": "agent", "name": "bob"})).json()
        alice_key = alice["api_key"]
        alice_id = alice["participant"]["id"]
        bob_id = bob["participant"]["id"]

        create = await client.post(
            "/tasks",
            headers={"Authorization": f"Bearer {alice_key}"},
            json={"subject": "later", "metadata": {}},
        )
        assert create.status_code == 201, create.text
        task = create.json()["task"]
        task_id = task["id"]
        # Empty task is valid: only initiator is a participant.
        assert task["participants"] == [alice_id]

        # GET shows zero events and no pending pairs.
        got = (await client.get(
            f"/tasks/{task_id}", headers={"Authorization": f"Bearer {alice_key}"},
        )).json()
        assert got["events"] == []
        assert got["pending_questions"] == []

        # First question event auto-joins targets and lands cleanly.
        q_resp = await client.post(
            f"/tasks/{task_id}/events",
            headers={"Authorization": f"Bearer {alice_key}"},
            json={
                "event_type": "question",
                "payload": {"to": [bob_id]},
                "content": {"text": "?"},
                "in_reply_to": None,
                "metadata": {},
            },
        )
        assert q_resp.status_code == 201, q_resp.text

        # GET now shows participant_joined + question.
        got = (await client.get(
            f"/tasks/{task_id}", headers={"Authorization": f"Bearer {alice_key}"},
        )).json()
        types = [e["event_type"] for e in got["events"]]
        assert types == ["participant_joined", "question"]
        assert sorted(got["task"]["participants"]) == sorted([alice_id, bob_id])


# ---------------------------------------------------------------------------
# The non-streaming routes can stay on httpx ASGITransport — no live server.
# ---------------------------------------------------------------------------

async def test_get_task_returns_404_for_non_participant() -> None:
    app = create_app(Settings.for_tests())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=5.0
    ) as client:
        alice = (await client.post("/participants", json={"type": "agent", "name": "alice"})).json()
        bob = (await client.post("/participants", json={"type": "agent", "name": "bob"})).json()
        intruder = (await client.post("/participants", json={"type": "agent", "name": "evil"})).json()

        create = await client.post(
            "/tasks",
            headers={"Authorization": f"Bearer {alice['api_key']}"},
            json={"subject": "", "metadata": {}},
        )
        assert create.status_code == 201
        task_id = create.json()["task"]["id"]

        # Drop a first question so bob is a participant — keeps the original
        # scenario shape (non-participant tries to read a populated task).
        await client.post(
            f"/tasks/{task_id}/events",
            headers={"Authorization": f"Bearer {alice['api_key']}"},
            json={
                "event_type": "question",
                "payload": {"to": [bob["participant"]["id"]]},
                "content": {"text": "?", "data": None},
                "in_reply_to": None,
                "metadata": {},
            },
        )

        # Intruder cannot see the task — 404 (don't leak existence).
        resp = await client.get(
            f"/tasks/{task_id}",
            headers={"Authorization": f"Bearer {intruder['api_key']}"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "task_not_found"


async def test_unauthenticated_request_returns_401() -> None:
    app = create_app(Settings.for_tests())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=5.0
    ) as client:
        # No header at all.
        resp = await client.get("/tasks/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

        # Bogus token does not resolve to any participant.
        resp = await client.get(
            "/tasks/00000000-0000-0000-0000-000000000000",
            headers={"Authorization": "Bearer not-a-real-key"},
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"


async def test_post_event_returns_403_not_a_participant() -> None:
    app = create_app(Settings.for_tests())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=5.0
    ) as client:
        alice = (await client.post("/participants", json={"type": "agent", "name": "alice"})).json()
        bob = (await client.post("/participants", json={"type": "agent", "name": "bob"})).json()
        intruder = (await client.post("/participants", json={"type": "agent", "name": "evil"})).json()

        create = await client.post(
            "/tasks",
            headers={"Authorization": f"Bearer {alice['api_key']}"},
            json={"subject": "", "metadata": {}},
        )
        assert create.status_code == 201
        task_id = create.json()["task"]["id"]

        question_resp = await client.post(
            f"/tasks/{task_id}/events",
            headers={"Authorization": f"Bearer {alice['api_key']}"},
            json={
                "event_type": "question",
                "payload": {"to": [bob["participant"]["id"]]},
                "content": {"text": "?", "data": None},
                "in_reply_to": None,
                "metadata": {},
            },
        )
        assert question_resp.status_code == 201
        question_id = question_resp.json()["event"]["id"]

        # Intruder tries to append an answer to a task they aren't in.
        resp = await client.post(
            f"/tasks/{task_id}/events",
            headers={"Authorization": f"Bearer {intruder['api_key']}"},
            json={
                "event_type": "answer",
                "payload": {"answering": [question_id]},
                "content": {},
                "in_reply_to": None,
                "metadata": {},
            },
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "not_a_participant"


async def test_invalid_event_shape_returns_422() -> None:
    app = create_app(Settings.for_tests())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=5.0
    ) as client:
        alice = (await client.post("/participants", json={"type": "agent", "name": "alice"})).json()

        # Create an empty task first.
        create = await client.post(
            "/tasks",
            headers={"Authorization": f"Bearer {alice['api_key']}"},
            json={"subject": "", "metadata": {}},
        )
        assert create.status_code == 201
        task_id = create.json()["task"]["id"]

        # Empty `to` list on a question event violates the discriminated-union schema.
        resp = await client.post(
            f"/tasks/{task_id}/events",
            headers={"Authorization": f"Bearer {alice['api_key']}"},
            json={
                "event_type": "question",
                "payload": {"to": []},
                "content": {},
                "in_reply_to": None,
                "metadata": {},
            },
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "invalid_event_shape"
