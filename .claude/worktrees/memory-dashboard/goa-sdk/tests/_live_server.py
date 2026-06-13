"""Same uvicorn fixture as goa-core/tests — duplicated here so the SDK test
suite has no path-based dependency on goa-core's tests/ tree."""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

import uvicorn
from fastapi import FastAPI


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


@asynccontextmanager
async def live_server(app: FastAPI) -> AsyncIterator[str]:
    port = _free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="error",
        # Drive FastAPI's lifespan so the Persistence bundle is entered
        # (SQLite opens its connection here) — matches production.
        lifespan="on",
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())

    deadline = asyncio.get_running_loop().time() + 5.0
    while not server.started:
        if asyncio.get_running_loop().time() > deadline:
            raise RuntimeError("uvicorn did not start within 5s")
        await asyncio.sleep(0.01)

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, BaseException):
                pass
