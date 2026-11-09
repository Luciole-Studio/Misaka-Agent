"""Translated from pi's ``packages/ai/src/utils/sleep.ts`` (v1.1.0).

MISAKA fork: signals are duck-typed (see ``misaka.utils._abort_signal``): ``throwIfAborted`` and the listener go
through ``misaka.utils.js``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from misaka.ai.utils.abort import _abortReason
from misaka.utils.js import addAbortListener, throwIfAborted


async def sleep(ms: float, signal: Any) -> None:
    throwIfAborted(signal)
    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()

    def onAbort() -> None:
        timeout.cancel()
        if not done.done():
            done.set_exception(_abortReason(signal))

    def finish() -> None:
        remove()
        if not done.done():
            done.set_result(None)

    timeout = loop.call_later(max(0.0, ms) / 1000, finish)
    remove = addAbortListener(signal, onAbort)
    try:
        await done
    finally:
        timeout.cancel()
        remove()


__all__ = ["sleep"]
