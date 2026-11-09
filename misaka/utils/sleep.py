"""Translated from pi's ``packages/coding-agent/src/utils/sleep.ts`` (v1.1.0).

MISAKA fork: signals are duck-typed (see ``misaka.utils._abort_signal``); the listener goes through
``misaka.utils.js.addAbortListener``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from misaka.utils.js import addAbortListener
from misaka.utils.values import signal_aborted


async def sleep(ms: float, signal: Any = None) -> None:
    """Sleep helper that respects abort signal."""
    if signal_aborted(signal):
        raise RuntimeError("Aborted")
    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()
    timeout = loop.call_later(max(0.0, ms) / 1000, lambda: None if done.done() else done.set_result(None))

    def onAbort() -> None:
        timeout.cancel()
        if not done.done():
            done.set_exception(RuntimeError("Aborted"))

    remove = addAbortListener(signal, onAbort) if signal is not None else (lambda: None)
    try:
        await done
    finally:
        timeout.cancel()
        remove()


__all__ = ["sleep"]
