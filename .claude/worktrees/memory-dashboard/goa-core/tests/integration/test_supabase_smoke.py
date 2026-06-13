"""Opt-in smoke test against a real Supabase project.

Skipped unless the full set of env vars below are set, so this never
runs in normal CI. To exercise it locally against a throwaway Supabase
project:

    export GOA_SUPABASE_TEST_DATABASE_URL='postgresql://postgres:[pw]@db.[ref].supabase.co:5432/postgres'
    export GOA_SUPABASE_TEST_ENDPOINT='https://[ref].storage.supabase.co/storage/v1/s3'
    export GOA_SUPABASE_TEST_BUCKET='goa-smoke'                # create this bucket in the dashboard first
    export GOA_SUPABASE_TEST_REGION='us-east-1'                # whatever your project's region is
    export GOA_SUPABASE_TEST_ACCESS_KEY='...'                  # from dashboard → Storage → Settings → S3 access keys
    export GOA_SUPABASE_TEST_SECRET_KEY='...'
    uv run pytest goa-core/tests/integration/test_supabase_smoke.py -v

What the test proves: the `Persistence.supabase(...)` factory wires
Postgres + Storage end-to-end against the real services — registration,
task creation, an event append, and a blob round-trip (put → open) all
work. Teardown drops the participants/tasks/events/blobs rows it
created and deletes the uploaded object so re-runs stay clean. The
bucket itself is **not** deleted.
"""

from __future__ import annotations

import os
import uuid
from uuid import UUID

import httpx
import pytest

from goa.config import Settings
from goa.main import create_app
from goa.repos.persistence import Persistence

from tests.integration._live_server import live_server


pytestmark = pytest.mark.asyncio


_REQUIRED_ENV = (
    "GOA_SUPABASE_TEST_DATABASE_URL",
    "GOA_SUPABASE_TEST_ENDPOINT",
    "GOA_SUPABASE_TEST_BUCKET",
    "GOA_SUPABASE_TEST_REGION",
    "GOA_SUPABASE_TEST_ACCESS_KEY",
    "GOA_SUPABASE_TEST_SECRET_KEY",
)


def _supabase_env_or_skip() -> dict[str, str]:
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.skip(
            "Supabase smoke test requires env vars: " + ", ".join(missing)
        )
    return {name: os.environ[name] for name in _REQUIRED_ENV}


async def test_supabase_smoke_end_to_end() -> None:
    env = _supabase_env_or_skip()
    persistence = Persistence.supabase(
        database_url=env["GOA_SUPABASE_TEST_DATABASE_URL"],
        storage_endpoint=env["GOA_SUPABASE_TEST_ENDPOINT"],
        storage_bucket=env["GOA_SUPABASE_TEST_BUCKET"],
        storage_region=env["GOA_SUPABASE_TEST_REGION"],
        storage_access_key=env["GOA_SUPABASE_TEST_ACCESS_KEY"],
        storage_secret_key=env["GOA_SUPABASE_TEST_SECRET_KEY"],
    )

    # Random suffix on names + external_ref so re-runs against the same
    # project don't collide on the (initiator, external_ref) slot.
    nonce = uuid.uuid4().hex[:8]

    settings = Settings.for_tests()
    app = create_app(settings, persistence=persistence)

    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
            alice = (await client.post(
                "/participants", json={"type": "agent", "name": f"smoke-alice-{nonce}"}
            )).json()
            bob = (await client.post(
                "/participants", json={"type": "agent", "name": f"smoke-bob-{nonce}"}
            )).json()
            alice_key = alice["api_key"]
            bob_id = bob["participant"]["id"]

            create_resp = await client.post(
                "/tasks",
                headers={"Authorization": f"Bearer {alice_key}"},
                json={
                    "subject": "smoke",
                    "external_ref": f"smoke-{nonce}",
                    "metadata": {},
                },
            )
            assert create_resp.status_code == 201, create_resp.text
            task_id = UUID(create_resp.json()["task"]["id"])

            q_resp = await client.post(
                f"/tasks/{task_id}/events",
                headers={"Authorization": f"Bearer {alice_key}"},
                json={
                    "event_type": "question",
                    "payload": {"to": [bob_id]},
                    "content": {"text": f"smoke probe {nonce}"},
                    "in_reply_to": None,
                    "metadata": {},
                },
            )
            assert q_resp.status_code == 201, q_resp.text

            # Blob round-trip: upload → meta → download.
            files = {
                "file": (
                    "smoke.txt",
                    f"hello from goa smoke {nonce}".encode(),
                    "text/plain",
                ),
            }
            upload = await client.post(
                "/blobs",
                headers={"Authorization": f"Bearer {alice_key}"},
                params={"task_id": str(task_id)},
                files=files,
            )
            assert upload.status_code == 201, upload.text
            blob_id = upload.json()["blob_id"]

            dl = await client.get(
                f"/blobs/{blob_id}",
                headers={"Authorization": f"Bearer {alice_key}"},
            )
            assert dl.status_code == 200
            assert dl.content == f"hello from goa smoke {nonce}".encode()
