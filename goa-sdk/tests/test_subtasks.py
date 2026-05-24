"""SDK sub-task tests: `parent_task_id` on create_task, `list_children`,
and `child_task_created` decoded as a typed event on the stream."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from goa.config import Settings
from goa.main import create_app
from goa.stream.hub import InMemoryStreamHub

from goa_sdk import (
    ChildTaskCreatedEvent,
    Goa,
    OutboundQuestion,
    StreamFrame,
)
from goa_sdk.events import Content, QuestionPayload

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
        if frame.event_name != "event":
            continue
        if frame.event is not None and frame.event.event_type == event_type:
            return frame


async def test_sdk_create_subtask_and_decode_child_task_created() -> None:
    app = create_app(Settings.for_tests())
    hub: InMemoryStreamHub = app.state.ctx.hub

    async with live_server(app) as base_url:
        s_client, _, s = await Goa.register_participant(
            base_url, type="service", name="chat"
        )
        c_client, _, c = await Goa.register_participant(
            base_url, type="agent", name="support"
        )
        p_client, _, p = await Goa.register_participant(
            base_url, type="agent", name="payments"
        )

        try:
            async with s_client.stream() as s_frames:
                await _wait_for_subscriber(hub, s.id)
                async with c_client.stream() as c_frames:
                    await _wait_for_subscriber(hub, c.id)
                    async with p_client.stream() as p_frames:
                        await _wait_for_subscriber(hub, p.id)

                        # S opens T1 to C.
                        t1, _t1_q = await s_client.start_task(
                            first_event=OutboundQuestion(
                                payload=QuestionPayload(to=[c.id]),
                                content=Content(text="refund?"),
                            ),
                            subject="refund inquiry",
                        )

                        # C sees T1's question.
                        await _drain_until(c_frames, "question")

                        # C spawns T2 with parent_task_id=T1.
                        t2, _t2_q = await c_client.start_task(
                            first_event=OutboundQuestion(
                                payload=QuestionPayload(to=[p.id]),
                                content=Content(text="any record?"),
                            ),
                            subject="payments lookup",
                            parent_task_id=t1.id,
                        )

                        # S sees `child_task_created` typed correctly.
                        child_frame = await _drain_until(s_frames, "child_task_created")
                        assert child_frame.event is not None
                        assert isinstance(child_frame.event, ChildTaskCreatedEvent)
                        assert child_frame.event.from_ is None
                        assert child_frame.event.payload.task_id == t2.id
                        assert child_frame.event.payload.spawned_by == c.id
                        assert child_frame.event.payload.subject == "payments lookup"
                        # T1 itself is a root task — its summary parent is null.
                        assert child_frame.task is not None
                        assert child_frame.task.parent_task_id is None

                        # P sees T2's own question (drains its participant_joined).
                        p_question = await _drain_until(p_frames, "question")
                        assert p_question.task_id == t2.id

                        # C reads T2 → parent_task_id is set on the SDK Task model.
                        c_t2 = await c_client.get_task(t2.id)
                        assert c_t2.task.parent_task_id == t1.id

                        # list_children: C is in T2; S is not.
                        c_children = await c_client.list_children(t1.id)
                        assert [item.task.id for item in c_children] == [t2.id]

                        s_children = await s_client.list_children(t1.id)
                        assert s_children == []
        finally:
            await s_client.aclose()
            await c_client.aclose()
            await p_client.aclose()
