"""Work that runs after a request has been answered.

Publishing to other relays, for example, can take seconds per relay and
must not hold up the homeserver's transaction. Tasks are tracked on
``app.state.background_tasks`` so they aren't garbage-collected mid-run
and are cancelled cleanly on shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine

from fastapi import FastAPI

logger = logging.getLogger(__name__)


def run_in_background(app: FastAPI, coroutine: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
    tasks: set[asyncio.Task[Any]] = app.state.background_tasks
    task = asyncio.create_task(coroutine, name=name)
    tasks.add(task)

    def done(finished: asyncio.Task[Any]) -> None:
        tasks.discard(finished)
        if not finished.cancelled() and finished.exception() is not None:
            logger.error("Background task %s failed", finished.get_name(), exc_info=finished.exception())

    task.add_done_callback(done)
    return task


async def drain(app: FastAPI) -> None:
    """Wait for every background task to finish (used by tests)."""
    while app.state.background_tasks:
        await asyncio.gather(*list(app.state.background_tasks), return_exceptions=True)


async def cancel_all(app: FastAPI) -> None:
    for task in list(app.state.background_tasks):
        task.cancel()
    await asyncio.gather(*list(app.state.background_tasks), return_exceptions=True)
