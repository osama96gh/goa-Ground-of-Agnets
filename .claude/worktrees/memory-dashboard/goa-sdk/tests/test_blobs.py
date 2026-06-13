"""SDK-level coverage for the §6.5 blob/attachment surface."""

from __future__ import annotations

import asyncio
import hashlib
from uuid import UUID

import pytest

from goa.config import Settings
from goa.main import create_app
from goa.stream.hub import InMemoryStreamHub

from goa_sdk import Goa, OutboundAnswer, OutboundQuestion
from goa_sdk.events import AnswerPayload, Content, QuestionEvent, QuestionPayload

from tests._live_server import live_server


pytestmark = pytest.mark.asyncio


async def _wait_for_subscriber(hub: InMemoryStreamHub, pid: UUID, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if hub.has_subscriber(pid):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"subscriber for {pid} never registered")


async def _next_question(it) -> QuestionEvent:
    while True:
        frame = await asyncio.wait_for(it.__anext__(), timeout=5.0)
        if frame.event_name != "event" or frame.event is None:
            continue
        if isinstance(frame.event, QuestionEvent):
            return frame.event


async def test_upload_blob_round_trip_with_event() -> None:
    app = create_app(Settings.for_tests(blob_max_bytes=4 * 1024 * 1024))
    hub: InMemoryStreamHub = app.state.ctx.hub

    async with live_server(app) as base_url:
        alice, _ak, alice_p = await Goa.register_participant(
            base_url, type="agent", name="alice",
        )
        bob, _bk, bob_p = await Goa.register_participant(
            base_url, type="agent", name="bob",
        )

        try:
            payload = b"hello attachments" * 200  # ~3.4 KB

            # Bob streams; alice creates a task, uploads a blob bound to it,
            # then appends the question that references the attachment.
            async with bob.stream() as bob_stream:
                bob_iter = bob_stream.__aiter__()
                await _wait_for_subscriber(hub, bob_p.id)

                task = await alice.create_task(subject="check this")
                attachment = await alice.upload_blob(
                    task.id, payload, filename="note.txt", mime_type="text/plain",
                )
                assert attachment.filename == "note.txt"
                assert attachment.size_bytes == len(payload)
                assert attachment.sha256 == hashlib.sha256(payload).hexdigest()

                q_event = await alice.append_event(
                    task.id,
                    OutboundQuestion(
                        payload=QuestionPayload(to=[bob_p.id]),
                        content=Content(
                            text="see attached", attachments=[attachment],
                        ),
                    ),
                )
                question = await _next_question(bob_iter)
                assert question.id == q_event.id
                assert len(question.content.attachments) == 1
                ref = question.content.attachments[0]
                assert ref.blob_id == attachment.blob_id

                # Bob downloads — bytes match exactly.
                downloaded = await bob.download_blob(ref.blob_id)
                assert downloaded == payload

                # Meta endpoint round-trips.
                meta = await bob.get_blob_meta(ref.blob_id)
                assert meta.sha256 == attachment.sha256
        finally:
            await alice.aclose()
            await bob.aclose()
