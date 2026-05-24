"""Shared helpers for Goa v2 examples.

Kept at module scope (not packaged) so each example script can run as
`python examples/foo/main.py` against a live Goa hub. Intentionally tiny —
the full SDK lives in `goa_sdk`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from uuid import UUID


def base_url_arg(parser: argparse.ArgumentParser) -> None:
    """Add `--base-url` to a script's parser. Default is `127.0.0.1:8000`,
    overridable via `GOA_BASE_URL`. The env var defaulting matches the
    Makefile so `make goa` + `make example-*` line up out of the box."""
    parser.add_argument(
        "--base-url",
        default=os.environ.get("GOA_BASE_URL", "http://127.0.0.1:8000"),
        help="Goa hub base URL (default: %(default)s)",
    )


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def load_example_env(example_dir: Path) -> tuple[str, UUID]:
    """Read GOA_API_KEY and GOA_PARTICIPANT_ID from `<example_dir>/.env`.

    Exits with code 2 and a friendly hint if the file is missing or
    incomplete — typically the user hasn't run `make setup` yet.
    """
    env_path = example_dir / ".env"
    if not env_path.exists():
        sys.stderr.write(
            f"[{example_dir.name}] missing {env_path} — run `make setup` "
            "to register this agent (requires `make goa` running).\n"
        )
        sys.exit(2)
    values = _parse_env_file(env_path)
    api_key = values.get("GOA_API_KEY")
    participant_id = values.get("GOA_PARTICIPANT_ID")
    if not api_key or not participant_id:
        sys.stderr.write(
            f"[{example_dir.name}] {env_path} is missing GOA_API_KEY or "
            "GOA_PARTICIPANT_ID — delete it and run `make setup` again.\n"
        )
        sys.exit(2)
    return api_key, UUID(participant_id)
