"""Fake timers for the tests that port vitest's, and ``flush`` to let runnable tasks run."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any


class FakeTimers:
    """pi's tests use vitest's fake timers; this ``timers`` object (``setTimeout``/``clearTimeout``) runs a timer when
    ``advance(ms)`` reaches it."""

    def __init__(self) -> None:
        self.now = 0.0
        self._sequence = 0
        self._timers: list[SimpleNamespace] = []

    def setTimeout(self, callback: Callable[[], None], delayMs: float) -> SimpleNamespace:
        self._sequence += 1
        timer = SimpleNamespace(when=self.now + delayMs, callback=callback, cancelled=False, sequence=self._sequence)
        self._timers.append(timer)
        return timer

    def clearTimeout(self, handle: Any) -> None:
        if handle is not None:
            handle.cancelled = True

    def advance(self, ms: float) -> None:
        """``vi.advanceTimersByTime(ms)``: run, in order, every timer due within ``ms``."""
        target = self.now + ms
        while True:
            due = [timer for timer in self._timers if not timer.cancelled and timer.when <= target]
            if not due:
                break
            timer = min(due, key=lambda candidate: (candidate.when, candidate.sequence))
            self._timers.remove(timer)
            self.now = max(self.now, timer.when)
            timer.callback()
        self.now = target
        self._timers = [timer for timer in self._timers if not timer.cancelled]

    @property
    def pending(self) -> int:
        return len([timer for timer in self._timers if not timer.cancelled])

    async def advanceAsync(self, ms: float) -> None:
        """``vi.advanceTimersByTimeAsync(ms)``: like ``advance``, but the tasks that became runnable get to run between
        two timers and before the first."""
        target = self.now + ms
        await flush()
        while True:
            due = [timer for timer in self._timers if not timer.cancelled and timer.when <= target]
            if not due:
                break
            timer = min(due, key=lambda candidate: (candidate.when, candidate.sequence))
            self._timers.remove(timer)
            self.now = max(self.now, timer.when)
            timer.callback()
            await flush()
        self.now = target
        self._timers = [timer for timer in self._timers if not timer.cancelled]


async def flush(turns: int = 20) -> None:
    """Let the tasks that can run, run."""
    for _ in range(turns):
        await asyncio.sleep(0)
