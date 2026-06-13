"""Chat service participant — interactive browser demo.

Tiny FastAPI app that fronts a chat UI for a customer talking to the
support agent through Goa. Replaces the original one-shot CLI (preserved
as `cli.py` next to this file).

Lifespan registers a single `chat-service` participant against the Goa
hub and discovers a support-capable agent via §11. The frontend (a
self-contained HTML page in `static/index.html`) polls the JSON endpoints
below; each poll hits Goa once via `goa_sdk.Goa`. There is intentionally
no SSE listener and no in-memory event cache — keeping the demo small.

Endpoints:
- `GET /`                          → static chat UI
- `GET /api/threads`               → list of tasks initiated by chat-service
- `GET /api/messages?external_ref` → flattened event log for that thread
- `POST /api/send`                 → upsert task on first message; append
                                     subsequent messages as new questions

Re-running with the same `--thread` from the UI keeps the same Goa task —
the service holds **no** local thread→task mapping (§6.4).
"""

from __future__ import annotations

import argparse
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goa_sdk import Goa, OutboundQuestion
from goa_sdk.events import AnswerEvent, Content, QuestionEvent, QuestionPayload

from _shared import base_url_arg, load_example_env


EXAMPLE_DIR = Path(__file__).resolve().parent
STATIC_DIR = EXAMPLE_DIR / "static"


class SendBody(BaseModel):
    external_ref: str = Field(min_length=1)
    message: str = Field(min_length=1)


class _State:
    """Process-wide handles. Populated by the lifespan and read by routes."""

    client: Goa | None = None
    me_id: UUID | None = None
    support_id: UUID | None = None


state = _State()


def _build_app(base_url: str) -> FastAPI:
    api_key, me_id = load_example_env(EXAMPLE_DIR)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> Any:
        client = Goa(api_key, base_url)
        print(f"[chat] starting as {me_id}")
        support_id = await _pick_support(client)
        if support_id is None:
            print("[chat] WARNING: no support-capable participant; "
                  "send will return 503 until one registers")
        else:
            print(f"[chat] support: {support_id}")
        state.client = client
        state.me_id = me_id
        state.support_id = support_id
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(title="goa chat-service demo", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/threads")
    async def threads() -> list[dict[str, Any]]:
        client = _require_client()
        items = await client.list_tasks(role="initiator")
        rows = [
            {
                "task_id": str(item.task.id),
                "external_ref": item.task.external_ref,
                "subject": item.task.subject,
                "last_activity_at": item.task.last_activity_at.isoformat(),
            }
            for item in items
            if item.task.external_ref is not None
        ]
        rows.sort(key=lambda r: r["last_activity_at"], reverse=True)
        return rows

    @app.get("/api/messages")
    async def messages(external_ref: str) -> dict[str, Any]:
        client = _require_client()
        matches = await client.list_tasks(external_ref=external_ref)
        if not matches:
            return {"task_id": None, "messages": []}
        task_id = matches[0].task.id
        result = await client.get_task(task_id)
        return {
            "task_id": str(task_id),
            "messages": [_flatten_event(ev) for ev in result.events],
        }

    @app.post("/api/send")
    async def send(body: SendBody) -> dict[str, Any]:
        client = _require_client()
        if state.support_id is None:
            # Re-check in case the support agent came up after our startup.
            picked = await _pick_support(client)
            if picked is not None:
                state.support_id = picked
                print(f"[chat] support: {state.support_id}")
        if state.support_id is None:
            raise HTTPException(
                status_code=503,
                detail="no support-capable participant; "
                       "start examples/support-agent/main.py",
            )

        outbound = OutboundQuestion(
            payload=QuestionPayload(to=[state.support_id]),
            content=Content(text=body.message),
        )
        task, created, event = await client.upsert_and_send(
            external_ref=body.external_ref,
            event=outbound,
            subject=f"thread {body.external_ref}",
        )
        return {
            "task_id": str(task.id),
            "event_id": str(event.id),
            "created": created,
        }

    return app


def _require_client() -> Goa:
    if state.client is None:
        raise HTTPException(status_code=503, detail="chat-service not yet ready")
    return state.client


async def _pick_support(client: Goa) -> UUID | None:
    """Pick the most-recently-registered support-capable participant.

    Hub registrations are append-only and survive script crashes, so an old
    failed run can leave a dead participant behind. Sorting by `created_at`
    desc keeps the demo pointed at the live one without manual cleanup.
    """
    candidates = await client.search_participants(capability=["support"])
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.created_at, reverse=True)
    return candidates[0].id


def _flatten_event(ev: Any) -> dict[str, Any]:
    text = ev.content.text if ev.content else None
    if isinstance(ev, QuestionEvent):
        kind = "question"
    elif isinstance(ev, AnswerEvent):
        kind = "answer"
    else:
        kind = ev.event_type
    if ev.from_ == state.me_id:
        from_role = "you"
    elif ev.from_ == state.support_id:
        from_role = "support"
    elif ev.from_ is None:
        from_role = "system"
    else:
        from_role = "other"
    return {
        "id": str(ev.id),
        "kind": kind,
        "from_role": from_role,
        "text": text,
        "created_at": ev.created_at.isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Goa v2 example — chat-service web demo"
    )
    base_url_arg(parser)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(_build_app(args.base_url), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
