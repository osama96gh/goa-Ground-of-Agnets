from __future__ import annotations

import pytest

from goa.auth import _parse_bearer, generate_api_key, hash_api_key
from goa.errors import Unauthorized


def test_hash_api_key_is_deterministic() -> None:
    a = hash_api_key("pep", "abc")
    b = hash_api_key("pep", "abc")
    assert a == b
    assert a != hash_api_key("other-pep", "abc")
    assert a != hash_api_key("pep", "def")


def test_generate_api_key_is_unique() -> None:
    keys = {generate_api_key() for _ in range(100)}
    assert len(keys) == 100


def test_parse_bearer_valid() -> None:
    assert _parse_bearer("Bearer abc.def") == "abc.def"
    assert _parse_bearer("bearer xyz") == "xyz"


@pytest.mark.parametrize(
    "header",
    [None, "", "Bearer", "Bearer ", "Basic abc", "abc"],
)
def test_parse_bearer_invalid(header: str | None) -> None:
    with pytest.raises(Unauthorized):
        _parse_bearer(header)
