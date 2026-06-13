"""Drift test: the committed `openapi.json` must match what `create_app()`
generates from the current code. If you change a route, payload model, or
error envelope, this test fails — `make openapi-export` is the fix.

Why this test exists: the HTTP API is Goa's primary contract. The
committed `openapi.json` is consumed by codegen clients and quoted in
docs. Letting it drift silently from the code would mean the docs lie
and the SDK / spec disagree. This test guarantees they're in lockstep.

The test imports the same `_serialize_spec()` the export script uses, so
there's only one place where "what should be in openapi.json" is defined.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC_PATH = REPO_ROOT / "openapi.json"
EXPORT_SCRIPT = REPO_ROOT / "scripts" / "export_openapi.py"


def _load_export_module():
    """Load `scripts/export_openapi.py` as a module without polluting
    sys.path. Returns the imported module."""
    spec = importlib.util.spec_from_file_location("export_openapi", EXPORT_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openapi_snapshot_matches_committed_spec() -> None:
    assert SPEC_PATH.exists(), (
        f"{SPEC_PATH.relative_to(REPO_ROOT)} is missing. "
        "Run `make openapi-export` to create it."
    )

    export = _load_export_module()
    regenerated = export._serialize_spec()
    committed = SPEC_PATH.read_text()

    assert regenerated == committed, (
        "openapi.json is out of date — the public API has changed since "
        "the spec was last exported.\n\n"
        "Run `make openapi-export` to regenerate the file, then re-run "
        "the tests."
    )
