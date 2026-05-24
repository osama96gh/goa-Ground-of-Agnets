from __future__ import annotations

import hmac
import secrets
from hashlib import sha256

from fastapi import Header

from goa.errors import Unauthorized


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def hash_api_key(pepper: str, api_key: str) -> str:
    return hmac.new(pepper.encode("utf-8"), api_key.encode("utf-8"), sha256).hexdigest()


def _parse_bearer(authorization: str | None) -> str:
    if not authorization:
        raise Unauthorized()
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise Unauthorized()
    return parts[1].strip()


def bearer_dependency():
    """FastAPI dep factory bound at app construction in deps.py."""

    async def _dep(
        authorization: str | None = Header(default=None),
    ) -> str | None:
        return authorization

    return _dep
