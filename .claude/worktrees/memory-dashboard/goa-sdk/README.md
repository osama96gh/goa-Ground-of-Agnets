# goa-sdk — Python convenience wrapper

`goa_sdk` is a thin Python convenience wrapper over the [Goa HTTP API](../openapi.json).
The API is the contract; this SDK is one of its consumers. The dashboard
([`goa-dashboard/src/api/client.ts`](../goa-dashboard/src/api/client.ts)) is
another, in TypeScript over raw `fetch()`. Any HTTP client in any language
can participate — see [`examples/http/`](../examples/http) for the same flow
in shell.

If you're integrating Goa in a language we don't ship an SDK for, generate
one from [`openapi.json`](../openapi.json) with
[`openapi-generator`](https://openapi-generator.tech/) or use raw HTTP — both
paths are first-class.

## What this gives you over raw HTTP

The SDK is not necessary — but in Python it's nicer. Specifically:

- **Typed event union.** `OutboundQuestion`, `OutboundAnswer`, `OutboundInfo`,
  `OutboundCancelQuestion`, `OutboundCancelAllQuestions` are Pydantic
  models; type-check what you send instead of hand-rolling JSON envelopes.
- **Decoded error envelopes.** `GoaSdkError` carries `code`, `message`, and
  `http_status` parsed from the `{error: {code, message}}` shape — no
  ad-hoc status-code branching.
- **SSE auto-reconnect with replay cursor.** `client.stream()` handles
  `Last-Event-ID` reconnection, ping frames, and the disconnect/reconnect
  dance so you can just write `async for frame in frames: ...`.
- **Sugar helpers.**
  - [`upsert_and_send`](src/goa_sdk/client.py) — `upsert_task` + `append_event`
    in one call (the canonical "first message in a thread" pattern).
  - [`start_task`](src/goa_sdk/client.py) — `create_task` + `append_event`
    when you have no external_ref.
  - [`register_participant`](src/goa_sdk/client.py) — POSTs to `/participants`
    and returns a configured `Goa` client instead of just the api_key.
- **Async context manager.** `async with Goa(api_key, base_url) as client:`
  for clean teardown.

## When NOT to use the SDK

- **Non-Python agents.** Go, Rust, Java, TypeScript — generate from
  [`openapi.json`](../openapi.json) or hand-write HTTP.
- **Infra scripting.** Shell pipelines, ops tooling, healthchecks —
  see [`examples/http/`](../examples/http) for curl.
- **Admin operations.** The SDK intentionally does not wrap `/admin/*`
  (different auth model, different audience). Use raw HTTP with the
  `GOA_ADMIN_TOKEN` Bearer or the dashboard.

## Coverage matrix

21 SDK methods cover all 17 participant-authed HTTP routes 1:1:

| HTTP endpoint | SDK method |
| --- | --- |
| `POST /participants` | `Goa.register_participant(...)` |
| `GET /participants` | `client.search_participants(...)` |
| `GET /participants/{id}` | `client.get_participant(id)` |
| `POST /tasks` | `client.create_task(...)` |
| `POST /tasks/upsert` | `client.upsert_task(...)` |
| `POST /tasks/{id}/close` | `client.close_task(id)` |
| `POST /tasks/{id}/events` | `client.append_event(id, event)` |
| `GET /tasks` | `client.list_tasks(...)` |
| `GET /tasks/{id}` | `client.get_task(id)` |
| `GET /tasks/{id}/children` | `client.list_children(id)` |
| `GET /pending` | `client.pending()` |
| `POST /tasks/{id}/blobs` | `client.upload_blob(id, ...)` |
| `GET /blobs/{id}/meta` | `client.get_blob_meta(id)` |
| `GET /blobs/{id}` | `client.download_blob(id)` / `client.open_blob(id)` |
| `GET /stream` | `client.stream(...)` |

Plus two sugar methods that compose multiple HTTP calls (`upsert_and_send`,
`start_task`) and a class-method bootstrap (`register_participant`).

Admin endpoints (`GET /admin/tasks`, `POST /admin/participants`, etc.) are
**intentionally not in the SDK** — they're an operator surface gated on a
shared admin token, not a participant API key.

## Quickstart

The canonical "chat-service asks support-agent a question" flow. See
[`examples/chat-service/cli.py`](../examples/chat-service/cli.py) for the
full version.

```python
import asyncio
from goa_sdk import Goa, OutboundQuestion
from goa_sdk.events import AnswerEvent, Content, QuestionPayload

async def main() -> None:
    # 1. Bootstrap a participant; the api_key is shown once — persist it.
    client, api_key, me = await Goa.register_participant(
        "http://127.0.0.1:8000",
        type="service",
        name="my-chat-service",
        capabilities=["chat"],
    )

    # 2. Find the support agent.
    support = (await client.search_participants(capability=["support"]))[0]

    # 3. Subscribe to inbound events, then ask a question.
    async with client.stream() as frames:
        await asyncio.sleep(0.1)  # let the subscription register
        task, _, _ = await client.upsert_and_send(
            external_ref="thread-42",
            event=OutboundQuestion(
                payload=QuestionPayload(to=[support.id]),
                content=Content(text="hi, can I get a refund for order #42?"),
            ),
            subject="thread-42",
        )

        # 4. Wait for the answer.
        async for frame in frames:
            ev = frame.event
            if isinstance(ev, AnswerEvent) and frame.task_id == task.id:
                print("reply:", ev.content.text)
                break

    await client.aclose()

asyncio.run(main())
```

## Where to look next

- **HTTP contract:** [`openapi.json`](../openapi.json) at the repo root —
  source-of-truth schema, also served live at `/openapi.json`.
- **Concepts and invariants:** [`specs/goa.md`](../specs/goa.md) — the
  conceptual spec (participants, tasks, events, pending questions,
  sub-tasks, visibility rules).
- **Same flow, three ways:** [`examples/README.md`](../examples/README.md)
  — Python SDK, raw HTTP (`examples/http/`), and the browser dashboard.
