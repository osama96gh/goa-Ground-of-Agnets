"""E2e for the full event grammar: cancel_question, cancel_all_questions,
multi-target, opening info.

Mirrors the walking-skeleton pattern (live_server + SSE consumer + barrier on
`has_subscriber`) but exercises every event variant end-to-end through the
HTTP / SSE surface.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest

from goa.config import Settings
from goa.main import create_app
from goa.stream.hub import InMemoryStreamHub

from tests.integration._helpers import (
    SseFrame,
    consume,
    create_task_with_question,
    drain_until_event_type,
    next_event_frame,
    wait_for_subscriber,
)
from tests.integration._live_server import live_server


pytestmark = pytest.mark.asyncio


async def _register(client: httpx.AsyncClient, name: str) -> tuple[str, UUID]:
    resp = await client.post("/participants", json={"type": "agent", "name": name})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["api_key"], UUID(body["participant"]["id"])


async def _open_stream(
    base_url: str, api_key: str, participant_id: UUID, hub: InMemoryStreamHub,
) -> tuple[asyncio.Task[None], asyncio.Queue[SseFrame]]:
    queue: asyncio.Queue[SseFrame] = asyncio.Queue()
    started = asyncio.Event()
    task = asyncio.create_task(consume(base_url, api_key, queue, started))
    await asyncio.wait_for(started.wait(), timeout=5.0)
    await wait_for_subscriber(hub, participant_id)
    return task, queue


async def _cancel_stream(task: asyncio.Task[None]) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, BaseException):
        pass


# ---------------------------------------------------------------------------
# cancel_question — multi-target retract delivers to both
# ---------------------------------------------------------------------------

async def test_cancel_question_multi_target_e2e() -> None:
    app = create_app(Settings.for_tests())
    hub: InMemoryStreamHub = app.state.ctx.hub

    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            alice_key, alice_id = await _register(client, "alice")
            bob_key, bob_id = await _register(client, "bob")
            carol_key, carol_id = await _register(client, "carol")

            bob_task, bob_q = await _open_stream(base_url, bob_key, bob_id, hub)
            carol_task, carol_q = await _open_stream(base_url, carol_key, carol_id, hub)
            try:
                # Multi-target question.
                task_id, question_id = await create_task_with_question(
                    client, alice_key,
                    targets=[str(bob_id), str(carol_id)],
                    subject="broadcast q",
                )

                # Make sure both participants have already received the
                # `question` fanout before the initiator emits the cancel —
                # otherwise the cancel can land before the question on a slow
                # consumer.
                for q in (bob_q, carol_q):
                    await drain_until_event_type(q, "question")

                # Initiator cancels the question task-wide.
                cancel = await client.post(
                    f"/tasks/{task_id}/events",
                    headers={"Authorization": f"Bearer {alice_key}"},
                    json={
                        "event_type": "cancel_question",
                        "payload": {"retracts": [str(question_id)]},
                        "content": {},
                        "in_reply_to": None,
                        "metadata": {},
                    },
                )
                assert cancel.status_code == 201, cancel.text

                # Both streams observe the cancel event.
                for q in (bob_q, carol_q):
                    cancel_frame = await drain_until_event_type(q, "cancel_question")
                    assert cancel_frame.data["task"]["pending_questions"] == []

                # Pending is empty per GET /tasks/{id}.
                get_resp = await client.get(
                    f"/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {alice_key}"},
                )
                assert get_resp.status_code == 200
                # Stages 2+3: pending is a sibling of task on the response.
                assert get_resp.json()["pending_questions"] == []
            finally:
                await _cancel_stream(carol_task)
                await _cancel_stream(bob_task)


# ---------------------------------------------------------------------------
# cancel_all_questions — answer arriving after cancel does not reopen
# ---------------------------------------------------------------------------

async def test_cancel_all_questions_then_late_answer_e2e() -> None:
    app = create_app(Settings.for_tests())
    hub: InMemoryStreamHub = app.state.ctx.hub

    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            alice_key, alice_id = await _register(client, "alice")
            bob_key, bob_id = await _register(client, "bob")

            bob_task, bob_q = await _open_stream(base_url, bob_key, bob_id, hub)
            try:
                # Open question to bob.
                task_id, question_id = await create_task_with_question(
                    client, alice_key, targets=[str(bob_id)],
                )

                # Wait until bob's stream actually delivered the question
                # before we clear pending — otherwise the cancel can race in
                # ahead of the question on bob's queue.
                await drain_until_event_type(bob_q, "question")

                # Initiator clears all.
                clear = await client.post(
                    f"/tasks/{task_id}/events",
                    headers={"Authorization": f"Bearer {alice_key}"},
                    json={
                        "event_type": "cancel_all_questions",
                        "payload": {},
                        "content": {},
                        "in_reply_to": None,
                        "metadata": {},
                    },
                )
                assert clear.status_code == 201, clear.text

                cancel_frame = await drain_until_event_type(bob_q, "cancel_all_questions")
                assert cancel_frame.data["task"]["pending_questions"] == []

                # Bob answers anyway (late) — accepted but does not reopen.
                answer = await client.post(
                    f"/tasks/{task_id}/events",
                    headers={"Authorization": f"Bearer {bob_key}"},
                    json={
                        "event_type": "answer",
                        "payload": {"answering": [str(question_id)]},
                        "content": {"text": "late", "data": None},
                        "in_reply_to": None,
                        "metadata": {},
                    },
                )
                assert answer.status_code == 201, answer.text

                answer_frame = await drain_until_event_type(bob_q, "answer")
                assert answer_frame.data["task"]["pending_questions"] == []

                # Final state: pending stays empty in canonical history.
                get_resp = await client.get(
                    f"/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {alice_key}"},
                )
                assert get_resp.status_code == 200
                payload = get_resp.json()
                assert payload["pending_questions"] == []
                types = [e["event_type"] for e in payload["events"]]
                assert types == [
                    "participant_joined",
                    "question",
                    "cancel_all_questions",
                    "answer",
                ]
            finally:
                await _cancel_stream(bob_task)


# ---------------------------------------------------------------------------
# multi-target question — per-recipient close
# ---------------------------------------------------------------------------

async def test_multi_target_question_per_recipient_close_e2e() -> None:
    app = create_app(Settings.for_tests())
    hub: InMemoryStreamHub = app.state.ctx.hub

    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            alice_key, alice_id = await _register(client, "alice")
            bob_key, bob_id = await _register(client, "bob")
            carol_key, carol_id = await _register(client, "carol")

            bob_task, _bob_q = await _open_stream(base_url, bob_key, bob_id, hub)
            carol_task, _carol_q = await _open_stream(base_url, carol_key, carol_id, hub)
            try:
                task_id, question_id = await create_task_with_question(
                    client, alice_key, targets=[str(bob_id), str(carol_id)],
                )

                # Bob answers first.
                bob_ans = await client.post(
                    f"/tasks/{task_id}/events",
                    headers={"Authorization": f"Bearer {bob_key}"},
                    json={
                        "event_type": "answer",
                        "payload": {"answering": [str(question_id)]},
                        "content": {"text": "from bob", "data": None},
                        "in_reply_to": None,
                        "metadata": {},
                    },
                )
                assert bob_ans.status_code == 201

                # Carol's pair is still open per GET.
                resp = await client.get(
                    f"/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {alice_key}"},
                )
                assert resp.status_code == 200
                pending = resp.json()["pending_questions"]
                assert pending == [[str(question_id), str(carol_id)]]

                # Carol answers — both pairs gone.
                carol_ans = await client.post(
                    f"/tasks/{task_id}/events",
                    headers={"Authorization": f"Bearer {carol_key}"},
                    json={
                        "event_type": "answer",
                        "payload": {"answering": [str(question_id)]},
                        "content": {"text": "from carol", "data": None},
                        "in_reply_to": None,
                        "metadata": {},
                    },
                )
                assert carol_ans.status_code == 201

                resp = await client.get(
                    f"/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {alice_key}"},
                )
                assert resp.json()["pending_questions"] == []
            finally:
                await _cancel_stream(carol_task)
                await _cancel_stream(bob_task)


# ---------------------------------------------------------------------------
# opening info — announcement-only task
# ---------------------------------------------------------------------------

async def test_opening_info_announcement_only_task_e2e() -> None:
    app = create_app(Settings.for_tests())
    hub: InMemoryStreamHub = app.state.ctx.hub

    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            alice_key, alice_id = await _register(client, "alice")

            alice_task, alice_q = await _open_stream(base_url, alice_key, alice_id, hub)
            try:
                # create empty task, then append info.
                create = await client.post(
                    "/tasks",
                    headers={"Authorization": f"Bearer {alice_key}"},
                    json={"subject": "service health update", "metadata": {}},
                )
                assert create.status_code == 201, create.text
                task_id = UUID(create.json()["task"]["id"])

                info_resp = await client.post(
                    f"/tasks/{task_id}/events",
                    headers={"Authorization": f"Bearer {alice_key}"},
                    json={
                        "event_type": "info",
                        "payload": {},
                        "content": {"text": "all systems nominal", "data": None},
                        "in_reply_to": None,
                        "metadata": {},
                    },
                )
                assert info_resp.status_code == 201, info_resp.text

                # Alice (the initiator) sees the info on her stream.
                info_frame = await drain_until_event_type(alice_q, "info")
                assert info_frame.data["task"]["participants"] == [str(alice_id)]
                assert info_frame.data["task"]["pending_questions"] == []

                # GET confirms only the info event in the log; only initiator in participants.
                resp = await client.get(
                    f"/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {alice_key}"},
                )
                assert resp.status_code == 200
                payload = resp.json()
                assert payload["task"]["participants"] == [str(alice_id)]
                assert [e["event_type"] for e in payload["events"]] == ["info"]
            finally:
                await _cancel_stream(alice_task)
