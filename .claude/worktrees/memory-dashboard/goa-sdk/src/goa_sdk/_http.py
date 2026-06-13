from __future__ import annotations

from typing import Any

import httpx


def auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def make_client(
    base_url: str,
    *,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float | None = 30.0,
) -> httpx.AsyncClient:
    headers: dict[str, str] = {}
    if api_key is not None:
        headers.update(auth_headers(api_key))
    return httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        transport=transport,
        timeout=timeout,
    )
