"""Export the FastAPI hub's OpenAPI spec to a committed artifact.

The HTTP API is Goa's primary contract — the Python SDK is one consumer,
the dashboard is another, and any other language is welcome. To make that
real, `openapi.json` at the repo root is the source-of-truth schema:
machine-readable, versioned in git, and the basis for codegen / drift
detection.

Two modes:

  python scripts/export_openapi.py           # regenerate openapi.json
  python scripts/export_openapi.py --check   # exit 1 if drifted (CI / tests)

Usage from the workspace: prefer `make openapi-export` / `make openapi-check`.

# Why the spec is the public-only contract

The committed `openapi.json` is what external integrators (codegen
clients, non-Python agents, documentation tools) consume. It deliberately
excludes:

  * `/admin/*` — operator-only observability, gated on `GOA_ADMIN_TOKEN`.
    Different audience, different auth model, different stability promise.
    Should not surface in codegen clients aimed at participants.
  * `/health` — a liveness probe, not part of the API contract. Marked
    `include_in_schema=False` at the route.

To make the export deterministic regardless of the developer's local
`.env.local` (which DOES set `GOA_ADMIN_TOKEN` for dev convenience), we
construct an explicit `Settings` with no admin token via
`Settings.for_tests()` and hand it to `create_app(settings=...)`. This
sidesteps `Settings.from_env()` entirely — the script's output never
depends on what env vars happen to be set when you run it.

Serialization uses `sort_keys=True, indent=2` so the file is stable
across FastAPI / Pydantic dict-ordering changes; PR diffs reflect real
API changes, not noise.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "openapi.json"


def _serialize_spec() -> str:
    """Generate the OpenAPI spec from a public-contract FastAPI instance
    (no admin token, no env dependency) and serialize deterministically.
    Returns the file content as a string."""
    # Imported lazily so `--help` works without the goa-core deps installed.
    from goa.config import Settings
    from goa.main import create_app

    settings = Settings.for_tests(admin_token=None)
    app = create_app(settings=settings)
    spec = app.openapi()
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def _write(content: str, path: Path) -> None:
    path.write_text(content)


def _check(content: str, path: Path) -> int:
    if not path.exists():
        sys.stderr.write(
            f"error: {path.relative_to(REPO_ROOT)} does not exist. "
            "Run `make openapi-export` to create it.\n"
        )
        return 1
    on_disk = path.read_text()
    if on_disk == content:
        return 0
    diff = difflib.unified_diff(
        on_disk.splitlines(keepends=True),
        content.splitlines(keepends=True),
        fromfile=f"{path.name} (committed)",
        tofile=f"{path.name} (regenerated)",
        n=3,
    )
    sys.stderr.write("".join(diff))
    sys.stderr.write(
        "\nopenapi.json is out of date — run `make openapi-export` to regenerate.\n"
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 with a diff if openapi.json on disk does not match "
        "the regenerated spec. Used by `make openapi-check` and the unit "
        "snapshot test.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=SPEC_PATH,
        help=f"Output path (default: {SPEC_PATH.relative_to(REPO_ROOT)}).",
    )
    args = parser.parse_args()

    content = _serialize_spec()

    if args.check:
        return _check(content, args.output)
    _write(content, args.output)
    print(f"wrote {args.output.relative_to(REPO_ROOT)} ({len(content)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
