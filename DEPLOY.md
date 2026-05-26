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
cp .env.deploy.example .env.deploy
$EDITOR .env.deploy                   # fill in GOA_DOMAIN, secrets, DB URL
make deploy                           # docker compose up -d, with safety checks
```

`make deploy` rejects placeholder secrets and missing required vars, then
runs Compose with the `bundled-db` profile by default. To use an external
Postgres instead, set `GOA_DATABASE_URL` to an external host in
`.env.deploy` and run `EXTERNAL_DB=1 make deploy`.

Once it's up, your domain serves:

| Path | What it is |
| --- | --- |
| `https://<domain>/` | Dashboard (prompts for `GOA_ADMIN_TOKEN` on first load) |
| `https://<domain>/admin/*` | Admin API (token-gated; the dashboard uses this) |
| `https://<domain>/stream` | SSE endpoint for participants (SDK clients) |
| `https://<domain>/tasks`, `/participants`, `/blobs/*`, `/pending` | Public hub API (Bearer-auth with participant API keys) |
| `https://<domain>/health` | Liveness/readiness probe (returns 200/503) |

## Picking your Postgres

| Backend | Use when | URL shape | Compose profile |
| --- | --- | --- | --- |
| **Bundled** | First deploy, want zero external accounts | `postgresql://goa:goa@postgres:5432/goa` | `--profile bundled-db` |
| **Supabase** | Already on Supabase | `postgresql://postgres.<ref>:<pwd>@aws-0-<region>.pooler.supabase.com:5432/postgres` | *(none)* |
| **Neon** | Want serverless Postgres | `postgresql://user:pass@ep-xxx.<region>.neon.tech/goa?sslmode=require` | *(none)* |
| **AWS RDS / self-hosted** | Bring your own | `postgresql://user:pass@host:5432/goa` | *(none)* |

> **⚠ Supabase users: use port 5432, NOT 6543.**
> The pooler URL shown in the "Transaction" tab of the Supabase dashboard
> won't work. `PostgresAdapter` uses prepared statements, which the
> transaction-mode pooler strips. Use the **Session pooler** URL (5432) or
> the **Direct connection** URL. Both work; the session pooler is usually
> easier on firewall-restricted networks.

The same rule applies to PgBouncer in transaction mode — Goa needs a
session-mode connection.

## Picking your blob backend (optional)

By default, attachments live in Postgres as `bytea` rows. That's fine until
your users attach files in bulk — large blobs in Postgres bloat the
table and slow down everything else.

To route blobs to any S3-compatible store, set:

```sh
GOA_BLOB_BACKEND=s3
GOA_BLOB_ENDPOINT=...
GOA_BLOB_BUCKET=...
GOA_BLOB_REGION=...
GOA_BLOB_ACCESS_KEY=...
GOA_BLOB_SECRET_KEY=...
```

Tested with:

| Provider | `GOA_BLOB_ENDPOINT` |
| --- | --- |
| AWS S3 | `https://s3.<region>.amazonaws.com` |
| Cloudflare R2 | `https://<account-id>.r2.cloudflarestorage.com` |
| Supabase Storage | `https://<project-ref>.storage.supabase.co/storage/v1/s3` |
| MinIO | `https://<your-minio>/` |
| Backblaze B2 | `https://s3.<region>.backblazeb2.com` |

Metadata (filename, MIME type, size, task association) stays in Postgres —
only the bytes move to S3.

## Common operations

```sh
make deploy           # up -d, with safety checks
make deploy-down      # stop the stack (data volumes preserved)
make deploy-logs      # tail logs from all services
make deploy-update    # rebuild and recreate only changed containers
```

To wipe and start fresh (destructive — kills bundled Postgres data):

```sh
docker compose --env-file .env.deploy --profile bundled-db down -v
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
- **Dashboard SPA / hub API path collision.** `/tasks` and `/participants`
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
       │   └─ /tasks /participants → content-negotiated   │
       │                                                  │
       │   hub   :8000 (FastAPI / uvicorn)                │
       │   │                                              │
       │   ▼                                              │
       │   postgres :5432 (bundled, optional profile)     │
       │   OR                                             │
       │   → external Postgres (Supabase / Neon / RDS)    │
       │                                                  │
       └──────────────────────────────────────────────────┘
```

The whole stack is three (or four) containers. Caddy is the single edge.

## Rotating secrets

To rotate `GOA_ADMIN_TOKEN`:

1. Edit `.env.deploy`, set the new value.
2. `docker compose --env-file .env.deploy --profile bundled-db up -d hub`
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
