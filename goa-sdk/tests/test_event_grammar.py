"""SDK happy-paths for the event grammar: drive `info`, `cancel_question`,
and `cancel_all_questions` through the SDK against a live server, and assert
the decoded SSE frames materialize as the right variant.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from goa.config import Settings
from goa.main import create_app
from goa.stream.hub import InMemoryStreamHub

from goa_sdk import (
    CancelAllQuestionsEvent,
    CancelQuestionEvent,
    Goa,
    InfoEvent,
    OutboundAnswer,
    OutboundCancelAllQuestions,
    OutboundCancelQuestion,
    OutboundInfo,
    OutboundQuestion,
    StreamFrame,
)
from goa_sdk.events import (
    AnswerPayload,
    CancelQuestionPayload,
    Content,
    QuestionPayload,
)

from tests._live_server import live_server


pytestmark = pytest.mark.asyncio


async def _wait_for_subscriber(hub: InMemoryStreamHub, pid: UUID, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if hub.has_subscriber(pid):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"subscriber for {pid} never registered")


async def _drain_until(it, event_type: str) -> StreamFrame:
    while True:
        frame = await asyncio.wait_for(it.__anext__(), timeout=5.0)
        if frame.event_name != "event" or frame.event is None:
            continue
        if frame.event.event_type == event_type:
            return frame


async def test_sdk_info_event() -> None:
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

                # alice opens a task targeting bob.
                task, question_event = await alice_client.start_task(
                    first_event=OutboundQuestion(
                        payload=QuestionPayload(to=[bob.id]),
                        content=Content(text="ping?"),
                    ),
                )
                # bob's stream sees the question first.
                await _drain_until(bob_frames, "question")

                # bob emits an info event in reply.
                info_ev = await bob_client.append_event(
                    task.id,
                    OutboundInfo(
                        in_reply_to=question_event.id,
                        content=Content(text="checking another specialist, ~30s"),
                    ),
                )
                info_frame = await _drain_until(bob_frames, "info")
                assert isinstance(info_frame.event, InfoEvent)
                assert info_frame.event.id == info_ev.id
                assert info_frame.event.in_reply_to == question_event.id
                # info doesn't change pending state — bob's pair is still open.
                assert info_frame.task is not None
                assert info_frame.task.pending_questions == [(question_event.id, bob.id)]
        finally:
            await alice_client.aclose()
            await bob_client.aclose()


async def test_sdk_cancel_question_event() -> None:
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
                    first_event=OutboundQuestion(payload=QuestionPayload(to=[bob.id])),
                )
                await _drain_until(bob_frames, "question")

                # Alice retracts.
                await alice_client.append_event(
                    task.id,
                    OutboundCancelQuestion(
                        payload=CancelQuestionPayload(retracts=[question_event.id]),
                    ),
                )
                cancel_frame = await _drain_until(bob_frames, "cancel_question")
                assert isinstance(cancel_frame.event, CancelQuestionEvent)
                assert cancel_frame.event.payload.retracts == [question_event.id]
                assert cancel_frame.task is not None
                assert cancel_frame.task.pending_questions == []

                got = await alice_client.get_task(task.id)
                assert got.pending_questions == []
                assert "cancel_question" in [e.event_type for e in got.events]
        finally:
            await alice_client.aclose()
            await bob_client.aclose()


async def test_sdk_cancel_all_questions_event() -> None:
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
                    first_event=OutboundQuestion(payload=QuestionPayload(to=[bob.id])),
                )
                await _drain_until(bob_frames, "question")

                await alice_client.append_event(task.id, OutboundCancelAllQuestions())
                clear_frame = await _drain_until(bob_frames, "cancel_all_questions")
                assert isinstance(clear_frame.event, CancelAllQuestionsEvent)
                assert clear_frame.task is not None
                assert clear_frame.task.pending_questions == []

                # Late answer is still acceptable but doesn't reopen.
                await bob_client.append_event(
                    task.id,
                    OutboundAnswer(
                        payload=AnswerPayload(answering=[question_event.id]),
                        content=Content(text="late"),
                    ),
                )
                got = await alice_client.get_task(task.id)
                assert got.pending_questions == []
                assert [e.event_type for e in got.events] == [
                    "participant_joined",
                    "question",
                    "cancel_all_questions",
                    "answer",
                ]
        finally:
            await alice_client.aclose()
            await bob_client.aclose()
