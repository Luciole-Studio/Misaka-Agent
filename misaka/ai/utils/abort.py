"""Translated from pi's ``packages/ai/src/utils/abort.ts`` (v1.1.0).

MISAKA fork: signals are duck-typed (see ``misaka.utils._abort_signal``); listeners go through
``misaka.utils.js.addAbortListener``. The operation is an awaitable started as a task; when the caller awaiting the
race is cancelled, that task is cancelled too (a promise cannot be, and ``asyncio.shield`` keeps a shared operation
alive), and an abandoned task is still observed so its late failure is never reported as unretrieved.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from misaka.utils.js import AbortController as _DomAbortController
from misaka.utils.js import DOMException, addAbortListener, rejectedPromise, startTask
from misaka.utils.values import signal_aborted


def _abortReason(signal: Any) -> BaseException:
    reason = getattr(signal, "reason", None)
    if reason is not None:
        # MISAKA fork: only an exception can be raised; another reason value is carried by a DOMException.
        return reason if isinstance(reason, BaseException) else DOMException(str(reason), "AbortError")
    error = DOMException("The operation was aborted", "AbortError")
    return error


def operationSignal(signal: Any = None) -> Any:
    """Create an operation-local signal for public APIs whose signal is optional."""
    return signal if signal is not None else _DomAbortController().signal


def _observe(task: asyncio.Future[Any]) -> None:
    task.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)


def raceWithAbortSignal[T](operation: Awaitable[T], signal: Any) -> asyncio.Future[T]:
    """Stop waiting for an operation when its signal aborts while continuing to observe the abandoned promise so a
    later rejection is always handled."""
    task = startTask(operation)
    if signal_aborted(signal):
        _observe(task)
        return rejectedPromise(_abortReason(signal))

    result: asyncio.Future[T] = asyncio.get_running_loop().create_future()
    settled = False

    def cleanup() -> None:
        remove()

    def onAbort() -> None:
        nonlocal settled
        if settled:
            return
        settled = True
        cleanup()
        _observe(task)
        if not result.done():
            result.set_exception(_abortReason(signal))

    remove = addAbortListener(signal, onAbort)

    def onSettled(finished: asyncio.Future[T]) -> None:
        nonlocal settled
        if settled:
            return
        settled = True
        cleanup()
        if result.done():
            return
        if finished.cancelled():
            result.cancel()
        elif finished.exception() is not None:
            result.set_exception(finished.exception())  # type: ignore[arg-type]
        else:
            result.set_result(finished.result())

    task.add_done_callback(onSettled)

    def onCallerDone(future: asyncio.Future[T]) -> None:
        # MISAKA fork: the caller stopped waiting (it was cancelled); cancel the operation it started.
        if future.cancelled() and not task.done():
            task.cancel()
            _observe(task)

    result.add_done_callback(onCallerDone)
    if signal_aborted(signal):
        onAbort()
    return result


__all__ = ["operationSignal", "raceWithAbortSignal"]
