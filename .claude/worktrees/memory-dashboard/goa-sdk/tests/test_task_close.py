"""End-to-end SDK test for `Goa.close_task` (§8).

Pairs with `goa-core/tests/integration/test_task_close_e2e.py` (HTTP)
and `goa-core/tests/unit/test_task_close.py` (service). This file
proves the SDK wrapper: serialization of the response into the SDK
`Task` model with the new `status` field, and that the SDK surfaces
the 409 as a typed error on post-close append.
"""

from __future__ import annotations

import pytest

from goa.config import Settings
from goa.main import create_app

from goa_sdk import Goa, OutboundQuestion
from goa_sdk.errors import GoaSdkError
from goa_sdk.events import Content, QuestionPayload

from tests._live_server import live_server


pytestmark = pytest.mark.asyncio


async def test_sdk_close_task_returns_closed_task() -> None:
    app = create_app(Settings.for_tests())

    async with live_server(app) as base_url:
        alice_client, _, _ = await Goa.register_participant(
            base_url, type="agent", name="alice",
        )

        task = await alice_client.create_task()
        assert task.status == "open"

        closed = await alice_client.close_task(task.id)
        assert closed.id == task.id
        assert closed.status == "closed"


async def test_sdk_append_after_close_raises_invalid_state() -> None:
    """The SDK turns the server's 409 `invalid_state` into a typed
    `GoaSdkError` with the matching code, so callers can branch on it."""
    app = create_app(Settings.for_tests())

    async with live_server(app) as base_url:
        alice_client, _, _ = await Goa.register_participant(
            base_url, type="agent", name="alice",
        )
        _, _, bob = await Goa.register_participant(
            base_url, type="agent", name="bob",
        )

        task = await alice_client.create_task()
        await alice_client.close_task(task.id)

        with pytest.raises(GoaSdkError) as excinfo:
            await alice_client.append_event(
                task.id,
                OutboundQuestion(
                    payload=QuestionPayload(to=[bob.id]),
                    content=Content(text="ping?"),
                ),
            )
        assert excinfo.value.code == "invalid_state"
        assert excinfo.value.http_status == 409


async def test_sdk_close_is_idempotent() -> None:
    app = create_app(Settings.for_tests())

    async with live_server(app) as base_url:
        alice_client, _, _ = await Goa.register_participant(
            base_url, type="agent", name="alice",
        )

        task = await alice_client.create_task()
        first = await alice_client.close_task(task.id)
        second = await alice_client.close_task(task.id)
        assert first.id == second.id
        assert second.status == "closed"
        assert first.updated_at == second.updated_at
