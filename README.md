# Goa — Ground of Agents

A centralized hub for **multi-party agent coordination** with persistent task
state. Agents and services register once, hold a single long-lived connection,
and exchange typed events inside named tasks. Sub-tasks give first-class
support to private delegation; pending-question state is materialized so any
participant can ask "what do I owe a reply to?" without scanning logs.

**Goa is HTTP API-first.** Every integration — Python SDK, React dashboard,
your own agent in whatever language — talks to the hub over the same HTTP API.
The contract lives in [`openapi.json`](openapi.json) at the repo root (also
served live at `/openapi.json` by the hub). Codegen friendly; not Python-locked.

Read [specs/goa.md](specs/goa.md) for the full conceptual spec.

## Quickstart

```sh
git clone <repo-url> goa && cd goa
make install
make demo
```

That's it. `make demo` bootstraps `.env.local` (with a random dev pepper),
starts the hub on `:8000`, registers the three example agents, brings them
online, and serves the dashboard on `:5173` — all in one terminal, with the
dashboard auto-logged-in.

### Prerequisites

- Python 3.11+ with [`uv`](https://docs.astral.sh/uv/) installed
- Node.js 18+ with `npm`
- `make`, `bash`, `openssl`, `nc` (preinstalled on macOS/Linux)

## Integrate in 30 seconds

Same flow, two ways. The HTTP version is the contract; the SDK version is the
Python shorthand for it.

### Raw HTTP (any language)

```sh
# 1. Register a participant — POST /participants is the only unauth endpoint.
api_key=$(curl -sS -X POST http://127.0.0.1:8000/participants \
  -H "Content-Type: application/json" \
  -d '{"type":"service","name":"my-chat","capabilities":["chat"]}' \
  | jq -r .api_key)

# 2. Find an agent with a capability you need.
support_id=$(curl -sS http://127.0.0.1:8000/participants?capability=support \
  -H "Authorization: Bearer $api_key" \
  | jq -r .participants[0].id)

# 3. Open a task and ask a question in two calls.
task_id=$(curl -sS -X POST http://127.0.0.1:8000/tasks/upsert \
  -H "Authorization: Bearer $api_key" -H "Content-Type: application/json" \
  -d '{"external_ref":"thread-1","on_create":{"subject":"thread-1"}}' \
  | jq -r .task.id)
curl -sS -X POST "http://127.0.0.1:8000/tasks/$task_id/events" \
  -H "Authorization: Bearer $api_key" -H "Content-Type: application/json" \
  -d "$(jq -n --arg to "$support_id" \
        '{event_type:"question",payload:{to:[$to]},content:{text:"hi"}}')"
```

The full version is at [`examples/http/`](examples/http) (5 scripts that
together drive the canonical refund flow). Or generate a client in your
language from [`openapi.json`](openapi.json) with
[`openapi-generator`](https://openapi-generator.tech/).

### Python SDK

```python
from goa_sdk import Goa, OutboundQuestion
from goa_sdk.events import Content, QuestionPayload

client, api_key, me = await Goa.register_participant(
    "http://127.0.0.1:8000",
    type="service", name="my-chat", capabilities=["chat"],
)
support = (await client.search_participants(capability=["support"]))[0]
task, _, _ = await client.upsert_and_send(
    external_ref="thread-1",
    event=OutboundQuestion(
        payload=QuestionPayload(to=[support.id]),
        content=Content(text="hi"),
    ),
    subject="thread-1",
)
```

The SDK is a thin wrapper around the same HTTP calls — see
[`goa-sdk/README.md`](goa-sdk/README.md) for what it adds (typed errors,
SSE auto-reconnect, sugar helpers like `upsert_and_send`).

## What's in the box

| Path | What it is |
| --- | --- |
| [openapi.json](openapi.json) | **The API contract.** Source of truth for endpoints, payloads, error envelopes. |
| [goa-core/](goa-core/) | **The HTTP API.** FastAPI app + persistence + SSE fan-out. |
| [goa-sdk/](goa-sdk/) | **Python convenience wrapper** over the HTTP API. One consumer, not the canonical path. |
| [goa-dashboard/](goa-dashboard/) | **Read-only React UI.** A non-SDK HTTP client (TypeScript over `fetch()`, see [src/api/client.ts](goa-dashboard/src/api/client.ts)). |
| [examples/](examples/) | **Same flow, three ways** — Python SDK (`chat-service/`, `support-agent/`, `payments-agent/`), raw HTTP ([`http/`](examples/http)), and the browser dashboard. |
| [specs/](specs/) | Conceptual spec ([goa.md](specs/goa.md)) and deferred work ([goa-roadmap.md](specs/goa-roadmap.md)). |
| [supabase/](supabase/) | Supabase project config for the Postgres backend path. |

## Pick your persistence backend

State backend is selected by a single env var, [`GOA_DATABASE_URL`](.env.local.example),
following the standard URL-scheme convention:

| Backend | Use when | URL |
| --- | --- | --- |
| **Postgres** | Local dev — default; production-shaped | `GOA_DATABASE_URL=postgresql://goa:goa@localhost:5432/goa` |
| **SQLite** | No Docker available, single-file persistence | `GOA_DATABASE_URL=sqlite:./goa.db` |
| **In-memory** | First poke, transient tests | `GOA_DATABASE_URL=` *(empty)* |

Open `.env.local` after the first `make demo`: each block is commented inline
with the tradeoffs. Uncomment the one you want and restart. (The dockerized
stack reads `.env.docker` instead — see [DEPLOY.md](DEPLOY.md).)

## Useful commands

```sh
make demo            # the only command you should need day-to-day
make demo-clean      # wipe DB + agent registrations, then `make demo` re-registers
make openapi-export  # regenerate openapi.json after API changes
make test            # goa-core + goa-sdk test suites
make dashboard-build # production static bundle in goa-dashboard/dist/
make help            # full target list
```

## Deploying for real

For self-hosting Goa on a VM with HTTPS, an external Postgres or
Supabase, and the dashboard at your own domain — see **[DEPLOY.md](DEPLOY.md)**.
The short version:

```sh
make bootstrap-env && $EDITOR .env.docker   # fill in domain, secrets, blob creds
make up                                     # docker compose up -d with safety checks
```

## Documentation

- **[openapi.json](openapi.json)** — the machine-readable API contract.
- **[specs/goa.md](specs/goa.md)** — concepts, invariants, wire shape.
- **[specs/goa-roadmap.md](specs/goa-roadmap.md)** — what's deferred and why.
- **[goa-sdk/README.md](goa-sdk/README.md)** — Python SDK, what it adds over raw HTTP.
- **[examples/README.md](examples/README.md)** — same flow, three ways.
- **[goa-dashboard/README.md](goa-dashboard/README.md)** — dashboard-specific notes.

## License

TBD.
