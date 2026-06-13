"""Golden e2e for the v2 contract.

Runs the §5 architecture scenario through one Goa instance with the three
reference participants (chat-service, support-agent, payments-agent). Asserts:

1. **Visibility (§8).** The chat service sees the parent task only — calling
   `GET /tasks/{sub_task_id}` returns 404. Likewise, the payments agent
   cannot read the parent.
2. **Sub-task lifecycle (§6.3).** A `child_task_created` system event lands
   in the parent task with the right `spawned_by` and `subject`.
3. **Pending drain (§7).** Both questions (parent + child) close after
   answers; `pending_questions` ends empty on both tasks.
4. **`upsert_task` is stateless service-side (§6.4).** The chat service
   re-upserts with the same `external_ref` and gets the same task id back,
   `created=False`.

This is the acceptance gate for v2-MVP per the implementation plan.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from goa.config import Settings
from goa.main import create_app

from goa_sdk import (
    AnswerEvent,
    ChildTaskCreatedEvent,
    Goa,
    GoaSdkError,
    OutboundAnswer,
    OutboundQuestion,
    QuestionEvent,
)
from goa_sdk.events import AnswerPayload, Content, QuestionPayload

from tests._live_server import live_server


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Mini participant runtimes — equivalent to examples/{support,payments}-agent
# but running as in-process tasks so the test can assert from the outside.
# ---------------------------------------------------------------------------

PAYMENT_KEYWORD = "refund"


@asynccontextmanager
async def _running(coro) -> AsyncIterator[asyncio.Task]:
    task = asyncio.create_task(coro)
    try:
        yield task
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, BaseException):
            pass


async def _payments_agent(client: Goa, me_id: UUID, ready: asyncio.Event) -> None:
    async with client.stream() as frames:
        ready.set()
        async for frame in frames:
            if frame.event_name != "event" or frame.event is None:
                continue
            ev = frame.event
            if not isinstance(ev, QuestionEvent) or me_id not in ev.payload.to:
                continue
            assert frame.task_id is not None
            await client.append_event(
                frame.task_id,
                OutboundAnswer(
                    payload=AnswerPayload(answering=[ev.id]),
                    content=Content(text=f"refund issued for: {ev.content.text}"),
                ),
            )


async def _support_agent(
    client: Goa, me_id: UUID, payments_id: UUID, ready: asyncio.Event,
) -> None:
    parent_for_child: dict[UUID, UUID] = {}
    pending_parent_question: dict[UUID, UUID] = {}

    async with client.stream() as frames:
        ready.set()
        async for frame in frames:
            if frame.event_name != "event" or frame.event is None:
                continue
            ev = frame.event
            task_id = frame.task_id
            assert task_id is not None

            if isinstance(ev, QuestionEvent) and me_id in ev.payload.to:
                text = (ev.content.text or "").lower()
                if PAYMENT_KEYWORD in text:
                    sub_task, _sub_q = await client.start_task(
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
                    await client.append_event(
                        task_id,
                        OutboundAnswer(
                            payload=AnswerPayload(answering=[ev.id]),
                            content=Content(text=f"support reply: {ev.content.text}"),
                        ),
                    )
                continue

            if (
                isinstance(ev, AnswerEvent)
                and task_id in parent_for_child
            ):
                parent_id = parent_for_child.pop(task_id)
                parent_q = pending_parent_question.pop(task_id, None)
                if parent_q is None:
                    continue
                await client.append_event(
                    parent_id,
                    OutboundAnswer(
                        payload=AnswerPayload(answering=[parent_q]),
                        content=Content(text=f"per payments: {ev.content.text}"),
                    ),
                )


# ---------------------------------------------------------------------------
# Golden scenario
# ---------------------------------------------------------------------------

async def _await_subscriber(hub, participant_id: UUID, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if hub.has_subscriber(participant_id):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"subscriber for {participant_id} never registered")


async def test_golden_e2e_chat_support_payments() -> None:
    app = create_app(Settings.for_tests())
    hub = app.state.ctx.hub

    async with live_server(app) as base_url:
        # Register the three participants. capabilities drive discovery.
        chat_client, _, chat = await Goa.register_participant(
            base_url, type="service", name="chat-service",
            description="customer chat front",
            capabilities=["chat"],
        )
        support_client, _, support = await Goa.register_participant(
            base_url, type="agent", name="support-agent",
            description="customer support; consults payments",
            capabilities=["support"],
        )
        payments_client, _, payments = await Goa.register_participant(
            base_url, type="agent", name="payments-agent",
            description="processes refunds",
            capabilities=["payments"],
        )

        try:
            payments_ready = asyncio.Event()
            support_ready = asyncio.Event()

            async with (
                _running(_payments_agent(payments_client, payments.id, payments_ready)),
                _running(
                    _support_agent(support_client, support.id, payments.id, support_ready),
                ),
            ):
                # Wait for both agent streams to be live before kicking off the
                # customer flow so the question/answer exchange is observable.
                await asyncio.wait_for(payments_ready.wait(), timeout=5.0)
                await asyncio.wait_for(support_ready.wait(), timeout=5.0)
                await _await_subscriber(hub, payments.id)
                await _await_subscriber(hub, support.id)

                # Chat-service stream — the customer's view. Open it before
                # upserting so we observe events in order.
                async with chat_client.stream() as chat_frames:
                    await _await_subscriber(hub, chat.id)

                    # 4. Upsert is stateless service-side: first call creates;
                    # the second with the same external_ref returns the same task.
                    first_task, first_created, _first_event = (
                        await chat_client.upsert_and_send(
                            external_ref="slack-thread-demo",
                            event=OutboundQuestion(
                                payload=QuestionPayload(to=[support.id]),
                                content=Content(
                                    text="hi, can I get a refund for order #42?"
                                ),
                            ),
                            subject="thread slack-thread-demo",
                        )
                    )
                    assert first_created is True

                    again_task, again_created = await chat_client.upsert_task(
                        external_ref="slack-thread-demo",
                    )
                    assert again_created is False
                    assert again_task.id == first_task.id

                    # Drain the chat stream until the customer-facing answer
                    # arrives. Track the sub-task id that the parent observed
                    # via `child_task_created` (3, below) along the way.
                    customer_answer_text: str | None = None
                    saw_child_event = False
                    async def _drain_chat(timeout: float = 10.0) -> None:
                        nonlocal customer_answer_text, saw_child_event
                        deadline = asyncio.get_running_loop().time() + timeout
                        async for frame in chat_frames:
                            if asyncio.get_running_loop().time() > deadline:
                                raise AssertionError("chat drain timed out")
                            if frame.event_name != "event" or frame.event is None:
                                continue
                            assert frame.task_id == first_task.id, (
                                "chat-service must NEVER receive frames for the sub-task"
                            )
                            if isinstance(frame.event, ChildTaskCreatedEvent):
                                saw_child_event = True
                                assert frame.event.payload.spawned_by == support.id
                                assert frame.event.payload.subject == "payments consult"
                            if (
                                isinstance(frame.event, AnswerEvent)
                                and frame.task_id == first_task.id
                            ):
                                customer_answer_text = frame.event.content.text
                                return
                    await _drain_chat()

                # 2. child_task_created landed in the parent.
                assert saw_child_event, "expected child_task_created in the parent task"

                # The customer answer carries the payments answer transitively.
                assert customer_answer_text is not None
                assert "refund" in customer_answer_text.lower()

                # 3. Pending drains on both parent and child.
                parent = await chat_client.get_task(first_task.id)
                assert parent.pending_questions == []

                # Find the child id by looking at the parent's events.
                child_id: UUID | None = None
                for ev in parent.events:
                    if isinstance(ev, ChildTaskCreatedEvent):
                        child_id = ev.payload.task_id
                        break
                assert child_id is not None

                # 1. Visibility — chat service cannot read the sub-task.
                with pytest.raises(GoaSdkError) as excinfo:
                    await chat_client.get_task(child_id)
                assert excinfo.value.http_status == 404

                # 1. Visibility — payments cannot read the parent.
                with pytest.raises(GoaSdkError) as excinfo:
                    await payments_client.get_task(first_task.id)
                assert excinfo.value.http_status == 404

                # The support agent IS in both — pending on child also drains.
                child = await support_client.get_task(child_id)
                assert child.pending_questions == []
        finally:
            for c in (chat_client, support_client, payments_client):
                await c.aclose()
