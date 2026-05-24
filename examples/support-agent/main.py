"""Support agent.

Answers most messages directly. On the keyword `"refund"`, spawns a sub-task
to the payments specialist (§5 scenario), waits for the specialist's answer,
then forwards a synthesized reply on the parent task. The customer-facing
chat service never sees the sub-task — that's the privacy guarantee from
§8.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goa_sdk import Goa, OutboundAnswer, OutboundQuestion
from goa_sdk.events import (
    AnswerEvent,
    AnswerPayload,
    Content,
    QuestionEvent,
    QuestionPayload,
)

from _shared import base_url_arg, load_example_env


EXAMPLE_DIR = Path(__file__).resolve().parent

PAYMENT_KEYWORD = "refund"

# Fake "thinking" delay before the support agent replies, so the customer-
# facing UI has a visible round-trip rather than an instant echo.
REPLY_DELAY_SECONDS = 2.0


async def _find_payments(client: Goa) -> UUID:
    """Discovery via §11. Capability tag is the contract."""
    candidates = await client.search_participants(capability=["payments"])
    if not candidates:
        raise RuntimeError("no payments-capable participant registered")
    return candidates[0].id


async def run(base_url: str) -> None:
    api_key, me_id = load_example_env(EXAMPLE_DIR)
    client = Goa(api_key, base_url)
    print(f"[support] starting as {me_id}")

    payments_id = await _find_payments(client)

    # parent_task_id → child sub-task id. We wait for the sub-task answer
    # before replying on the parent. (Production code should also handle
    # restart resumption — out of scope for the example.)
    parent_for_child: dict[UUID, UUID] = {}
    pending_parent_question: dict[UUID, UUID] = {}

    try:
        async with client.stream() as frames:
            async for frame in frames:
                if frame.event_name != "event" or frame.event is None:
                    continue
                ev = frame.event
                task_id = frame.task_id
                assert task_id is not None

                # Customer-side question: either escalate or answer directly.
                if isinstance(ev, QuestionEvent) and me_id in ev.payload.to:
                    text = (ev.content.text or "").lower()
                    if PAYMENT_KEYWORD in text:
                        print(f"[support] escalating to payments for task {task_id}")
                        sub_task, _ = await client.start_task(
                            parent_task_id=task_id,
                            first_event=OutboundQuestion(
                                payload=QuestionPayload(to=[payments_id]),
                                content=Content(
                                    text=f"customer asking about: {ev.content.text}"
                                ),
                            ),
                            subject="payments consult",
                        )
                        parent_for_child[sub_task.id] = task_id
                        pending_parent_question[sub_task.id] = ev.id
                    else:
                        print(f"[support] answering directly on {task_id} "
                              f"(after {REPLY_DELAY_SECONDS}s)")
                        await asyncio.sleep(REPLY_DELAY_SECONDS)
                        await client.append_event(
                            task_id,
                            OutboundAnswer(
                                payload=AnswerPayload(answering=[ev.id]),
                                content=Content(
                                    text=f"support reply: {ev.content.text or ''}",
                                ),
                            ),
                        )
                    continue

                # Sub-task answer from payments: forward to the parent.
                if (
                    isinstance(ev, AnswerEvent)
                    and task_id in parent_for_child
                ):
                    parent_id = parent_for_child.pop(task_id)
                    parent_q = pending_parent_question.pop(task_id, None)
                    if parent_q is None:
                        continue
                    print(f"[support] forwarding payments answer to parent "
                          f"{parent_id} (after {REPLY_DELAY_SECONDS}s)")
                    await asyncio.sleep(REPLY_DELAY_SECONDS)
                    await client.append_event(
                        parent_id,
                        OutboundAnswer(
                            payload=AnswerPayload(answering=[parent_q]),
                            content=Content(
                                text=f"per payments: {ev.content.text or ''}",
                            ),
                        ),
                    )
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Goa v2 example — support agent")
    base_url_arg(parser)
    args = parser.parse_args()
    asyncio.run(run(args.base_url))


if __name__ == "__main__":
    main()
