"""HTTP-level tests for the `/memory` endpoints against a live hub."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from goa.config import Settings
from goa.main import create_app

from tests.integration._live_server import live_server


pytestmark = pytest.mark.asyncio


async def _register(http: httpx.AsyncClient, name: str = "mem-agent") -> str:
    resp = await http.post("/participants", json={"type": "agent", "name": name})
    resp.raise_for_status()
    return resp.json()["api_key"]


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


async def test_memory_requires_auth() -> None:
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            resp = await http.post("/memory", json={"key": "k", "value": 1})
            assert resp.status_code == 401
            assert resp.json()["error"]["code"] == "unauthorized"


async def test_upsert_fetch_list_delete_flow() -> None:
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            h = _auth(await _register(http))

            # create -> 201
            r = await http.post(
                "/memory",
                headers=h,
                json={"key": "user:U1:tone", "value": {"prefers": "email"}, "tags": ["user"]},
            )
            assert r.status_code == 201
            assert r.json()["key"] == "user:U1:tone"
            assert r.json()["value"] == {"prefers": "email"}

            # overwrite same key -> 200
            r = await http.post(
                "/memory",
                headers=h,
                json={"key": "user:U1:tone", "value": {"prefers": "sms"}, "tags": ["user"]},
            )
            assert r.status_code == 200

            # exact fetch
            r = await http.get("/memory", headers=h, params={"key": "user:U1:tone"})
            assert r.status_code == 200
            entries = r.json()["entries"]
            assert len(entries) == 1 and entries[0]["value"] == {"prefers": "sms"}

            # missing key -> empty list, not 404
            r = await http.get("/memory", headers=h, params={"key": "nope"})
            assert r.status_code == 200 and r.json()["entries"] == []

            # second key, then list by prefix + AND-ed tag
            await http.post(
                "/memory",
                headers=h,
                json={"key": "user:U1:lang", "value": "en", "tags": ["user"]},
            )
            r = await http.get(
                "/memory", headers=h, params=[("prefix", "user:U1:"), ("tag", "user")]
            )
            keys = sorted(e["key"] for e in r.json()["entries"])
            assert keys == ["user:U1:lang", "user:U1:tone"]

            # a different namespace is untouched
            r = await http.get("/memory", headers=h, params={"prefix": "user:U2:"})
            assert r.json()["entries"] == []

            # delete one
            r = await http.request("DELETE", "/memory", headers=h, params={"key": "user:U1:tone"})
            assert r.status_code == 200 and r.json() == {"deleted": 1}

            # forget the rest by prefix
            r = await http.request("DELETE", "/memory", headers=h, params={"prefix": "user:U1:"})
            assert r.json() == {"deleted": 1}
            r = await http.get("/memory", headers=h, params={"prefix": "user:U1:"})
            assert r.json()["entries"] == []


async def test_delete_without_key_or_prefix_is_400() -> None:
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            h = _auth(await _register(http))
            r = await http.request("DELETE", "/memory", headers=h)
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "invalid_request"


async def test_memory_is_isolated_per_participant() -> None:
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            ha = _auth(await _register(http, "a"))
            hb = _auth(await _register(http, "b"))
            await http.post("/memory", headers=ha, json={"key": "secret", "value": "a-only"})
            # b sees nothing of a's
            assert (await http.get("/memory", headers=hb, params={"key": "secret"})).json()["entries"] == []
            assert (await http.get("/memory", headers=hb)).json()["entries"] == []
            # a still has it
            got = (await http.get("/memory", headers=ha, params={"key": "secret"})).json()["entries"]
            assert got[0]["value"] == "a-only"


async def test_entry_too_large_returns_413() -> None:
    app = create_app(replace(Settings.for_tests(), memory_max_entry_bytes=50))
    async with live_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url) as http:
            h = _auth(await _register(http))
            r = await http.post("/memory", headers=h, json={"key": "k", "value": "x" * 200})
            assert r.status_code == 413
            assert r.json()["error"]["code"] == "memory_entry_too_large"
