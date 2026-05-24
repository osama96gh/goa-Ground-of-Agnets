"""Payments specialist agent.

Subscribes to its stream and answers any `question` event targeted at it. No
awareness of the chat service or sub-task structure — it just sees questions
land on tasks it's a participant of and answers them. Spec §5.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow `python examples/payments-agent/main.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goa_sdk import Goa, OutboundAnswer
from goa_sdk.events import AnswerPayload, Content, QuestionEvent

from _shared import base_url_arg, load_example_env

EXAMPLE_DIR = Path(__file__).resolve().parent


def _payments_answer(text: str | None) -> str:
    """Hardcoded reply — keeps the example deterministic for the golden e2e."""
    return f"refund issued for: {text or '(no detail)'}"


async def run(base_url: str) -> None:
    api_key, me_id = load_example_env(EXAMPLE_DIR)
    client = Goa(api_key, base_url)
    print(f"[payments] starting as {me_id}")

    try:
        async with client.stream() as frames:
            async for frame in frames:
                if frame.event_name != "event" or frame.event is None:
                    continue
                if not isinstance(frame.event, QuestionEvent):
                    continue
                if me_id not in frame.event.payload.to:
                    continue
                reply = _payments_answer(frame.event.content.text)
                print(f"[payments] answering question {frame.event.id} on task {frame.task_id}")
                await client.append_event(
                    frame.task_id,
                    OutboundAnswer(
                        payload=AnswerPayload(answering=[frame.event.id]),
                        content=Content(text=reply),
                    ),
                )
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Goa v2 example — payments agent")
    base_url_arg(parser)
    args = parser.parse_args()
    asyncio.run(run(args.base_url))


if __name__ == "__main__":
    main()
