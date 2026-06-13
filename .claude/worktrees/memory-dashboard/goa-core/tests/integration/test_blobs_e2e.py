"""End-to-end coverage for §6.5 / §9.4 — multi-modal attachments.

Blobs upload via `POST /tasks/{id}/blobs` (task-scoped at upload time);
events flow through `POST /tasks/{id}/events` and may reference uploaded
blob ids in `payload.attachments`."""

from __future__ import annotations

import hashlib
from uuid import UUID

import httpx
import pytest

from goa.config import Settings
from goa.main import create_app


pytestmark = pytest.mark.asyncio


async def _client() -> httpx.AsyncClient:
    app = create_app(Settings.for_tests(blob_max_bytes=4 * 1024 * 1024))
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=10.0
    )


async def _register(client: httpx.AsyncClient, name: str) -> tuple[str, str]:
    resp = await client.post("/participants", json={"type": "agent", "name": name})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["api_key"], body["participant"]["id"]


async def _create_task(
    client: httpx.AsyncClient, api_key: str, *, subject: str = "",
) -> str:
    resp = await client.post(
        "/tasks",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"subject": subject, "metadata": {}},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["task"]["id"]


async def _upload_blob(
    client: httpx.AsyncClient,
    api_key: str,
    task_id: str,
    *,
    filename: str,
    body: bytes,
    mime_type: str = "application/octet-stream",
) -> httpx.Response:
    return await client.post(
        f"/tasks/{task_id}/blobs",
        headers={"Authorization": f"Bearer {api_key}"},
        files={"file": (filename, body, mime_type)},
    )


async def _append_question(
    client: httpx.AsyncClient,
    api_key: str,
    task_id: str,
    *,
    to: list[str],
    text: str = "?",
    attachments: list[dict] | None = None,
) -> httpx.Response:
    return await client.post(
        f"/tasks/{task_id}/events",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "event_type": "question",
            "payload": {"to": to},
            "content": {"text": text, "attachments": attachments or []},
            "in_reply_to": None,
            "metadata": {},
        },
    )


async def test_upload_then_reference_in_event_then_download() -> None:
    """Golden path: alice creates a task, uploads a blob bound to it,
    references it in a question to bob, bob fetches the task and downloads
    the blob's bytes."""
    async with await _client() as client:
        alice_key, alice_id = await _register(client, "alice")
        bob_key, bob_id = await _register(client, "bob")

        task_id = await _create_task(client, alice_key, subject="look at this")

        payload = b"\x89PNG\r\n\x1a\n" + b"hello goa attachments" * 16
        upload = await _upload_blob(
            client, alice_key, task_id,
            filename="hello.png", body=payload, mime_type="image/png",
        )
        assert upload.status_code == 201, upload.text
        att = upload.json()
        assert att["filename"] == "hello.png"
        assert att["mime_type"] == "image/png"
        assert att["size_bytes"] == len(payload)
        assert att["sha256"] == hashlib.sha256(payload).hexdigest()
        blob_id = att["blob_id"]

        # Alice appends a question that references the blob.
        q = await _append_question(
            client, alice_key, task_id, to=[bob_id], text="what is this?",
            attachments=[att],
        )
        assert q.status_code == 201, q.text

        # Bob can see the attachment metadata on the question.
        get = await client.get(
            f"/tasks/{task_id}",
            headers={"Authorization": f"Bearer {bob_key}"},
        )
        assert get.status_code == 200, get.text
        events = get.json()["events"]
        question = next(e for e in events if e["event_type"] == "question")
        assert question["content"]["attachments"][0]["blob_id"] == blob_id

        # Bob can download the blob (he is a participant of a task linked to it).
        dl = await client.get(
            f"/blobs/{blob_id}",
            headers={"Authorization": f"Bearer {bob_key}"},
        )
        assert dl.status_code == 200
        assert dl.content == payload
        assert dl.headers["content-type"].startswith("image/png")

        # And the meta endpoint round-trips.
        meta = await client.get(
            f"/blobs/{blob_id}/meta",
            headers={"Authorization": f"Bearer {bob_key}"},
        )
        assert meta.status_code == 200
        assert meta.json()["sha256"] == att["sha256"]


async def test_upload_too_large_returns_413() -> None:
    async with await _client() as client:
        alice_key, _ = await _register(client, "alice")
        task_id = await _create_task(client, alice_key)
        # 4 MB cap configured above; send 4 MB + 1 byte.
        body = b"a" * (4 * 1024 * 1024 + 1)
        resp = await _upload_blob(
            client, alice_key, task_id, filename="big.bin", body=body,
        )
        assert resp.status_code == 413
        assert resp.json()["error"]["code"] == "blob_too_large"


async def test_event_referencing_unknown_blob_returns_404() -> None:
    async with await _client() as client:
        alice_key, _ = await _register(client, "alice")
        bob_key, bob_id = await _register(client, "bob")

        bogus = {
            "blob_id": "00000000-0000-0000-0000-000000000000",
            "filename": "ghost.bin",
            "mime_type": "application/octet-stream",
            "size_bytes": 0,
            "sha256": "0" * 64,
        }
        task_id = await _create_task(client, alice_key)
        resp = await _append_question(
            client, alice_key, task_id, to=[bob_id], attachments=[bogus],
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "blob_not_found"


async def test_event_referencing_blob_bound_to_other_task_returns_403() -> None:
    """A blob is bound to exactly one task at upload time. Referencing it
    from an event in a different task is rejected with `blob_forbidden`."""
    async with await _client() as client:
        alice_key, _alice_id = await _register(client, "alice")
        _bob_key, bob_id = await _register(client, "bob")
        carol_key, _carol_id = await _register(client, "carol")

        # Carol uploads a blob bound to her own task.
        carol_task_id = await _create_task(client, carol_key)
        upload = await _upload_blob(
            client, carol_key, carol_task_id,
            filename="secret.txt", body=b"top secret", mime_type="text/plain",
        )
        assert upload.status_code == 201
        carol_att = upload.json()

        # Alice creates her own task and tries to reference Carol's blob. Rejected.
        alice_task_id = await _create_task(client, alice_key)
        resp = await _append_question(
            client, alice_key, alice_task_id, to=[bob_id], attachments=[carol_att],
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "blob_forbidden"


async def test_non_participant_cannot_download_blob() -> None:
    async with await _client() as client:
        alice_key, _alice_id = await _register(client, "alice")
        _bob_key, bob_id = await _register(client, "bob")
        evil_key, _ = await _register(client, "evil")

        task_id = await _create_task(client, alice_key)
        upload = await _upload_blob(
            client, alice_key, task_id, filename="x.bin", body=b"x" * 32,
        )
        att = upload.json()
        blob_id = att["blob_id"]

        await _append_question(
            client, alice_key, task_id, to=[bob_id], attachments=[att],
        )

        # Evil is in no task bound to the blob.
        resp = await client.get(
            f"/blobs/{blob_id}",
            headers={"Authorization": f"Bearer {evil_key}"},
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "blob_forbidden"


async def test_existing_examples_with_no_attachments_still_work() -> None:
    """Backward compat — events without `attachments` keep working and the
    field defaults to an empty list on the wire."""
    async with await _client() as client:
        alice_key, _ = await _register(client, "alice")
        bob_key, bob_id = await _register(client, "bob")

        task_id = await _create_task(client, alice_key)
        await _append_question(
            client, alice_key, task_id, to=[bob_id], text="no attachments here",
        )

        get = await client.get(
            f"/tasks/{task_id}",
            headers={"Authorization": f"Bearer {bob_key}"},
        )
        events = get.json()["events"]
        question = next(e for e in events if e["event_type"] == "question")
        assert question["content"]["attachments"] == []


async def test_admin_can_download_any_blob() -> None:
    """The admin token bypasses participant-scoped authz so the dashboard
    can render attachments without an agent key."""
    settings = Settings.for_tests(admin_token="adm-tok", blob_max_bytes=4 * 1024 * 1024)
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=10.0
    ) as client:
        alice_key, _ = await _register(client, "alice")
        task_id = await _create_task(client, alice_key)
        upload = await _upload_blob(
            client, alice_key, task_id,
            filename="a.txt", body=b"hello", mime_type="text/plain",
        )
        blob_id = upload.json()["blob_id"]

        resp = await client.get(
            f"/admin/blobs/{blob_id}",
            headers={"Authorization": "Bearer adm-tok"},
        )
        assert resp.status_code == 200
        assert resp.content == b"hello"

        meta = await client.get(
            f"/admin/blobs/{blob_id}/meta",
            headers={"Authorization": "Bearer adm-tok"},
        )
        assert meta.status_code == 200
        assert meta.json()["filename"] == "a.txt"
