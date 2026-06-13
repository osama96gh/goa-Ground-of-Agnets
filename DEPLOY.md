# Deploying Goa

This is the **self-host** path: one VM, Docker, one domain. You get HTTPS,
the dashboard, the hub API, and a Postgres of your choice — all from one
`docker compose` command.

For local development, see [README.md](README.md) — that's a different
workflow (`make demo`).

## Prerequisites

- A host with Docker 24+ and Docker Compose v2.
- A DNS A/AAAA record pointing your domain at the host (skip this if you'll
  use `GOA_DOMAIN=:443` for self-signed local TLS).
- Ports 80 and 443 reachable from the public internet (Let's Encrypt does
  HTTP-01 challenges on port 80).

## Five-minute deploy

```sh
git clone <repo-url> goa && cd goa
make bootstrap-env                    # creates .env.local + .env.docker from templates
$EDITOR .env.docker                   # fill in GOA_DOMAIN, secrets, blob creds
make up                               # docker compose up -d, with safety checks
```

`make up` warns on placeholder secrets and missing required vars, then
runs Compose. The dockerized stack reads **only** `.env.docker` — a
complete, self-contained env file. The other file, `.env.local`, is the
equivalent for `make demo` (native hub on the host); the two files don't
overlay or share, by design. See [README.md](README.md) for the local-dev
workflow.

The bundled Postgres service always runs as part of the stack; to use an
external Postgres instead, set `GOA_DATABASE_URL` to an external host in
`.env.docker`, then run `EXTERNAL_DB=1 make up` to stop the bundled
service.

Once it's up, your domain serves:

**The HTTP API is the primary surface.** The dashboard and the Python SDK
are both clients of it; other languages can codegen from
`https://<domain>/openapi.json` (or the [committed `openapi.json`](openapi.json)).

| Path | What it is |
| --- | --- |
| `https://<domain>/tasks`, `/participants`, `/memory`, `/blobs/*`, `/pending` | Public hub API (Bearer-auth with participant API keys) |
| `https://<domain>/stream` | SSE endpoint for participants (any HTTP client; the SDK is one) |
| `https://<domain>/openapi.json` | Public API contract — machine-readable schema for codegen |
| `https://<domain>/admin/*` | Admin API (token-gated; the dashboard uses this) |
| `https://<domain>/` | Dashboard (prompts for `GOA_ADMIN_TOKEN` on first load) |
| `https://<domain>/health` | Liveness/readiness probe (returns 200/503) |

## Picking your Postgres

| Backend | Use when | URL shape | Notes |
| --- | --- | --- | --- |
| **Bundled** | First deploy, want zero external accounts | `postgresql://goa:goa@postgres:5432/goa` | Always runs by default. |
| **Supabase** | Already on Supabase | `postgresql://postgres.<ref>:<pwd>@aws-0-<region>.pooler.supabase.com:5432/postgres` | Use `EXTERNAL_DB=1` to stop the bundled service. |
| **Neon** | Want serverless Postgres | `postgresql://user:pass@ep-xxx.<region>.neon.tech/goa?sslmode=require` | Use `EXTERNAL_DB=1` to stop the bundled service. |
| **AWS RDS / self-hosted** | Bring your own | `postgresql://user:pass@host:5432/goa` | Use `EXTERNAL_DB=1` to stop the bundled service. |

> **⚠ Supabase users: use port 5432, NOT 6543.**
> The pooler URL shown in the "Transaction" tab of the Supabase dashboard
> won't work. `PostgresAdapter` uses prepared statements, which the
> transaction-mode pooler strips. Use the **Session pooler** URL (5432) or
> the **Direct connection** URL. Both work; the session pooler is usually
> easier on firewall-restricted networks.

The same rule applies to PgBouncer in transaction mode — Goa needs a
session-mode connection.

## Picking your blob backend

Postgres holds no blob bytes in this design — bytes go to an S3-compatible
store. With any Postgres `GOA_DATABASE_URL` you **must** set
`GOA_BLOB_BACKEND=s3` and fill in the five S3 fields; the hub refuses to
start otherwise.

The default `.env.docker` points at the **bundled MinIO** service in
`docker-compose.yml`, so `docker compose up` works offline out of the box.
A one-shot `minio-init` container creates the configured bucket
(idempotent) before the hub starts; the hub waits on
`service_completed_successfully` to avoid race conditions on first start.

To swap to a managed store, change `GOA_BLOB_ENDPOINT` (and the
credentials) in `.env.docker`, then stop the bundled MinIO:

```sh
docker compose --env-file .env.docker up --scale minio=0 --scale minio-init=0
```

Tested with:

| Provider | `GOA_BLOB_ENDPOINT` |
| --- | --- |
| Bundled MinIO | `http://localhost:9000` (native) / `http://minio:9000` (in-Docker) |
| AWS S3 | `https://s3.<region>.amazonaws.com` |
| Cloudflare R2 | `https://<account-id>.r2.cloudflarestorage.com` |
| Supabase Storage | `https://<project-ref>.storage.supabase.co/storage/v1/s3` |
| Backblaze B2 | `https://s3.<region>.backblazeb2.com` |

Metadata (filename, MIME type, size, task association) stays in Postgres —
only the bytes move to S3.

## Common operations

```sh
make up           # bring up the stack (with safety checks)
make down         # stop the stack (data volumes preserved)
make logs         # tail logs from all services
make update       # pull/rebuild images and recreate changed containers
```

To wipe and start fresh (destructive — kills bundled Postgres data):

```sh
docker compose --env-file .env.docker down -v
```

## Known limitations

These are real constraints in this release — please don't be surprised.

- **Single replica only.** The SSE fan-out layer (`StreamHub` in
  `goa-core/src/goa/stream/`) is in-process. Running two `hub` replicas
  would cause SSE clients connected to replica B to miss events written
  on replica A. A Redis/NATS pub-sub adapter is roadmap work; until then,
  scale vertically.
- **No transaction-mode pooling.** See the Supabase callout above. Goa
  uses prepared statements; transaction-mode poolers strip them.
- **Schema applies on first connect; no migration tool.** `PostgresAdapter`
  runs `CREATE TABLE IF NOT EXISTS` on startup. Downgrades and manual
  schema edits are not first-class.
- **Dashboard SPA / hub API path collision.** `/tasks`, `/participants`, and `/memory`
  are both dashboard client-routes and hub API endpoints. The Caddyfile
  resolves this by content-negotiation: `Accept: text/html` → SPA,
  anything else → API. This works for all real-world clients (browsers
  vs SDKs) but a `curl` with no `Accept` header on `/tasks` would land
  on the hub API (returning JSON), which is usually what you want anyway.

## Architecture

```
       ┌──────────────────────────────────────────────────┐
       │                                                  │
   ▶ ──┤ web   :443 (Caddy)                               │
       │   ├─ TLS via Let's Encrypt (auto)                │
       │   ├─ Dashboard SPA at /  (static, from image)    │
       │   ├─ /admin/* /stream /health → hub:8000         │
       │   └─ /tasks /participants /memory → negotiated   │
       │                                                  │
       │   hub   :8000 (FastAPI / uvicorn)                │
       │   │ ├─ task / participant / event reads + writes │
       │   ▼ ▼                                            │
       │   postgres :5432   minio :9000                   │
       │   (metadata)       (blob bytes)                  │
       │                                                  │
       │   Both bundled and always-on by default. Point   │
       │   at managed alternatives in .env.docker and use │
       │   --scale to stop the bundled ones.              │
       │                                                  │
       └──────────────────────────────────────────────────┘
```

The whole stack is three (or four) containers. Caddy is the single edge.

## Rotating secrets

To rotate `GOA_ADMIN_TOKEN`:

1. Edit `.env.docker`, set the new value.
2. `docker compose --env-file .env.docker up -d hub`
   (recreates only the hub container).
3. Reload the dashboard in your browser; it'll prompt for the new token.

Rotating `GOA_SERVER_PEPPER` is **destructive** — it invalidates every
participant's API key. Plan for re-registering all agents/services after
rotation, or don't rotate.

## Going beyond a single VM

This deployment plan covers what most OSS users need. For larger needs:

- **Cloud one-click (Fly.io / Render):** roadmap — same Dockerfile, new
  platform config.
- **Kubernetes:** roadmap — a Helm chart.
- **Multi-replica horizontal scaling:** requires a goa-core refactor of
  the StreamHub (Redis or NATS adapter). Tracked in
  [specs/goa-roadmap.md](specs/goa-roadmap.md).
