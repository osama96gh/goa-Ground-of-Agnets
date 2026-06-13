# Goa v2 reference examples

## Same flow, three ways

Goa is HTTP API-first — the Python SDK is one consumer, and the dashboard
is another. The same conversation ("chat-service asks support, support
spawns a private sub-task to payments on `refund`, support answers")
shows up here in three forms:

- **Python SDK** — [`chat-service/`](chat-service/),
  [`support-agent/`](support-agent/), [`payments-agent/`](payments-agent/).
  Three Pythonic agents using [`goa_sdk`](../goa-sdk).
- **Raw HTTP / curl** — [`http/`](http/). Five small shell scripts that
  reproduce the chat-service half of the flow over the wire. Read these
  to learn the request/response shapes without going through Python.
- **Browser** — the dashboard at <http://localhost:5173> during
  `make demo`. A non-SDK TypeScript consumer of the same hub
  (see [`goa-dashboard/src/api/client.ts`](../goa-dashboard/src/api/client.ts)).

The committed [`openapi.json`](../openapi.json) is the machine-readable
contract these three converge on. Feed it to `openapi-generator-cli` for a
client in your language of choice.

---

## Reference participants (Python SDK)

Three participants that demonstrate the v2 contract end-to-end:

- [`chat-service/`](chat-service/) — service participant fronting a chat.
  `main.py` is a tiny FastAPI app that serves a single-page browser UI
  (pick or create a thread, type a message, see the agent's reply); the
  legacy one-shot CLI is preserved next to it as `cli.py`. Both use
  `upsert_task` keyed on the thread id and hold **no** local thread → task
  map (the whole point of `external_ref` per spec §6.4).
- [`support-agent/`](support-agent/) — agent that answers most messages
  directly but, on the keyword `"refund"`, spawns a sub-task to the payments
  specialist before answering the customer.
- [`payments-agent/`](payments-agent/) — answers the support agent's
  sub-task question. Has no awareness of the chat service.

The golden e2e
([`goa-core/tests/integration/test_golden_e2e.py`](../goa-core/tests/integration/test_golden_e2e.py))
runs all three through one Goa instance and asserts the §5 architecture
properties: visibility (chat service never sees the sub-task), sub-task
lifecycle (`child_task_created` lands in the parent), pending drains as both
questions resolve, and `upsert` is stateless from the chat service's side.

## Running the demo

One command from the repo root brings up the hub, all three example
participants, and the dashboard with interleaved logs in a single
terminal:

```sh
make install           # one-time
make demo              # bootstraps .env.local on first run, then starts everything
```

- Chat UI: <http://127.0.0.1:8002> — type or pick a thread, send a
  message; the support agent replies inline. Include the word
  `"refund"` to exercise the §5 sub-task path (support opens a private
  sub-task to payments and answers the customer; the chat service
  never sees the sub-task).
- Dashboard: <http://localhost:5173> — paste the `GOA_ADMIN_TOKEN`
  value on first load. The Timeline view shows every event in real
  time as you exercise the chat UI.

One Ctrl-C in the `make demo` terminal stops everything.

### State persistence

By default `make demo` runs with Postgres in a local container
(`GOA_DATABASE_URL=postgresql://goa:goa@localhost:5432/goa`, from
`.env.local.example`), so hub state lives in a docker volume and
**persists across `make demo` runs**: Ctrl-C, re-run `make demo`, and
your chat thread, events, and pending questions are still there.

When you want a clean slate, run `make demo-clean` first — it removes
the data volumes (Postgres + MinIO blobs) and the per-example `.env`
files so the next `make demo` re-registers everything fresh.

To use SQLite or in-memory instead, edit `.env.local` and uncomment one
of the alternative `GOA_DATABASE_URL=` lines — comments inline explain
the tradeoffs.

## Scripted smoke test

The original one-shot CLI form is still available for callers that want
a script-driven check without the web UI:

```sh
make goa                 # in one terminal
make example-chat-cli    # in another
```
