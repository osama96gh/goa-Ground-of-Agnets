from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from goa.config import Settings
from goa.main import create_app
from goa.stream.hub import InMemoryStreamHub

from goa_sdk import (
    Goa,
    OutboundAnswer,
    OutboundQuestion,
    StreamFrame,
)
from goa_sdk.events import AnswerPayload, Content, QuestionPayload

from tests._live_server import live_server


pytestmark = pytest.mark.asyncio


async def _wait_for_subscriber(hub: InMemoryStreamHub, pid: UUID, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if hub.has_subscriber(pid):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"subscriber for {pid} never registered")


async def _next_event_frame(it) -> StreamFrame:
    while True:
        frame = await asyncio.wait_for(it.__anext__(), timeout=5.0)
        if frame.event_name == "event":
            return frame


async def test_sdk_walking_skeleton() -> None:
    app = create_app(Settings.for_tests())
    hub: InMemoryStreamHub = app.state.ctx.hub

    async with live_server(app) as base_url:
        alice_client, _, alice = await Goa.register_participant(
            base_url, type="agent", name="alice"
        )
        bob_client, _, bob = await Goa.register_participant(
            base_url, type="agent", name="bob"
        )

        try:
            async with bob_client.stream() as bob_frames:
                await _wait_for_subscriber(hub, bob.id)

                task, question_event = await alice_client.start_task(
                    first_event=OutboundQuestion(
                        payload=QuestionPayload(to=[bob.id]),
                        content=Content(text="ping?"),
                    ),
                    subject="hello",
                )

                joined = await _next_event_frame(bob_frames)
                assert joined.event is not None
                assert joined.event.event_type == "participant_joined"
                assert joined.event.payload.participant_id == bob.id  # type: ignore[union-attr]

                question = await _next_event_frame(bob_frames)
                assert question.event is not None
                assert question.event.event_type == "question"
                assert question.event.id == question_event.id

                async with alice_client.stream() as alice_frames:
                    await _wait_for_subscriber(hub, alice.id)

                    await bob_client.append_event(
                        task.id,
                        OutboundAnswer(
                            payload=AnswerPayload(answering=[question_event.id]),
                            content=Content(text="pong"),
                        ),
                    )

                    answer = await _next_event_frame(alice_frames)
                    assert answer.event is not None
                    assert answer.event.event_type == "answer"
                    assert answer.task is not None
                    assert answer.task.pending_questions == []

                got = await alice_client.get_task(task.id)
                assert got.pending_questions == []
                assert [ev.event_type for ev in got.events] == [
                    "participant_joined",
                    "question",
                    "answer",
                ]
        finally:
            await alice_client.aclose()
            await bob_client.aclose()
