#!/usr/bin/env bash
# Manages bundled-service containers for `make demo` (Postgres + MinIO).
#
# Despite the name, this script manages all the local "demo dependencies"
# the hub talks to natively — currently Postgres (state) and MinIO (S3-
# compatible blob bytes). Kept under one script so `make demo-db-up`
# starts everything the hub needs and `make demo-db-down` stops it.
#
# Behavior:
#   up    — for each service whose configured endpoint points at a local
#           host, start a container if not already running. No-op for
#           sqlite://, empty (in-memory), or endpoints targeting any
#           non-local host (external service).
#   down  — stop + remove the containers. Volumes preserved.
#   wipe  — remove containers AND their data volumes.
#
# Why this exists: the deploy path (docker-compose.yml) bundles Postgres
# + MinIO so `docker compose up` works offline. Mirroring that for
# `make demo` (native hub) catches schema/SDK bugs locally instead of in
# production. Container reuse + named volumes mean repeated `make demo`
# runs are fast — no schema or bucket re-init on every start.

set -euo pipefail

cd "$(dirname "$0")/.."

PG_CONTAINER=goa-demo-postgres
PG_VOLUME=goa-demo-pgdata
PG_IMAGE=postgres:16-alpine

MINIO_CONTAINER=goa-demo-minio
MINIO_VOLUME=goa-demo-miniodata
MINIO_IMAGE=minio/minio:latest
MC_IMAGE=minio/mc:latest

# Read config from .env.local if not already in the environment. The
# Makefile exports these via `include .env.local`, but this script may
# also be invoked standalone.
env_get() {
  local key="$1"
  if [ -f .env.local ]; then
    awk -F= -v k="$key" '
      /^[[:space:]]*#/ { next }
      $0 ~ "^[[:space:]]*"k"[[:space:]]*=" {
        sub(/^[[:space:]]*[^=]+=[[:space:]]*/, "", $0)
        gsub(/^["'"'"']|["'"'"']$/, "", $0)
        print
        exit
      }
    ' .env.local
  fi
}

if [ -z "${GOA_DATABASE_URL+x}" ]; then
  GOA_DATABASE_URL="$(env_get GOA_DATABASE_URL)"
fi
GOA_DATABASE_URL="${GOA_DATABASE_URL:-}"

GOA_BLOB_BACKEND="${GOA_BLOB_BACKEND:-$(env_get GOA_BLOB_BACKEND)}"
GOA_BLOB_BACKEND="${GOA_BLOB_BACKEND:-db}"
GOA_BLOB_ENDPOINT="${GOA_BLOB_ENDPOINT:-$(env_get GOA_BLOB_ENDPOINT)}"
GOA_BLOB_BUCKET="${GOA_BLOB_BUCKET:-$(env_get GOA_BLOB_BUCKET)}"
GOA_BLOB_BUCKET="${GOA_BLOB_BUCKET:-goa-blobs}"
GOA_BLOB_ACCESS_KEY="${GOA_BLOB_ACCESS_KEY:-$(env_get GOA_BLOB_ACCESS_KEY)}"
GOA_BLOB_SECRET_KEY="${GOA_BLOB_SECRET_KEY:-$(env_get GOA_BLOB_SECRET_KEY)}"

# ─── Predicates ─────────────────────────────────────────────

is_local_postgres() {
  case "$GOA_DATABASE_URL" in
    postgresql://*@localhost:*|postgresql://*@127.0.0.1:*|postgres://*@localhost:*|postgres://*@127.0.0.1:*)
      return 0 ;;
    *)
      return 1 ;;
  esac
}

is_local_minio() {
  # MinIO is only relevant when blob backend is s3 AND endpoint is local.
  if [ "$GOA_BLOB_BACKEND" != "s3" ]; then
    return 1
  fi
  case "$GOA_BLOB_ENDPOINT" in
    http://localhost:*|http://127.0.0.1:*|https://localhost:*|https://127.0.0.1:*)
      return 0 ;;
    *)
      return 1 ;;
  esac
}

# Extract host port from an endpoint URL. Defaults to 9000.
minio_port() {
  echo "${GOA_BLOB_ENDPOINT:-http://localhost:9000}" \
    | awk -F: '{print $NF}' \
    | awk -F/ '{print $1}'
}

# ─── Docker plumbing ────────────────────────────────────────

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    cat >&2 <<EOF
error: a bundled local service is configured but \`docker\` is not on PATH.

Options:
  1. Install Docker, then re-run \`make demo\`.
  2. Edit .env.local and swap to SQLite (single file, no Docker):
       GOA_DATABASE_URL=sqlite:./goa.db
       GOA_BLOB_BACKEND=db
  3. Point GOA_DATABASE_URL / GOA_BLOB_ENDPOINT at external services.
EOF
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "error: docker is installed but the daemon is not reachable. Start Docker Desktop / dockerd and retry." >&2
    exit 1
  fi
}

container_state() {
  # Prints: running | exited | missing
  local name="$1"
  local status
  status="$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || true)"
  if [ -z "$status" ]; then
    echo missing
  else
    echo "$status"
  fi
}

# ─── Postgres ───────────────────────────────────────────────

postgres_up() {
  if ! is_local_postgres; then
    if [ -z "$GOA_DATABASE_URL" ]; then
      echo "demo-db: GOA_DATABASE_URL is empty (in-memory) — skipping Postgres."
    elif [[ "$GOA_DATABASE_URL" == sqlite:* ]]; then
      echo "demo-db: GOA_DATABASE_URL is sqlite — skipping Postgres."
    else
      echo "demo-db: GOA_DATABASE_URL points at a non-local host — skipping Postgres."
    fi
    return 0
  fi

  require_docker

  case "$(container_state "$PG_CONTAINER")" in
    running)
      echo "demo-db: $PG_CONTAINER already running."
      ;;
    exited)
      echo "demo-db: starting existing $PG_CONTAINER…"
      docker start "$PG_CONTAINER" >/dev/null
      ;;
    missing)
      echo "demo-db: creating $PG_CONTAINER on localhost:5432…"
      docker run -d --name "$PG_CONTAINER" \
        -e POSTGRES_USER=goa \
        -e POSTGRES_PASSWORD=goa \
        -e POSTGRES_DB=goa \
        -p 5432:5432 \
        -v "$PG_VOLUME":/var/lib/postgresql/data \
        "$PG_IMAGE" >/dev/null
      ;;
  esac

  echo -n "demo-db: waiting for Postgres to accept connections"
  for _ in $(seq 1 60); do
    if docker exec "$PG_CONTAINER" pg_isready -U goa -d goa >/dev/null 2>&1; then
      echo " ✓"
      return 0
    fi
    echo -n "."
    sleep 0.5
  done
  echo
  echo "error: Postgres did not become ready within 30s. Recent logs:" >&2
  docker logs --tail 30 "$PG_CONTAINER" >&2 || true
  exit 1
}

postgres_down() {
  if [ "$(container_state "$PG_CONTAINER")" != missing ]; then
    echo "demo-db: removing $PG_CONTAINER (volume $PG_VOLUME preserved)…"
    docker rm -f "$PG_CONTAINER" >/dev/null
  fi
}

postgres_wipe() {
  postgres_down
  if docker volume inspect "$PG_VOLUME" >/dev/null 2>&1; then
    echo "demo-db: removing data volume $PG_VOLUME…"
    docker volume rm "$PG_VOLUME" >/dev/null
  fi
}

# ─── MinIO ──────────────────────────────────────────────────

minio_up() {
  if ! is_local_minio; then
    if [ "$GOA_BLOB_BACKEND" != "s3" ]; then
      echo "demo-db: GOA_BLOB_BACKEND=$GOA_BLOB_BACKEND — skipping MinIO."
    else
      echo "demo-db: GOA_BLOB_ENDPOINT points at a non-local host — skipping MinIO."
    fi
    return 0
  fi

  require_docker

  if [ -z "${GOA_BLOB_ACCESS_KEY:-}" ] || [ -z "${GOA_BLOB_SECRET_KEY:-}" ]; then
    echo "error: GOA_BLOB_ACCESS_KEY and GOA_BLOB_SECRET_KEY must be set (≥8 chars)." >&2
    exit 1
  fi

  local port
  port="$(minio_port)"

  case "$(container_state "$MINIO_CONTAINER")" in
    running)
      echo "demo-db: $MINIO_CONTAINER already running."
      ;;
    exited)
      echo "demo-db: starting existing $MINIO_CONTAINER…"
      docker start "$MINIO_CONTAINER" >/dev/null
      ;;
    missing)
      echo "demo-db: creating $MINIO_CONTAINER on localhost:$port…"
      docker run -d --name "$MINIO_CONTAINER" \
        -e MINIO_ROOT_USER="$GOA_BLOB_ACCESS_KEY" \
        -e MINIO_ROOT_PASSWORD="$GOA_BLOB_SECRET_KEY" \
        -p "${port}:9000" \
        -v "$MINIO_VOLUME":/data \
        "$MINIO_IMAGE" \
        server /data --console-address ":9001" >/dev/null
      ;;
  esac

  echo -n "demo-db: waiting for MinIO to become healthy"
  for _ in $(seq 1 60); do
    if docker exec "$MINIO_CONTAINER" \
        curl -fs "http://127.0.0.1:9000/minio/health/live" >/dev/null 2>&1; then
      echo " ✓"
      break
    fi
    echo -n "."
    sleep 0.5
  done

  # Idempotent bucket creation via a one-shot `mc` container on the same
  # docker bridge as the MinIO container.
  echo "demo-db: ensuring bucket '$GOA_BLOB_BUCKET' exists…"
  docker run --rm \
    --network "container:$MINIO_CONTAINER" \
    -e MC_HOST_local="http://${GOA_BLOB_ACCESS_KEY}:${GOA_BLOB_SECRET_KEY}@127.0.0.1:9000" \
    "$MC_IMAGE" \
    mb --ignore-existing "local/$GOA_BLOB_BUCKET" >/dev/null
}

minio_down() {
  if [ "$(container_state "$MINIO_CONTAINER")" != missing ]; then
    echo "demo-db: removing $MINIO_CONTAINER (volume $MINIO_VOLUME preserved)…"
    docker rm -f "$MINIO_CONTAINER" >/dev/null
  fi
}

minio_wipe() {
  minio_down
  if docker volume inspect "$MINIO_VOLUME" >/dev/null 2>&1; then
    echo "demo-db: removing data volume $MINIO_VOLUME…"
    docker volume rm "$MINIO_VOLUME" >/dev/null
  fi
}

# ─── Entrypoints ────────────────────────────────────────────

cmd_up() {
  postgres_up
  minio_up
}

cmd_down() {
  if ! command -v docker >/dev/null 2>&1; then
    return 0
  fi
  postgres_down
  minio_down
}

cmd_wipe() {
  if ! command -v docker >/dev/null 2>&1; then
    return 0
  fi
  postgres_wipe
  minio_wipe
}

case "${1:-up}" in
  up)   cmd_up ;;
  down) cmd_down ;;
  wipe) cmd_wipe ;;
  *)
    echo "usage: $0 {up|down|wipe}" >&2
    exit 2
    ;;
esac
