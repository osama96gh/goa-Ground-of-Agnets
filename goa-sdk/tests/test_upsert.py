"""SDK tests for `Goa.upsert_task` and `Goa.upsert_and_send` (§9.2)."""

from __future__ import annotations

import pytest

from goa.config import Settings
from goa.main import create_app

from goa_sdk import Goa, GoaSdkError, OutboundQuestion
from goa_sdk.events import Content, QuestionPayload

from tests._live_server import live_server


pytestmark = pytest.mark.asyncio


async def test_upsert_creates_then_finds_existing() -> None:
    """`upsert_task` returns `(task, created)` and never appends an event.
    Use `upsert_and_send` for the find-or-create-then-send flow."""
    app = create_app(Settings.for_tests())

    async with live_server(app) as base_url:
        s_client, _, _s = await Goa.register_participant(
            base_url, type="service", name="chat"
        )
        _c_client, _, c = await Goa.register_participant(
            base_url, type="agent", name="support"
        )

        try:
            first_task, first_created, _first_event = await s_client.upsert_and_send(
                external_ref="slack-thread-abc123",
                event=OutboundQuestion(
                    payload=QuestionPayload(to=[c.id]),
                    content=Content(text="where's my refund?"),
                ),
                subject="refund",
            )
            assert first_created is True

            second_task, second_created = await s_client.upsert_task(
                external_ref="slack-thread-abc123",
            )
            assert second_created is False
            assert second_task.id == first_task.id

            # The SDK exposes `external_ref` on the read-side `Task` model.
            fetched = await s_client.get_task(first_task.id)
            assert fetched.task.external_ref == "slack-thread-abc123"
        finally:
            await s_client.aclose()


async def test_create_task_with_external_ref_collision_raises_sdk_error() -> None:
    """Direct `create_task` after an `upsert_task` with the same `external_ref`
    should surface as `GoaSdkError(code=external_ref_in_use, http_status=409)`."""
    app = create_app(Settings.for_tests())

    async with live_server(app) as base_url:
        s_client, _, _ = await Goa.register_participant(
            base_url, type="service", name="chat"
        )
        _c_client, _, c = await Goa.register_participant(
            base_url, type="agent", name="support"
        )

        try:
            await s_client.upsert_task(external_ref="slack-thread-xyz")

            with pytest.raises(GoaSdkError) as excinfo:
                await s_client.create_task(external_ref="slack-thread-xyz")
            assert excinfo.value.code == "external_ref_in_use"
            assert excinfo.value.http_status == 409
        finally:
            await s_client.aclose()
