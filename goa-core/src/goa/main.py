from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from goa.api.admin import router as admin_router
from goa.api.blobs import router as blobs_router
from goa.api.participants import router as participants_router
from goa.api.stream import router as stream_router
from goa.api.tasks import router as tasks_router
from goa.config import Settings
from goa.deps import build_context
from goa.errors import install_error_handlers
from goa.repos.persistence import Persistence
from goa.stream.hub import StreamHub

# Sentinel ID for the health-check probe: a random UUID that will never match
# a real participant, so `get(_HEALTH_PROBE_ID)` returns None on success and
# raises if the backing store is unreachable. Cheap (one indexed lookup) and
# touches the actual DB connection — not just the process.
_HEALTH_PROBE_ID = UUID("00000000-0000-0000-0000-000000000000")


def create_app(
    settings: Settings | None = None,
    *,
    persistence: Persistence | None = None,
    hub: StreamHub | None = None,
) -> FastAPI:
    """Construct the Goa hub. Pass a `Persistence` bundle to wire custom
    backends; omit it to use the in-memory defaults.

    ADK-style entry point — consumers ship a `Persistence` whose three
    fields satisfy the `TaskLog`, `ParticipantStore`, and `BlobStore`
    Protocols. The bundle is the single injection point: callers must
    provide all three backends together, so a half-wired deployment
    (one Protocol persistent, the others silently in-memory) is
    structurally impossible. See `goa.repos.protocols` for the contracts
    and `goa.repos.memory` for reference impls.

    >>> from goa import create_app, Persistence
    >>> app = create_app()  # zero-config in-memory
    >>>
    >>> # Or with custom backends:
    >>> from my_pg import PostgresTaskLog, PostgresParticipantStore
    >>> from my_s3 import S3BlobStore
    >>> app = create_app(persistence=Persistence(
    ...     task_log=PostgresTaskLog(dsn=...),
    ...     participant_store=PostgresParticipantStore(dsn=...),
    ...     blob_store=S3BlobStore(bucket=...),
    ... ))
    """
    settings = settings or Settings.from_env()
    ctx = build_context(settings, persistence=persistence, hub=hub)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Enter every persistence-backed store that owns a connection/pool
        # (SQLite opens its connection here, runs CREATE TABLE IF NOT EXISTS).
        # In-memory stores are skipped silently in the fan-out.
        async with ctx._persistence:
            yield

    app = FastAPI(title="Goa", version="0.5.0", lifespan=lifespan)
    app.state.ctx = ctx

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    install_error_handlers(app)

    @app.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        """Liveness + readiness probe. No auth — load balancers and Compose
        healthchecks can't carry the admin token. Returns 200 when the
        persistence layer answers, 503 otherwise. The probe is a single
        indexed lookup on a sentinel UUID that will never match a real row.
        """
        try:
            await ctx.participant_store.get(_HEALTH_PROBE_ID)
        except Exception as exc:  # noqa: BLE001 — any failure means degraded
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "detail": str(exc)[:200]},
            )
        return JSONResponse(status_code=200, content={"status": "ok"})

    app.include_router(participants_router)
    app.include_router(tasks_router)
    app.include_router(blobs_router)
    app.include_router(stream_router)
    if settings.admin_token:
        # Admin routes are gated at module-load time — when no token is set,
        # the routes simply do not exist and 404. This keeps stock dev
        # deployments from accidentally exposing the firehose.
        app.include_router(admin_router)

    return app


def __getattr__(name: str) -> FastAPI:
    """Lazy `app` for `uvicorn goa.main:app` so `import goa.main` does not
    require `GOA_SERVER_PEPPER` to be set (the smoke test imports the module
    without env)."""
    if name == "app":
        instance = create_app()
        globals()["app"] = instance
        return instance
    raise AttributeError(f"module 'goa.main' has no attribute {name!r}")
