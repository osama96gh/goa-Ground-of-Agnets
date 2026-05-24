"""Attachment demo (spec §6.5).

Two participants: `reporter` (initiator) opens a task with a small PNG
attached to its question. `analyst` listens on its stream, sees the
question, downloads the attachment, and answers with an annotated image
of its own.

This is the smallest end-to-end use of `Goa.upload_blob` /
`Content.attachments` / `Goa.download_blob` against a live hub.
"""

from __future__ import annotations

import argparse
import asyncio
import struct
import sys
import zlib
from pathlib import Path

# Allow `python examples/attachment-demo/main.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from goa_sdk import Goa, OutboundAnswer, OutboundQuestion
from goa_sdk.events import AnswerPayload, Content, QuestionEvent, QuestionPayload

from _shared import base_url_arg


def _make_gradient_png(
    color_top: tuple[int, int, int],
    color_bottom: tuple[int, int, int],
    *,
    width: int = 256,
    height: int = 256,
) -> bytes:
    """Build a vertical gradient PNG so the demo produces real, visible images
    without a file-system dependency. RGB, 8-bit, no alpha — small enough to
    stay under any reasonable upload limit."""
    rows: list[bytes] = []
    last = max(height - 1, 1)
    for y in range(height):
        t = y / last
        r = int(color_top[0] * (1 - t) + color_bottom[0] * t)
        g = int(color_top[1] * (1 - t) + color_bottom[1] * t)
        b = int(color_top[2] * (1 - t) + color_bottom[2] * t)
        # PNG scanline = filter-type byte (0 = None) + width*3 RGB bytes.
        rows.append(b"\x00" + bytes((r, g, b)) * width)
    raw = b"".join(rows)
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _sample_png() -> bytes:
    # Crimson → indigo. Reporter's "input" image.
    return _make_gradient_png((220, 30, 60), (40, 0, 120))


def _annotated_png() -> bytes:
    # Forest green → gold. Analyst's "output" image.
    return _make_gradient_png((20, 130, 50), (240, 200, 30))


async def run_analyst(base_url: str) -> None:
    client, _key, me = await Goa.register_participant(
        base_url, type="agent", name="analyst", description="annotates uploaded images",
    )
    print(f"[analyst] registered as {me.id}")
    try:
        async with client.stream() as frames:
            async for frame in frames:
                if frame.event_name != "event" or frame.event is None:
                    continue
                if not isinstance(frame.event, QuestionEvent):
                    continue
                if me.id not in frame.event.payload.to:
                    continue
                attachments = frame.event.content.attachments
                print(
                    f"[analyst] question {frame.event.id} on task {frame.task_id} "
                    f"with {len(attachments)} attachment(s)",
                )
                for att in attachments:
                    blob = await client.download_blob(att.blob_id)
                    print(
                        f"[analyst]   downloaded {att.filename} "
                        f"({att.mime_type}, {len(blob)} bytes, sha256={att.sha256[:12]}…)",
                    )
                # Answer with an annotated image of our own.
                annotated = await client.upload_blob(
                    _annotated_png(),
                    filename="annotated.png",
                    mime_type="image/png",
                )
                await client.append_event(
                    frame.task_id,
                    OutboundAnswer(
                        payload=AnswerPayload(answering=[frame.event.id]),
                        content=Content(text="here you go", attachments=[annotated]),
                    ),
                )
    finally:
        await client.aclose()


async def run_reporter(base_url: str, target_id: str) -> None:
    from uuid import UUID

    client, _key, me = await Goa.register_participant(
        base_url, type="agent", name="reporter",
    )
    print(f"[reporter] registered as {me.id}")
    try:
        att = await client.upload_blob(
            _sample_png(),
            filename="sample.png",
            mime_type="image/png",
        )
        print(f"[reporter] uploaded {att.filename} ({att.size_bytes} bytes) → {att.blob_id}")
        task, question = await client.start_task(
            subject="please annotate this image",
            first_event=OutboundQuestion(
                payload=QuestionPayload(to=[UUID(target_id)]),
                content=Content(text="annotate this please", attachments=[att]),
            ),
        )
        print(f"[reporter] created task {task.id} with question {question.id}")
        # Wait for the answer
        async with client.stream() as frames:
            async for frame in frames:
                if frame.event_name != "event" or frame.event is None:
                    continue
                if frame.event.event_type != "answer":
                    continue
                ans_attachments = frame.event.content.attachments
                print(
                    f"[reporter] got answer {frame.event.id} with "
                    f"{len(ans_attachments)} attachment(s); text={frame.event.content.text!r}",
                )
                for att in ans_attachments:
                    blob = await client.download_blob(att.blob_id)
                    print(
                        f"[reporter]   downloaded {att.filename} "
                        f"({att.mime_type}, {len(blob)} bytes)",
                    )
                return
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Goa v2 example — attachment demo")
    base_url_arg(parser)
    parser.add_argument(
        "role", choices=("analyst", "reporter"),
        help="run as the analyst (waits for questions) or reporter (sends one)",
    )
    parser.add_argument(
        "--target-id",
        help="(reporter only) participant id of the analyst to question",
    )
    args = parser.parse_args()
    if args.role == "analyst":
        asyncio.run(run_analyst(args.base_url))
    else:
        if not args.target_id:
            parser.error("--target-id is required for the reporter role")
        asyncio.run(run_reporter(args.base_url, args.target_id))


if __name__ == "__main__":
    main()
