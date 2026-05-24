"""Chat service participant — one-shot CLI form.

Drives a single hardcoded customer message through Goa to the support agent,
prints the answer it gets back, and exits. Demonstrates `upsert_task` keyed
on a thread id so the service holds **no** local mapping table (§6.4).

For the interactive browser-based version, see `main.py` in this directory.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goa_sdk import Goa, OutboundQuestion
from goa_sdk.events import AnswerEvent, Content, QuestionPayload

from _shared import base_url_arg


async def _find_support(client: Goa) -> str:
    candidates = await client.search_participants(capability=["support"])
    if not candidates:
        raise RuntimeError("no support-capable participant registered")
    return str(candidates[0].id)


async def run(base_url: str, thread: str, message: str) -> None:
    client, _api_key, me = await Goa.register_participant(
        base_url,
        type="service",
        name="chat-service",
        description="fronts a chat thread for the customer",
        capabilities=["chat"],
    )
    print(f"[chat] registered as {me.id}")

    support_id = await _find_support(client)

    async with client.stream() as frames:
        # Wait for our subscription to register before sending so we don't
        # miss the inbound answer. The hub's replay buffer would catch us up
        # anyway, but for a script-driven example we keep ordering simple.
        await asyncio.sleep(0.2)

        task, created, _ = await client.upsert_and_send(
            external_ref=thread,
            event=OutboundQuestion(
                payload=QuestionPayload(to=[support_id]),
                content=Content(text=message),
            ),
            subject=f"thread {thread}",
        )
        if created:
            print(f"[chat] opened task {task.id} for thread {thread}")
        else:
            print(f"[chat] resumed existing task {task.id} for thread {thread}")

        async for frame in frames:
            if frame.event_name != "event" or frame.event is None:
                continue
            ev = frame.event
            if isinstance(ev, AnswerEvent) and frame.task_id == task.id:
                print(f"[chat] customer reply: {ev.content.text}")
                break

    await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Goa v2 example — chat service")
    base_url_arg(parser)
    parser.add_argument("--thread", default="slack-thread-demo")
    parser.add_argument(
        "--message",
        default="hi, can I get a refund for order #42?",
        help="hardcoded customer message; include 'refund' to trigger the sub-task path",
    )
    args = parser.parse_args()
    asyncio.run(run(args.base_url, args.thread, args.message))


if __name__ == "__main__":
    main()
