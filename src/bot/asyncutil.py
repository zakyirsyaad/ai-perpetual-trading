"""Small asyncio helpers shared by the trading core and the telemetry feed."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


async def with_timeout(awaitable: Awaitable[T], timeout: float) -> T:
    """`asyncio.wait_for` that never swallows a cancellation.

    Before Python 3.12, `wait_for` returns the finished result and drops the caller's
    cancellation when the work completes in the same loop tick. A feed that is being stopped
    would then keep running forever, so this waits with `asyncio.wait` instead and cancels
    whatever is left unfinished. Raises `asyncio.TimeoutError` on timeout.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            raise asyncio.TimeoutError()
        return task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
