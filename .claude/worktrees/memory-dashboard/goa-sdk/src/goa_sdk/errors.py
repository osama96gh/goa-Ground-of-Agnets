from __future__ import annotations

from typing import Any

import httpx


class GoaSdkError(Exception):
    """Server-returned error decoded from the `{error: {code, message}}`
    envelope (spec §12). `http_status` carries the HTTP code for callers
    that prefer status-based branching."""

    def __init__(self, *, code: str, message: str, http_status: int) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.http_status = http_status


def raise_for_response(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        body = response.json()
    except ValueError:
        body = {}
    err = body.get("error", {}) if isinstance(body, dict) else {}
    code = err.get("code") or "error"
    message = err.get("message") or response.text or "request failed"
    raise GoaSdkError(code=code, message=message, http_status=response.status_code)
