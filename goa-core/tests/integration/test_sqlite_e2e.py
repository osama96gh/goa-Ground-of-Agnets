"""End-to-end tests against the SQLite-backed `Persistence`.

The protocol-level conformance suites already prove `SqliteAdapter`
honors the three Protocol contracts. These tests prove the *full stack*
(routes + service + projection + hub + SSE) runs against SQLite without
in-memory-only assumptions leaking through, and that state survives a
hub restart — Stage 1's headline durability win.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from goa.config import Settings
from goa.main import create_app
from goa.repos.persistence import Persistence

from tests.integration._helpers import (
    SseFrame,
    consume,
    next_event_frame,
    wait_for_subscriber,
)
from tests.integration._live_server import live_server


pytestmark = pytest.mark.asyncio


async def test_walking_skeleton_against_sqlite(tmp_path: Path) -> None:
    """Mirror of `test_walking_skeleton.py::test_walking_skeleton_two_participants_one_q_one_a`
    against a SQLite-backed Persistence — proves the full request path
    (registration, task creation, event append, SSE fan-out, pending
    drain) works end-to-end with the persistent adapter."""
    persistence = Persistence.sqlite(tmp_path / "goa.db")
    app = create_app(Settings.for_tests(), persistence=persistence)

    async with live_server(app) as base_url:
        hub = app.state.ctx.hub
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            alice = (await client.post(
                "/participants", json={"type": "agent", "name": "alice"}
            )).json()
            bob = (await client.post(
                "/participants", json={"type": "agent", "name": "bob"}
            )).json()
            alice_key, bob_key = alice["api_key"], bob["api_key"]
            alice_id = UUID(alice["participant"]["id"])
            bob_id = UUID(bob["participant"]["id"])

            bob_q: asyncio.Queue[SseFrame] = asyncio.Queue()
            bob_started = asyncio.Event()
            bob_task = asyncio.create_task(consume(base_url, bob_key, bob_q, bob_started))
            try:
                await asyncio.wait_for(bob_started.wait(), timeout=5.0)
                await wait_for_subscriber(hub, bob_id)

                create_resp = await client.post(
                    "/tasks",
                    headers={"Authorization": f"Bearer {alice_key}"},
                    json={"subject": "sqlite-skeleton", "metadata": {}},
                )
                assert create_resp.status_code == 201, create_resp.text
                task_id = UUID(create_resp.json()["task"]["id"])

                question_resp = await client.post(
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
                assert question_resp.status_code == 201, question_resp.text
                question_id = UUID(question_resp.json()["event"]["id"])
                # The hub-assigned seq is on the persisted event.
                assert question_resp.json()["event"]["seq"] >= 1

                joined = await next_event_frame(bob_q)
                assert joined.data["event"]["event_type"] == "participant_joined"
                qframe = await next_event_frame(bob_q)
                assert qframe.data["event"]["event_type"] == "question"
                assert UUID(qframe.data["event"]["id"]) == question_id

                alice_q: asyncio.Queue[SseFrame] = asyncio.Queue()
                alice_started = asyncio.Event()
                alice_task = asyncio.create_task(consume(base_url, alice_key, alice_q, alice_started))
                try:
                    await asyncio.wait_for(alice_started.wait(), timeout=5.0)
                    await wait_for_subscriber(hub, alice_id)

                    ans_resp = await client.post(
                        f"/tasks/{task_id}/events",
                        headers={"Authorization": f"Bearer {bob_key}"},
                        json={
                            "event_type": "answer",
                            "payload": {"answering": [str(question_id)]},
                            "content": {"text": "pong"},
                            "in_reply_to": None,
                            "metadata": {},
                        },
                    )
                    assert ans_resp.status_code == 201, ans_resp.text

                    answer_frame = await next_event_frame(alice_q)
                    assert answer_frame.data["event"]["event_type"] == "answer"
                    assert answer_frame.data["task"]["pending_questions"] == []

                    get_resp = await client.get(
                        f"/tasks/{task_id}",
                        headers={"Authorization": f"Bearer {alice_key}"},
                    )
                    assert get_resp.status_code == 200
                    types = [e["event_type"] for e in get_resp.json()["events"]]
                    assert types == ["participant_joined", "question", "answer"]

                    # Per-task monotonic seq is materialized in the persisted
                    # log — proves Step 1's contract holds through SQLite.
                    seqs = [e["seq"] for e in get_resp.json()["events"]]
                    assert seqs == [1, 2, 3]
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


async def test_state_survives_hub_restart(tmp_path: Path) -> None:
    """Headline Stage 1 win: kill the hub, restart it against the same
    file, and prior tasks + events are still there. In-memory cannot do
    this — that's the whole point.

    Subscribers are by definition transient (no client is connected
    while the hub is down), so the test only proves the *persisted*
    surface survives: registry, task, event log, pending projection
    (which cold-rebuilds from the events table).
    """
    db_path = tmp_path / "goa.db"
    settings = Settings.for_tests()

    # Phase 1 — first hub: register, create task, append events.
    persistence_1 = Persistence.sqlite(db_path)
    app_1 = create_app(settings, persistence=persistence_1)
    async with live_server(app_1) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            alice = (await client.post(
                "/participants", json={"type": "agent", "name": "alice"}
            )).json()
            bob = (await client.post(
                "/participants", json={"type": "agent", "name": "bob"}
            )).json()
            alice_key = alice["api_key"]
            alice_id = alice["participant"]["id"]
            bob_id = bob["participant"]["id"]

            create_resp = await client.post(
                "/tasks",
                headers={"Authorization": f"Bearer {alice_key}"},
                json={
                    "subject": "persisting work",
                    "external_ref": "thread-xyz",
                    "metadata": {"trace": "abc"},
                },
            )
            assert create_resp.status_code == 201
            task_id = create_resp.json()["task"]["id"]

            q_resp = await client.post(
                f"/tasks/{task_id}/events",
                headers={"Authorization": f"Bearer {alice_key}"},
                json={
                    "event_type": "question",
                    "payload": {"to": [bob_id]},
                    "content": {"text": "remember me?"},
                    "in_reply_to": None,
                    "metadata": {},
                },
            )
            assert q_resp.status_code == 201
            question_id = q_resp.json()["event"]["id"]

    # Hub 1 exited — its lifespan closed the SQLite connection on the way out.

    # Phase 2 — fresh hub against the same file. Same api_key (from above)
    # must still authenticate; the task and events must still be there;
    # the pending projection rebuilds from the persisted log on first read.
    persistence_2 = Persistence.sqlite(db_path)
    app_2 = create_app(settings, persistence=persistence_2)
    async with live_server(app_2) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
            # External_ref slot is still claimed: upsert returns the prior task.
            upsert_resp = await client.post(
                "/tasks/upsert",
                headers={"Authorization": f"Bearer {alice_key}"},
                json={
                    "external_ref": "thread-xyz",
                    "on_create": {"subject": "would-be-new", "metadata": {}},
                },
            )
            assert upsert_resp.status_code == 200, upsert_resp.text
            assert upsert_resp.json()["created"] is False
            assert upsert_resp.json()["task"]["id"] == task_id

            # The event log survived.
            get_resp = await client.get(
                f"/tasks/{task_id}",
                headers={"Authorization": f"Bearer {alice_key}"},
            )
            assert get_resp.status_code == 200, get_resp.text
            body = get_resp.json()
            types = [e["event_type"] for e in body["events"]]
            assert types == ["participant_joined", "question"]

            # Pending question is still open — projection cold-rebuilt from
            # the events table when bob's `/pending` was first computed.
            pend_resp = await client.get(
                "/pending", headers={"Authorization": f"Bearer {bob['api_key']}"},
            )
            assert pend_resp.status_code == 200
            pending = pend_resp.json()
            assert len(pending) == 1
            assert pending[0]["question_event_id"] == question_id
            assert pending[0]["task_id"] == task_id
