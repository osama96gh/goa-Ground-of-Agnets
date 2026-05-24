"""SDK tests for discovery (`search_participants`, `get_participant`),
expanded `list_tasks`, and `pending`."""

from __future__ import annotations

import pytest

from goa.config import Settings
from goa.main import create_app

from goa_sdk import Goa, GoaSdkError, OutboundAnswer, OutboundQuestion
from goa_sdk.events import AnswerPayload, Content, QuestionPayload

from tests._live_server import live_server


pytestmark = pytest.mark.asyncio


async def test_search_participants_anded_capabilities() -> None:
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        legal, _, legal_p = await Goa.register_participant(
            base_url,
            type="agent", name="legal-summarizer",
            description="legal contracts",
            capabilities=["summarize", "legal"],
        )
        bullet, _, bullet_p = await Goa.register_participant(
            base_url,
            type="agent", name="bullet-summarizer",
            description="general",
            capabilities=["summarize"],
        )
        chat, _, chat_p = await Goa.register_participant(
            base_url,
            type="service", name="chat",
            description="chat bridge",
            capabilities=["chat"],
        )
        try:
            both = await legal.search_participants(capability=["summarize", "legal"])
            assert [p.id for p in both] == [legal_p.id]

            single = await legal.search_participants(capability=["summarize"])
            assert {p.id for p in single} == {legal_p.id, bullet_p.id}

            services = await legal.search_participants(type="service")
            assert [p.id for p in services] == [chat_p.id]

            by_q = await legal.search_participants(q="contracts")
            assert [p.id for p in by_q] == [legal_p.id]

            fetched = await legal.get_participant(chat_p.id)
            assert fetched.id == chat_p.id
            assert fetched.capabilities == ["chat"]

            with pytest.raises(GoaSdkError) as excinfo:
                await legal.get_participant(legal_p.id.__class__(int=0))
            assert excinfo.value.http_status == 404
        finally:
            for c in (legal, bullet, chat):
                await c.aclose()


async def test_list_tasks_and_pending_round_trip() -> None:
    app = create_app(Settings.for_tests())
    async with live_server(app) as base_url:
        init, _, init_p = await Goa.register_participant(
            base_url, type="agent", name="init",
        )
        target, _, target_p = await Goa.register_participant(
            base_url, type="agent", name="target",
        )
        try:
            # Two pending tasks targeting `target`.
            t1, q1 = await init.start_task(
                first_event=OutboundQuestion(
                    payload=QuestionPayload(to=[target_p.id]),
                    content=Content(text="q1"),
                ),
                subject="t1",
            )
            t2, q2 = await init.start_task(
                first_event=OutboundQuestion(
                    payload=QuestionPayload(to=[target_p.id]),
                    content=Content(text="q2"),
                ),
                subject="t2",
            )

            # init: role=initiator returns both.
            init_tasks = await init.list_tasks(role="initiator")
            assert {item.task.id for item in init_tasks} == {t1.id, t2.id}

            # target: pending() returns both pairs.
            pending = await target.pending()
            assert {(p.task_id, p.question_event_id) for p in pending} == {
                (t1.id, q1.id),
                (t2.id, q2.id),
            }
            for row in pending:
                assert row.from_ == init_p.id

            # target: list_tasks(role="member", has_pending=True) returns both.
            member_tasks = await target.list_tasks(role="member", has_pending=True)
            assert {item.task.id for item in member_tasks} == {t1.id, t2.id}

            # Answer t1 → target's pending shrinks.
            await target.append_event(
                t1.id,
                OutboundAnswer(
                    payload=AnswerPayload(answering=[q1.id]),
                    content=Content(text="a1"),
                ),
            )
            pending = await target.pending()
            assert [(p.task_id, p.question_event_id) for p in pending] == [
                (t2.id, q2.id),
            ]
        finally:
            await init.aclose()
            await target.aclose()
