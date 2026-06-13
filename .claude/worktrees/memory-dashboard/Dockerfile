# syntax=docker/dockerfile:1.7
# Hub image — multi-stage build of the `goa` package from goa-core/.
#
# Stage 1 (builder): installs uv, syncs the workspace into a venv with the
# production-relevant optional deps (postgres + s3). All build tooling
# stays in this stage.
#
# Stage 2 (runtime): copies only the venv and the package source onto a
# slim Python image, runs as a non-root user, executes uvicorn directly.
# No build tools, no uv, no .git.

# ─── Stage 1: build ──────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# Install uv. Pinned to a known-good release for reproducibility — bump
# manually when upgrading.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

WORKDIR /app

# Copy workspace manifests first so the dep-resolve layer caches across
# source-only edits.
COPY pyproject.toml uv.lock ./
COPY goa-core/pyproject.toml goa-core/
COPY goa-sdk/pyproject.toml  goa-sdk/

# Resolve + install the hub's production dependency tree, no dev deps.
# `--extra postgres --extra s3` pulls asyncpg and aioboto3 so the runtime
# can serve whichever GOA_DATABASE_URL / GOA_BLOB_BACKEND the operator
# picks without rebuilding the image.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync \
        --package goa \
        --no-dev \
        --extra postgres \
        --extra s3 \
        --frozen \
        --no-install-project

# Now copy the package source and install the package itself.
COPY goa-core/ goa-core/
COPY goa-sdk/  goa-sdk/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync \
        --package goa \
        --no-dev \
        --extra postgres \
        --extra s3 \
        --frozen


# ─── Stage 2: runtime ────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# wget is used by the HEALTHCHECK below. ca-certificates lets the hub
# talk to managed Postgres providers (Supabase / Neon / RDS) over TLS.
RUN apt-get update \
    && apt-get install --no-install-recommends -y wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. uid 10001 is a common convention well outside the
# host's reserved range, avoiding collisions with mounted volumes.
RUN groupadd --system --gid 10001 goa \
    && useradd  --system --uid 10001 --gid goa --home-dir /app --shell /sbin/nologin goa

WORKDIR /app

# Copy the resolved venv and source from the builder. Ownership flips to
# the non-root user in the same layer so we don't ship root-owned files.
COPY --from=builder --chown=goa:goa /app/.venv     /app/.venv
COPY --from=builder --chown=goa:goa /app/goa-core  /app/goa-core
COPY --from=builder --chown=goa:goa /app/goa-sdk   /app/goa-sdk

USER goa

EXPOSE 8000

# Container-level healthcheck. Mirrors the Compose-level check (Compose
# wins when both are present) — kept here so `docker run` users get it
# without configuring Compose.
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=5 \
    CMD wget -q -O- http://127.0.0.1:8000/health || exit 1

# Exec form so signals (SIGTERM from `docker stop`) reach uvicorn directly,
# letting it close SSE connections cleanly before the container exits.
CMD ["uvicorn", "goa.main:app", "--host", "0.0.0.0", "--port", "8000"]
