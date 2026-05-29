# Goa over raw HTTP

The same flow `examples/chat-service/cli.py` demonstrates with the Python SDK,
written instead as a handful of `curl` scripts. The HTTP API is Goa's primary
contract — these scripts exist to make that concrete for non-Python users
and to serve as copy-pasteable wire-shape documentation.

## Prerequisites

- A Goa hub reachable at `$BASE_URL` (defaults to `http://127.0.0.1:8000`).
- `jq` for JSON extraction. Available everywhere.
- The **support-agent** and **payments-agent** registered AND running, since
  they're the participants that actually answer the question. The simplest
  way is to run `make demo` from the repo root — it brings up the hub, all
  three demo agents, and the dashboard. The curl scripts here register a
  *separate* `chat-service-curl` identity that asks the running support
  agent the same canonical refund question.

## What each script does

```
00_register.sh        # POST /participants — register chat-service-curl + look up support
01_create_task.sh     # POST /tasks/upsert  — find-or-create a task by external_ref
02_ask_question.sh    # POST /tasks/{id}/events — question event targeting support
03_stream_answer.sh   # GET  /stream         — wait for the AnswerEvent, print, exit
04_close.sh           # POST /tasks/{id}/close — mark the task closed (initiator-only)
```

Bearer tokens, participant IDs, and the task ID flow between scripts via a
local `.env.http` file that `00_register.sh` creates. The file is gitignored.

## Run order

In one terminal, with `make demo` already running:

```sh
cd examples/http
bash 00_register.sh        # writes .env.http with API key + IDs
bash 01_create_task.sh     # writes TASK_ID into .env.http
bash 02_ask_question.sh    # fires the question
bash 03_stream_answer.sh   # blocks until the answer arrives, prints it
bash 04_close.sh           # closes the task
```

Override the hub URL with `BASE_URL=http://your-host[:port] bash 00_register.sh`.

## What this proves

Goa is HTTP-first. The Python SDK is a convenience wrapper over these same
calls; the React dashboard (`goa-dashboard/src/api/client.ts`) is another
non-SDK HTTP consumer of the same hub. Any HTTP client in any language can
participate.

The committed [`openapi.json`](../../openapi.json) at the repo root is the
machine-readable contract these scripts conform to — feed it to
`openapi-generator-cli` to scaffold a client in your language of choice.
