"""End-to-end tests against a Postgres-backed `Persistence` (+ MinIO for blobs).

Mirror of `test_sqlite_e2e.py`. The protocol-contract suite already
proves each adapter individually honors its Protocol; these tests
prove the *full stack* (routes + service + projection + hub + SSE)
works against the Postgres+S3 composition and that state survives a
hub restart.

Skipped automatically when Docker / `testcontainers` is unavailable
— see [tests/unit/_postgres_factory.py](../unit/_postgres_factory.py).
"""

from __future__ import annotations

import asyncio
import uuid
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
from tests.unit._postgres_factory import (
    _drop_database,
    _get_minio,
    _per_test_dsn,
)


pytestmark = pytest.mark.asyncio


async def _build_supabase_like_persistence() -> tuple[Persistence, str]:
    """Build a `Persistence.supabase(...)` against the testcontainer
    Postgres + MinIO. Returns the bundle and the per-test db_name so
    the caller can drop it after the test."""
    try:
        import aioboto3
        from botocore.config import Config
    except ImportError:
        pytest.skip("aioboto3 not installed — install the [s3] extra")

    dsn, db_name = await _per_test_dsn()
    endpoint, access_key, secret_key = _get_minio()
    region = "us-east-1"
    bucket = f"goa-e2e-{uuid.uuid4().hex[:12]}"

    # Bootstrap the bucket up front — the adapter does not create it.
    session = aioboto3.Session()
    async with session.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    ) as bootstrap:
        await bootstrap.create_bucket(Bucket=bucket)

    persistence = Persistence.supabase(
        database_url=dsn,
        storage_endpoint=endpoint,
        storage_bucket=bucket,
        storage_region=region,
        storage_access_key=access_key,
        storage_secret_key=secret_key,
    )
    return persistence, db_name


async def test_walking_skeleton_against_postgres(tmp_path: Path) -> None:
    """The full request path (registration, task creation, event append,
    SSE fan-out, pending drain) works end-to-end with the Postgres+S3
    composition."""
    persistence, db_name = await _build_supabase_like_persistence()
    app = create_app(Settings.for_tests(), persistence=persistence)

    try:
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
                        json={"subject": "pg-skeleton", "metadata": {}},
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
                    assert question_resp.json()["event"]["seq"] >= 1

                    joined = await next_event_frame(bob_q)
                    assert joined.data["event"]["event_type"] == "participant_joined"
                    qframe = await next_event_frame(bob_q)
                    assert qframe.data["event"]["event_type"] == "question"
                    assert UUID(qframe.data["event"]["id"]) == question_id

                    alice_q: asyncio.Queue[SseFrame] = asyncio.Queue()
                    alice_started = asyncio.Event()
                    alice_task = asyncio.create_task(
                        consume(base_url, alice_key, alice_q, alice_started)
                    )
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
    finally:
        await _drop_database(db_name)


async def test_state_survives_hub_restart_against_postgres(tmp_path: Path) -> None:
    """The headline durability win: kill the hub, restart it against the
    same Postgres database, prior tasks + events are still there. The
    blob store rebinds to the same bucket — uploaded objects survive too.
    """
    try:
        import aioboto3
        from botocore.config import Config
    except ImportError:
        pytest.skip("aioboto3 not installed — install the [s3] extra")

    dsn, db_name = await _per_test_dsn()
    endpoint, access_key, secret_key = _get_minio()
    region = "us-east-1"
    bucket = f"goa-restart-{uuid.uuid4().hex[:12]}"

    session = aioboto3.Session()
    async with session.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    ) as bootstrap:
        await bootstrap.create_bucket(Bucket=bucket)

    settings = Settings.for_tests()

    try:
        # ---- Phase 1: first hub. Register, create task, append events.
        persistence_1 = Persistence.supabase(
            database_url=dsn,
            storage_endpoint=endpoint,
            storage_bucket=bucket,
            storage_region=region,
            storage_access_key=access_key,
            storage_secret_key=secret_key,
        )
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
                bob_id = bob["participant"]["id"]

                create_resp = await client.post(
                    "/tasks",
                    headers={"Authorization": f"Bearer {alice_key}"},
                    json={
                        "subject": "persisting work",
                        "external_ref": "thread-pg",
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

        # ---- Phase 2: fresh hub against the same database. Same API
        # key still authenticates; external_ref slot still claimed;
        # pending projection rebuilds from the persisted log.
        persistence_2 = Persistence.supabase(
            database_url=dsn,
            storage_endpoint=endpoint,
            storage_bucket=bucket,
            storage_region=region,
            storage_access_key=access_key,
            storage_secret_key=secret_key,
        )
        app_2 = create_app(settings, persistence=persistence_2)
        async with live_server(app_2) as base_url:
            async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
                upsert_resp = await client.post(
                    "/tasks/upsert",
                    headers={"Authorization": f"Bearer {alice_key}"},
                    json={
                        "external_ref": "thread-pg",
                        "on_create": {"subject": "would-be-new", "metadata": {}},
                    },
                )
                assert upsert_resp.status_code == 200, upsert_resp.text
                assert upsert_resp.json()["created"] is False
                assert upsert_resp.json()["task"]["id"] == task_id

                get_resp = await client.get(
                    f"/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {alice_key}"},
                )
                assert get_resp.status_code == 200, get_resp.text
                body = get_resp.json()
                types = [e["event_type"] for e in body["events"]]
                assert types == ["participant_joined", "question"]

                pend_resp = await client.get(
                    "/pending", headers={"Authorization": f"Bearer {bob['api_key']}"},
                )
                assert pend_resp.status_code == 200
                pending = pend_resp.json()
                assert len(pending) == 1
                assert pending[0]["question_event_id"] == question_id
                assert pending[0]["task_id"] == task_id
    finally:
        await _drop_database(db_name)
