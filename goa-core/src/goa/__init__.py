"""Goa — collaborative task orchestration for multi-agent systems.

Public entry points:

- `create_app(...)` — construct the FastAPI hub. Pass a `Persistence`
  bundle to wire custom backends; omit it for the in-memory defaults.
- `Persistence` — bundle of the three persistence Protocols. Single
  injection point so a deployment can't be half-wired by accident.
- `Settings` — runtime configuration (env-driven via `Settings.from_env()`
  or constructed directly for tests via `Settings.for_tests()`).
- `TaskLog`, `ParticipantStore`, `BlobStore` — the three persistence
  Protocols a consumer implements to plug in a custom backend.
- `InMemoryTaskLog`, `InMemoryParticipantStore`, `InMemoryBlobStore` —
  the zero-config defaults; also useful as fakes in tests.
"""

from goa.config import Settings
from goa.main import create_app
from goa.repos.memory import (
    InMemoryBlobStore,
    InMemoryParticipantStore,
    InMemoryTaskLog,
)
from goa.repos.persistence import Persistence
from goa.repos.protocols import BlobStore, ParticipantStore, TaskLog

__all__ = [
    "BlobStore",
    "InMemoryBlobStore",
    "InMemoryParticipantStore",
    "InMemoryTaskLog",
    "ParticipantStore",
    "Persistence",
    "Settings",
    "TaskLog",
    "create_app",
]
