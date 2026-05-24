"""Spec §5 sub-task privacy scenario: a chat service `S` opens task T1 to
support agent `C`; `C` privately consults payments agent `P` via sub-task T2.
`S` learns that a child was spawned (via `child_task_created`) but cannot
read T2; `P` cannot read T1."""

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


async def _register(client: httpx.AsyncClient, name: str, type_: str = "agent") -> tuple[str, UUID]:
    resp = await client.post("/participants", json={"type": type_, "name": name})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["api_key"], UUID(body["participant"]["id"])


async def _open_stream(
    base_url: str, hub: InMemoryStreamHub, participant_id: UUID, api_key: str
) -> tuple[asyncio.Queue[SseFrame], asyncio.Task[None]]:
    queue: asyncio.Queue[SseFrame] = asyncio.Queue()
    started = asyncio.Event()
    task = asyncio.create_task(consume(base_url, api_key, queue, started))
    await asyncio.wait_for(started.wait(), timeout=5.0)
    await wait_for_subscriber(hub, participant_id)
    return queue, task


async def test_slack_support_payments_subtask_scenario() -> None:
    app = create_app(Settings.for_tests())
    hub: InMemoryStreamHub = app.state.ctx.hub

    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            s_key, s_id = await _register(client, "chat-service", "service")
            c_key, c_id = await _register(client, "support-agent")
            p_key, p_id = await _register(client, "payments-agent")

            # All three open their streams up front. The barrier on
            # has_subscriber prevents fanout from racing the subscription.
            s_q, s_task = await _open_stream(base_url, hub, s_id, s_key)
            c_q, c_task = await _open_stream(base_url, hub, c_id, c_key)
            p_q, p_task = await _open_stream(base_url, hub, p_id, p_key)

            try:
                # ----------------------------------------------------------
                # 1. S opens T1 targeting C.
                # ----------------------------------------------------------
                t1_id, t1_q1_id = await create_task_with_question(
                    client, s_key,
                    targets=[str(c_id)],
                    subject="refund inquiry",
                    text="where's my refund?",
                )

                # C sees the question land in T1.
                t1_question_for_c = await drain_until_event_type(c_q, "question")
                assert UUID(t1_question_for_c.data["task_id"]) == t1_id

                # ----------------------------------------------------------
                # 2. C spawns sub-task T2 targeting P.
                # ----------------------------------------------------------
                t2_id, t2_q2_id = await create_task_with_question(
                    client, c_key,
                    targets=[str(p_id)],
                    subject="internal lookup",
                    parent_task_id=str(t1_id),
                    text="any refund record?",
                )

                # ----------------------------------------------------------
                # 3. Privacy assertions
                # ----------------------------------------------------------
                # S sees `child_task_created` in T1 (same parent, S is in T1).
                child_event_for_s = await drain_until_event_type(s_q, "child_task_created")
                assert UUID(child_event_for_s.data["task_id"]) == t1_id
                evt = child_event_for_s.data["event"]
                assert evt["from"] is None
                assert UUID(evt["payload"]["task_id"]) == t2_id
                assert UUID(evt["payload"]["spawned_by"]) == c_id
                assert evt["payload"]["subject"] == "internal lookup"
                # T1 itself is a root — its summary's parent_task_id is null.
                assert child_event_for_s.data["task"]["parent_task_id"] is None

                # P does NOT see anything about T1. Drain P's queue: it should
                # only have its own T2 question (and the ParticipantJoined
                # auto-join system event for itself).
                p_question = await drain_until_event_type(p_q, "question")
                assert UUID(p_question.data["task_id"]) == t2_id
                # Confirm the P stream never carries a T1-scoped frame: any
                # subsequent frame in the queue (with a small idle window)
                # must not be tagged with T1's id.
                with pytest.raises(asyncio.TimeoutError):
                    while True:
                        more = await asyncio.wait_for(p_q.get(), timeout=0.2)
                        if more.event != "event":
                            continue
                        assert UUID(more.data["task_id"]) != t1_id

                # S calling GET /tasks/T2 → 404 task_not_found.
                s_t2_resp = await client.get(
                    f"/tasks/{t2_id}",
                    headers={"Authorization": f"Bearer {s_key}"},
                )
                assert s_t2_resp.status_code == 404
                assert s_t2_resp.json()["error"]["code"] == "task_not_found"

                # P calling GET /tasks/T1 → 404 task_not_found.
                p_t1_resp = await client.get(
                    f"/tasks/{t1_id}",
                    headers={"Authorization": f"Bearer {p_key}"},
                )
                assert p_t1_resp.status_code == 404
                assert p_t1_resp.json()["error"]["code"] == "task_not_found"

                # C calling GET /tasks/T2 → 200 with parent_task_id == T1.
                c_t2_resp = await client.get(
                    f"/tasks/{t2_id}",
                    headers={"Authorization": f"Bearer {c_key}"},
                )
                assert c_t2_resp.status_code == 200, c_t2_resp.text
                assert UUID(c_t2_resp.json()["task"]["parent_task_id"]) == t1_id

                # ----------------------------------------------------------
                # 4. GET /tasks/{T1}/children
                # ----------------------------------------------------------
                # C (in T2) sees [T2].
                c_children_resp = await client.get(
                    f"/tasks/{t1_id}/children",
                    headers={"Authorization": f"Bearer {c_key}"},
                )
                assert c_children_resp.status_code == 200, c_children_resp.text
                c_children = c_children_resp.json()["children"]
                assert [UUID(item["task"]["id"]) for item in c_children] == [t2_id]

                # S (not in T2) sees []. S is in T1 but the filter "children
                # the caller participates in" excludes T2.
                s_children_resp = await client.get(
                    f"/tasks/{t1_id}/children",
                    headers={"Authorization": f"Bearer {s_key}"},
                )
                assert s_children_resp.status_code == 200, s_children_resp.text
                assert s_children_resp.json()["children"] == []

                # ----------------------------------------------------------
                # 5. Complete the §5 flow: P answers T2.Q2; C answers T1.Q1.
                # ----------------------------------------------------------
                p_answer = await client.post(
                    f"/tasks/{t2_id}/events",
                    headers={"Authorization": f"Bearer {p_key}"},
                    json={
                        "event_type": "answer",
                        "payload": {"answering": [str(t2_q2_id)]},
                        "content": {"text": "yes — refunded yesterday", "data": None},
                        "in_reply_to": None,
                        "metadata": {},
                    },
                )
                assert p_answer.status_code == 201, p_answer.text

                c_t2_answer = await drain_until_event_type(c_q, "answer")
                assert UUID(c_t2_answer.data["task_id"]) == t2_id

                c_answer = await client.post(
                    f"/tasks/{t1_id}/events",
                    headers={"Authorization": f"Bearer {c_key}"},
                    json={
                        "event_type": "answer",
                        "payload": {"answering": [str(t1_q1_id)]},
                        "content": {"text": "your refund posted yesterday", "data": None},
                        "in_reply_to": None,
                        "metadata": {},
                    },
                )
                assert c_answer.status_code == 201, c_answer.text

                s_t1_answer = await drain_until_event_type(s_q, "answer")
                assert UUID(s_t1_answer.data["task_id"]) == t1_id
            finally:
                for t in (s_task, c_task, p_task):
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, BaseException):
                        pass


async def test_create_subtask_with_unknown_parent_returns_403() -> None:
    app = create_app(Settings.for_tests())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=5.0
    ) as client:
        alice = (await client.post("/participants", json={"type": "agent", "name": "alice"})).json()
        bob = (await client.post("/participants", json={"type": "agent", "name": "bob"})).json()

        bogus_parent_id = "00000000-0000-0000-0000-000000000000"
        resp = await client.post(
            "/tasks",
            headers={"Authorization": f"Bearer {alice['api_key']}"},
            json={"subject": "", "parent_task_id": bogus_parent_id, "metadata": {}},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "parent_task_not_visible"


async def test_create_subtask_when_caller_not_in_parent_returns_403() -> None:
    app = create_app(Settings.for_tests())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=5.0
    ) as client:
        alice = (await client.post("/participants", json={"type": "agent", "name": "alice"})).json()
        bob = (await client.post("/participants", json={"type": "agent", "name": "bob"})).json()
        eve = (await client.post("/participants", json={"type": "agent", "name": "eve"})).json()

        # Alice opens T1 to bob; eve is not in T1.
        t1_id, _ = await create_task_with_question(
            client, alice["api_key"], targets=[bob["participant"]["id"]],
        )

        # Eve tries to spawn a child of T1.
        resp = await client.post(
            "/tasks",
            headers={"Authorization": f"Bearer {eve['api_key']}"},
            json={"subject": "", "parent_task_id": str(t1_id), "metadata": {}},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "parent_task_not_visible"
