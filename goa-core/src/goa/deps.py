from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from fastapi import Header, Request

from goa.auth import _parse_bearer, hash_api_key
from goa.config import Settings
from goa.domain.models import Participant
from goa.errors import Unauthorized
from goa.repos.persistence import Persistence
from goa.repos.protocols import BlobStore, ParticipantStore, TaskLog
from goa.services.tasks import TaskService
from goa.stream.hub import InMemoryStreamHub, StreamHub


@dataclass
class AppContext:
    settings: Settings
    # Underscored: the bundle is held only so the FastAPI lifespan can
    # enter/exit it. Handlers consume the individual stores by their
    # typed slots below, not the bundle.
    _persistence: Persistence
    participant_store: ParticipantStore
    task_log: TaskLog
    blob_store: BlobStore
    hub: StreamHub
    service: TaskService


def build_context(
    settings: Settings,
    *,
    persistence: Persistence | None = None,
    hub: StreamHub | None = None,
) -> AppContext:
    """Compose an `AppContext` from a caller-supplied `Persistence` bundle.
    `None` falls back to `Persistence.in_memory()`. The hub is swappable
    independently (Redis/NATS adapters land post-MVP; today's only impl
    is `InMemoryStreamHub`).

    The resolved `Persistence` bundle is stored on the context so the
    FastAPI lifespan (in `main.create_app`) can enter/exit it during
    app startup/shutdown.
    """
    p = persistence if persistence is not None else Persistence.from_settings(settings)
    h = hub if hub is not None else InMemoryStreamHub(
        replay_buffer_size=settings.replay_buffer_size,
        queue_size=settings.subscriber_queue_size,
    )
    service = TaskService(p.participant_store, p.task_log, p.blob_store, h)
    return AppContext(
        settings=settings,
        _persistence=p,
        participant_store=p.participant_store,
        task_log=p.task_log,
        blob_store=p.blob_store,
        hub=h,
        service=service,
    )


def build_default_context(settings: Settings) -> AppContext:
    """Zero-config default — every store is in-memory."""
    return build_context(settings)


def get_ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


def make_bearer_dependency() -> Callable[..., Awaitable[Participant]]:
    async def _dep(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> Participant:
        token = _parse_bearer(authorization)
        ctx = get_ctx(request)
        digest = hash_api_key(ctx.settings.server_pepper, token)
        participant = await ctx.participant_store.get_by_api_key_hash(digest)
        if participant is None:
            raise Unauthorized()
        return participant

    return _dep
