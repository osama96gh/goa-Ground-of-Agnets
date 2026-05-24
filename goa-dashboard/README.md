# Goa Dashboard (v2)

Read-only observability dashboard for a running Goa hub. Shows the live event
firehose, the participant directory, and per-task event logs. **No write
surface** — task creation and event composition happen via the SDK or
[examples/](../examples) scripts, not the dashboard.

## Running it

The dashboard talks to the hub's `/admin/*` routes, which are gated behind
`GOA_ADMIN_TOKEN`. That env var must be set on the hub for the routes to
exist (the repo's `.env.example` ships a development default).

The simplest path is `make demo` from the repo root — it brings up the hub,
the three example participants, and the dashboard together. On first load
the dashboard prompts for the admin token; paste the value of
`GOA_ADMIN_TOKEN` from your `.env`. It is stored in `localStorage`.

## Pages

- **Timeline** — newest-first list of every event in the system, streamed via
  `GET /admin/stream`. Click an event to jump to its task.
- **Tasks** — global task list (top-level by default; toggle to include
  sub-tasks). Filter by `has_pending`. Backed by `GET /admin/tasks`.
- **Tasks/:id** — task detail: header, pending pairs, sub-task tree, full
  event log. Backed by `GET /admin/tasks/{id}`.
- **Participants** — registry directory with capability AND-ing, name/q
  search, and type filter. Backed by `GET /admin/participants`.

## Building for production

```sh
make dashboard-build      # → goa-dashboard/dist/
```

The bundle is plain static assets; serve `dist/` from any web server. Set up
an `/admin` reverse proxy to the hub at runtime (the dev server uses Vite's
proxy in [vite.config.ts](vite.config.ts)).

## What's not in this dashboard

- Posting events (questions, answers, info) — use the SDK.
- Creating tasks or sub-tasks — use the SDK.
- Registering participants from the UI — use `POST /participants` directly
  or the [examples](../examples) which all self-register.

These were intentionally cut from the dashboard's scope to keep it small and
focused on "see what's happening." See [specs/goa.md](../specs/goa.md) §13 for
the items deferred to future work.
