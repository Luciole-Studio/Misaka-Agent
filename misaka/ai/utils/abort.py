"""Cancellation primitives, translated from pi's ``utils/abort.ts``,
``abort-signals.ts`` and ``sleep.ts``.

pi composes ``AbortSignal``s. misaka passes duck-typed objects around instead: anything
with an ``aborted`` flag counts, and anything that also has an awaitable ``wait()`` can be
waited on. These helpers work on that shape, so a caller can hand in an
``asyncio.Event``-backed controller, a provider's own signal object, or ``None``.

The one behaviour worth keeping deliberately is in ``race_with_abort_signal``: the
abandoned operation is still observed after the race is lost. Dropping it would surface
later as a "Task exception was never retrieved" warning from a failure nobody is waiting
for any more.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from misaka.utils.values import signal_aborted

# How often a flag-only signal is re-read. Short enough that a person pressing Esc does
# not notice, cheap enough that an idle wait costs nothing measurable.
FLAG_POLL_INTERVAL_S = 0.05


async def wait_for_abort(signal: Any) -> None:
    """Resolve when ``signal`` aborts, whichever shape of signal it is.

    Two shapes reach this package. The controllers here expose ``wait()``; misaka's own
    ``ui/tui/components/cancellable_loader.AbortSignal`` is a bare dataclass with an
    ``aborted`` flag and nothing else -- and that is the one the interactive session
    passes down: ``interactive/interactive_mode.py`` hands ``dialog.signal`` -- a
    ``login_dialog`` controller's signal -- straight into ``authStorage.login``. If a
    signal without ``wait()`` were parked here forever,
    every helper in this module would degrade into a plain await for exactly that shape,
    and pressing Esc would do nothing until the request finished on its own; polling the
    flag is what avoids that.
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
    """A signal misaka's helpers accept: an ``aborted`` flag plus an awaitable ``wait()``."""

    def __init__(self) -> None:
        self.aborted = False
        self._event = asyncio.Event()

    def abort(self) -> None:
        self.aborted = True
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


class CombinedAbortSignal:
    """Aborted as soon as any of its inputs is -- upstream's ``combineAbortSignals``.

    Upstream (``utils/abort-signals.ts``) builds this out of a fresh ``AbortController``
    plus one ``abort`` listener per input, and hands back a ``cleanup()`` to unsubscribe
    them. This is a view over the inputs instead: nothing is subscribed, so there is
    nothing for a ``cleanup()`` to unsubscribe and nothing to leak by forgetting to.
    """

    def __init__(self, *signals: Any) -> None:
        self._signals = [signal for signal in signals if signal is not None]

    @property
    def aborted(self) -> bool:
        return any(signal_aborted(signal) for signal in self._signals)

    async def wait(self) -> None:
        if not self._signals:
            # Nothing can tell us about an abort, so waiting forever is honest; reporting
            # one immediately would abort work that was never cancelled.
            await asyncio.Event().wait()
            return
        tasks = [asyncio.ensure_future(wait_for_abort(signal)) for signal in self._signals]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()


def combine_abort_signals(*signals: Any) -> Any:
    """One signal covering them all, collapsing the trivial cases the way upstream does."""
    active = [signal for signal in signals if signal is not None]
    if not active:
        return None
    if len(active) == 1:
        return active[0]
    return CombinedAbortSignal(*active)


def throw_if_aborted(signal: Any) -> None:
    """pi's ``signal.throwIfAborted()``, raising what the request path already raises."""
    if signal_aborted(signal):
        raise RuntimeError("Request was aborted")


async def race_with_abort_signal[T](operation: Awaitable[T], signal: Any) -> T:
    """Stop waiting when the signal aborts, while still observing the abandoned work."""
    task = asyncio.ensure_future(operation)
    if signal_aborted(signal):
        task.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)
        raise RuntimeError("Request was aborted")

    if signal is None:
        return await task

    aborting = asyncio.ensure_future(wait_for_abort(signal))
    try:
        await asyncio.wait({task, aborting}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        aborting.cancel()

    if task.done():
        return task.result()
    # The signal won. Keep observing the task so its eventual failure is not "never
    # retrieved", then report the abort to the caller who is no longer waiting.
    task.add_done_callback(lambda finished: finished.exception() if not finished.cancelled() else None)
    raise RuntimeError("Request was aborted")


async def sleep(ms: float, signal: Any = None) -> None:
    """Sleep, unless the caller gives up first."""
    throw_if_aborted(signal)
    delay = max(0.0, ms) / 1000
    if signal is None:
        await asyncio.sleep(delay)
        return
    aborting = asyncio.ensure_future(wait_for_abort(signal))
    sleeping = asyncio.ensure_future(asyncio.sleep(delay))
    try:
        await asyncio.wait({sleeping, aborting}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        aborting.cancel()
        sleeping.cancel()
    throw_if_aborted(signal)


__all__ = [
    "FLAG_POLL_INTERVAL_S",
    "AbortController",
    "CombinedAbortSignal",
    "combine_abort_signals",
    "race_with_abort_signal",
    "sleep",
    "throw_if_aborted",
    "wait_for_abort",
]
