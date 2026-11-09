"""MISAKA fork: the flag-style abort signals misaka passes around, where pi passes DOM ``AbortSignal``s.

pi composes ``AbortSignal``s. misaka also passes duck-typed objects around: anything with an ``aborted`` flag counts,
and anything that also has an awaitable ``wait()`` can be waited on. These helpers work on that shape, so a caller can
hand in an ``asyncio.Event``-backed controller, a provider's own signal object, ``misaka.utils.js.AbortSignal``, or
``None``. They have no upstream counterpart; pi's own helpers are ported line by line in ``misaka.ai.utils.abort``,
``abort_signals`` and ``sleep`` (pi-ai) and in ``misaka.utils.abort`` and ``sleep`` (coding agent).

This module lives outside ``misaka.ai`` because ``misaka.utils.js`` needs ``wait_for_abort``, and importing anything
under ``misaka.ai`` runs ``misaka/ai/__init__.py``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from typing import Any

from misaka.utils.values import signal_aborted

# How often a flag-only signal is re-read. Short enough that a person pressing Esc does not notice, cheap enough that
# an idle wait costs nothing measurable.
FLAG_POLL_INTERVAL_S = 0.05


async def wait_for_abort(signal: Any) -> None:
    """Resolve when ``signal`` aborts, whichever shape of signal it is.

    Two shapes reach this module. The controllers here and ``misaka.utils.js.AbortSignal`` expose ``wait()``; a
    duck-typed signal may carry only an ``aborted`` flag. If a signal without ``wait()`` were parked here forever,
    every helper that waits on a signal would degrade into a plain await for exactly that shape, and pressing Esc
    would do nothing until the request finished on its own; polling the flag is what avoids that.
    """
    if signal is None:
        await asyncio.Event().wait()
        return
    waiter = getattr(signal, "wait", None)
    if callable(waiter):
        await waiter()
        return
    while not signal_aborted(signal):
        await asyncio.sleep(FLAG_POLL_INTERVAL_S)


class AbortController:
    """A signal misaka's helpers accept: an ``aborted`` flag plus an awaitable ``wait()``; the controller is its own
    signal."""

    def __init__(self) -> None:
        self.aborted = False
        self._event = asyncio.Event()

    def abort(self) -> None:
        self.aborted = True
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


class AnyAbortSignal:
    """``AbortSignal.any(signals)`` as a view over the inputs: aborted as soon as any of them is, with the reason of the
    first that aborted. Nothing is subscribed, so there is nothing to clean up."""

    def __init__(self, *signals: Any) -> None:
        self._signals = [signal for signal in signals if signal is not None]

    @property
    def aborted(self) -> bool:
        return any(signal_aborted(signal) for signal in self._signals)

    @property
    def reason(self) -> Any:
        """The reason of the first input that aborted, as ``AbortSignal.any`` reports it."""
        return next((getattr(signal, "reason", None) for signal in self._signals if signal_aborted(signal)), None)

    async def wait(self) -> None:
        if not self._signals:
            # Nothing can tell us about an abort, so waiting forever is honest; reporting one immediately would abort
            # work that was never cancelled.
            await asyncio.Event().wait()
            return
        tasks = [asyncio.ensure_future(wait_for_abort(signal)) for signal in self._signals]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()


class TimeoutAbortSignal:
    """``AbortSignal.timeout(ms)``: aborts by itself once ``ms`` have passed, with a ``TimeoutError`` as its reason.
    Deadline based, so it works without a running event loop."""

    def __init__(self, ms: float) -> None:
        self._deadline = time.monotonic() + max(0.0, ms) / 1000
        self.reason = TimeoutError("The operation was aborted due to timeout")

    @property
    def aborted(self) -> bool:
        return time.monotonic() >= self._deadline

    async def wait(self) -> None:
        await asyncio.sleep(max(0.0, self._deadline - time.monotonic()))


def abort_reason(signal: Any) -> BaseException:
    """What a fetch whose ``signal`` aborted rejects with: its ``reason`` when it carries one, else the error misaka's
    request path raises for an abort."""
    reason = getattr(signal, "reason", None)
    return reason if isinstance(reason, BaseException) else RuntimeError("Request was aborted")


def throw_if_aborted(signal: Any) -> None:
    """Raise what misaka's request path raises for an abort once ``signal`` has aborted."""
    if signal_aborted(signal):
        raise RuntimeError("Request was aborted")


async def cancel_on_abort[T](operation: Awaitable[T], signal: Any) -> T:
    """Await ``operation`` under ``signal``; an abort cancels it and raises ``abort_reason(signal)``, as fetch tears
    down a request whose signal aborts. Use it for work nobody else shares."""
    task = asyncio.ensure_future(operation)
    try:
        if signal_aborted(signal):
            raise abort_reason(signal)
        if signal is None:
            return await task
        aborting = asyncio.ensure_future(wait_for_abort(signal))
        try:
            await asyncio.wait({task, aborting}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            aborting.cancel()
        if task.done() and not signal_aborted(signal):
            return task.result()
        raise abort_reason(signal)
    finally:
        task.cancel()


__all__ = [
    "FLAG_POLL_INTERVAL_S",
    "AbortController",
    "AnyAbortSignal",
    "TimeoutAbortSignal",
    "abort_reason",
    "cancel_on_abort",
    "throw_if_aborted",
    "wait_for_abort",
]
