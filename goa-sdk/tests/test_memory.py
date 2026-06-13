"""SDK tests for agent-private memory: `remember` / `recall` / `recall_all`
/ `forget` against a live hub."""

from __future__ import annotations

import pytest

from goa.config import Settings
from goa.main import create_app

from goa_sdk import Goa

from tests._live_server import live_server


pytestmark = pytest.mark.asyncio


async def test_remember_recall_roundtrip() -> None:
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        client, _, _ = await Goa.register_participant(base_url, type="agent", name="mem")
        try:
            entry = await client.remember(
                "user:U1:tone", {"prefers": "email"}, tags=["user"]
            )
            assert entry.key == "user:U1:tone"
            assert entry.value == {"prefers": "email"}
            assert entry.tags == ["user"]

            got = await client.recall("user:U1:tone")
            assert got is not None and got.value == {"prefers": "email"}

            # miss returns None, not an error
            assert await client.recall("missing") is None

            # overwrite preserves created_at; it's a full replace, so we
            # re-send tags to keep them (omitting tags would reset to []).
            again = await client.remember(
                "user:U1:tone", {"prefers": "sms"}, tags=["user"]
            )
            assert again.created_at == entry.created_at

            await client.remember("user:U1:lang", "en", tags=["user"])
            all_u1 = await client.recall_all(prefix="user:U1:")
            assert sorted(e.key for e in all_u1) == ["user:U1:lang", "user:U1:tone"]

            tagged = await client.recall_all(tags=["user"])
            assert len(tagged) == 2

            assert await client.forget(key="user:U1:tone") == 1
            assert await client.recall("user:U1:tone") is None

            assert await client.forget(prefix="user:U1:") == 1
            assert await client.recall_all(prefix="user:U1:") == []
        finally:
            await client.aclose()


async def test_memory_is_private_per_participant() -> None:
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        a_client, _, _ = await Goa.register_participant(base_url, type="agent", name="a")
        b_client, _, _ = await Goa.register_participant(base_url, type="agent", name="b")
        try:
            await a_client.remember("secret", "a-only")
            assert await b_client.recall("secret") is None
            assert await b_client.recall_all() == []
            got = await a_client.recall("secret")
            assert got is not None and got.value == "a-only"
        finally:
            await a_client.aclose()
            await b_client.aclose()
