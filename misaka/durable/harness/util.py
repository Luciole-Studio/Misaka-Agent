"""Small helpers shared by the Harness modules.

Translated from pi's ``packages/durable/src/harness/util.ts``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from misaka.chord._promise import isRejection
from misaka.chord.context import Context, abortError
from misaka.utils.js import DOMException, rejectedPromise

from ..errors import SessionFailed
from ..types import Cursor, Page


class Waiters:
    """Pending waits by key. Each settles once: through ``resolve``, ``rejectAll``, or cancellation of its context."""

    def __init__(self) -> None:
        self._sets: dict[Any, dict[asyncio.Future[Any], None]] = {}

    def add(self, key: Any, context: Context) -> asyncio.Future[Any]:
        signal = context.abortSignal
        if signal is not None and signal.aborted:
            return rejectedPromise(abortError(signal))
        waiter: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        waiters = self._sets.get(key)
        if waiters is None:
            waiters = {}
            self._sets[key] = waiters
        waiters[waiter] = None

        def forget() -> None:
            waiters.pop(waiter, None)
            if len(waiters) == 0 and self._sets.get(key) is waiters:
                del self._sets[key]

        def onAbort() -> None:
            forget()
            if not waiter.done():
                waiter.set_exception(abortError(signal))

        if signal is not None:
            signal.addEventListener("abort", onAbort, once=True)

        def settled(done: asyncio.Future[Any]) -> None:
            if signal is not None:
                signal.removeEventListener("abort", onAbort)
            # MISAKA fork: an awaiter can stop waiting with asyncio cancellation (``wait_for``, a task group), which
            # cancels the future; upstream stops a wait only by aborting its context. A cancelled waiter leaves its set
            # as an aborted one does, so polling a long-lived task with timeouts holds nothing.
            if done.cancelled():
                forget()

        waiter.add_done_callback(settled)
        return waiter

    def keys(self) -> list[Any]:
        return list(self._sets.keys())

    def resolve(self, key: Any, value: Any) -> None:
        waiters = self._sets.pop(key, None)
        for waiter in list(waiters or ()):
            if not waiter.done():
                waiter.set_result(value)

    def rejectAll(self, error: BaseException) -> None:
        sets = list(self._sets.values())
        self._sets.clear()
        for waiters in sets:
            for waiter in list(waiters):
                if not waiter.done():
                    waiter.set_exception(error)


class LineWait:
    """A wait registered by a job on the Session line (``waitForTask``, ``SubmissionHandle.wait``). MISAKA fork: a
    caller can stop waiting with asyncio cancellation while the job is still queued on the shielded line, and the job
    then registers a waiter nobody awaits; upstream stops a wait only by aborting its context, which ``Waiters.add``
    sees. ``abandon()`` lets go of the waiter, registered already or later, so it leaves its set (``Waiters.add``)."""

    __slots__ = ("abandoned", "waiter")

    def __init__(self) -> None:
        self.abandoned = False
        self.waiter: asyncio.Future[Any] | None = None

    def register(self, waiter: asyncio.Future[Any]) -> asyncio.Future[Any]:
        self.waiter = waiter
        if self.abandoned:
            waiter.cancel()
        return waiter

    def abandon(self) -> None:
        self.abandoned = True
        if self.waiter is not None:
            self.waiter.cancel()


async def scanAll(scan: Callable[[Cursor | None], Awaitable[Page]]) -> list[Any]:
    """Every item of a paginated scan, in page order."""
    items: list[Any] = []
    cursor: Cursor | None = None
    while True:
        page = await scan(cursor)
        items.extend(page["items"])
        cursor = page.get("next")
        if cursor is None:
            break
    return items


def thrownBy(error: BaseException) -> Exception:
    """What a handler threw. A handler that ends with ``CancelledError`` because something it awaited was cancelled has
    thrown, as awaiting a rejected promise does (an ``AbortError``); cancelling the task that runs it is not a failure
    and propagates."""
    if isinstance(error, Exception):
        return error
    if isRejection(error):
        return DOMException("The operation was aborted", "AbortError")
    raise error


def asThrown(error: BaseException) -> Exception:
    """``error`` as the exception a promise rejects with: an ``Exception`` as it is, anything else (a cancellation, an
    interrupt) as an ``AbortError``, so whoever awaits the rejected promise is not itself cancelled by it."""
    return error if isinstance(error, Exception) else DOMException("The operation was aborted", "AbortError")


def closedError(session: Any) -> Exception:
    """What a closed Harness rejects with: ``SessionFailed`` with the cause when it closed because the Session failed."""
    failure = session.failure
    return RuntimeError("Harness is closed") if failure is None else SessionFailed(failure["error"])


__all__ = ["LineWait", "Waiters", "asThrown", "closedError", "scanAll", "thrownBy"]
