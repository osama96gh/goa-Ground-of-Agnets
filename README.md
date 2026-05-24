# Goa — Ground of Agents

A centralized hub for **multi-party agent coordination** with persistent task
state. Agents and services register once, hold a single long-lived connection,
and exchange typed events inside named tasks. Sub-tasks give first-class
support to private delegation; pending-question state is materialized so any
participant can ask "what do I owe a reply to?" without scanning logs.

Read [specs/goa.md](specs/goa.md) for the full conceptual spec.

## Quickstart

```sh
git clone <repo-url> goa && cd goa
make install
make demo
```

That's it. `make demo` bootstraps `.env` (with a random dev pepper), starts
the hub on `:8000`, registers the three example agents, brings them online,
and serves the dashboard on `:5173` — all in one terminal, with the dashboard
auto-logged-in.

### Prerequisites

- Python 3.11+ with [`uv`](https://docs.astral.sh/uv/) installed
- Node.js 18+ with `npm`
- `make`, `bash`, `openssl`, `nc` (preinstalled on macOS/Linux)

## What's in the box

| Path | What it is |
| --- | --- |
| [goa-core/](goa-core/) | The hub: FastAPI app + persistence + SSE fan-out. |
| [goa-sdk/](goa-sdk/) | Python client for agents and services. |
| [examples/](examples/) | Three reference participants (`chat-service`, `support-agent`, `payments-agent`) that demo the canonical multi-agent flow. |
| [goa-dashboard/](goa-dashboard/) | Read-only Vite/React observability UI — timeline, tasks, participants. |
| [specs/](specs/) | Conceptual spec ([goa.md](specs/goa.md)) and deferred work ([goa-roadmap.md](specs/goa-roadmap.md)). |
| [supabase/](supabase/) | Supabase project config for the Postgres backend path. |

## Pick your persistence backend

State backend is selected by a single env var, [`GOA_DATABASE_URL`](.env.example),
following the standard URL-scheme convention:

| Backend | Use when | URL |
| --- | --- | --- |
| **In-memory** | First poke, transient tests | `GOA_DATABASE_URL=` *(empty)* |
| **SQLite** | Local dev — default | `GOA_DATABASE_URL=sqlite:./goa.db` |
| **Postgres / Supabase** | Production-shaped, multi-process | `GOA_DATABASE_URL=postgresql://…` |

Open `.env` after the first `make demo`: each block is commented inline with
the tradeoffs. Uncomment the one you want and restart.

## Useful commands

```sh
make demo            # the only command you should need day-to-day
make demo-clean      # wipe SQLite + agent registrations, then `make demo` re-registers
make test            # goa-core + goa-sdk test suites
make dashboard-build # production static bundle in goa-dashboard/dist/
make help            # full target list
```

## Documentation

- **[specs/goa.md](specs/goa.md)** — concepts, invariants, wire shape. Start here.
- **[specs/goa-roadmap.md](specs/goa-roadmap.md)** — what's deferred and why.
- **[goa-dashboard/README.md](goa-dashboard/README.md)** — dashboard-specific notes.
- **[examples/README.md](examples/README.md)** — what each demo participant does.

## License

TBD.
