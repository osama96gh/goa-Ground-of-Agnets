"""End-to-end SDK test for idempotent event append.

Pairs with `goa-core/tests/unit/test_idempotency.py` (service-level)
and the parametrized contract tests in `test_task_log_contract.py`
(both in-memory and SQLite). This file proves the wire round-trip:
the SDK forwards `client_event_id`, the hub honors it, and the
returned `Event` echoes the key back.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from goa.config import Settings
from goa.main import create_app

from goa_sdk import Goa, OutboundQuestion
from goa_sdk.events import Content, QuestionPayload

from tests._live_server import live_server


pytestmark = pytest.mark.asyncio


async def test_sdk_repeat_question_returns_same_event() -> None:
    """Sending the same `client_event_id` twice through the SDK against a
    live server resolves to one persisted event. The second response
    body is identical to the first."""
    app = create_app(Settings.for_tests())

    async with live_server(app) as base_url:
        alice_client, _, _ = await Goa.register_participant(
            base_url, type="agent", name="alice",
        )
        _, _, bob = await Goa.register_participant(
            base_url, type="agent", name="bob",
        )

        task = await alice_client.create_task()
        key = uuid4()
        body = OutboundQuestion(
            payload=QuestionPayload(to=[bob.id]),
            content=Content(text="ping?"),
            client_event_id=key,
        )

        first = await alice_client.append_event(task.id, body)
        second = await alice_client.append_event(task.id, body)

        # Server-set fields are identical — same row served twice.
        assert first.id == second.id
        assert first.created_at == second.created_at
        # And the key round-tripped onto the persisted event.
        assert first.client_event_id == key
        assert second.client_event_id == key


async def test_sdk_no_key_means_distinct_events() -> None:
    """Without `client_event_id` on the outbound event, the SDK opts out
    of dedup and the server creates two distinct events on retry."""
    app = create_app(Settings.for_tests())

    async with live_server(app) as base_url:
        alice_client, _, _ = await Goa.register_participant(
            base_url, type="agent", name="alice",
        )
        _, _, bob = await Goa.register_participant(
            base_url, type="agent", name="bob",
        )

        task = await alice_client.create_task()
        body = OutboundQuestion(
            payload=QuestionPayload(to=[bob.id]),
            content=Content(text="ping?"),
        )

        first = await alice_client.append_event(task.id, body)
        second = await alice_client.append_event(task.id, body)

        assert first.id != second.id
        assert first.client_event_id is None
        assert second.client_event_id is None
