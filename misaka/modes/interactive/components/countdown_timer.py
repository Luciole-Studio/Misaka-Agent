"""Translated from pi's ``modes/interactive/components/countdown-timer.ts`` (v1.1.0).

Reusable countdown timer for dialog components.
"""

from __future__ import annotations

import asyncio
import math
import threading
from collections.abc import Callable
from typing import Any


class CountdownTimer:
    def __init__(
        self,
        timeoutMs: float,
        tui: Any | None,
        onTick: Callable[[int], None],
        onExpire: Callable[[], None],
    ) -> None:
        # MISAKA fork: `setInterval` is a timer that re-arms itself (see `_scheduleTick`), so this holds the pending
        # one; `_intervalToken` drops a tick already under way when `dispose` runs.
        self.intervalId: asyncio.TimerHandle | threading.Timer | None = None
        self._intervalToken = 0
        self.tui = tui
        self.onTick = onTick
        self.onExpire = onExpire
        self.remainingSeconds = math.ceil(timeoutMs / 1000)
        self.onTick(self.remainingSeconds)

        self._scheduleTick(self._intervalToken)

    def _scheduleTick(self, token: int) -> None:
        """MISAKA fork: ``setInterval`` as a timer on the running event loop that re-arms itself (a daemon
        ``threading.Timer`` when no loop runs). The callbacks resolve asyncio futures (extension dialog timeouts),
        which must happen on the loop's thread to wake a loop parked in select()."""

        def tick() -> None:
            if token != self._intervalToken:
                return
            self.remainingSeconds -= 1
            self.onTick(self.remainingSeconds)
            if self.tui is not None:
                self.tui.requestRender()

            if self.remainingSeconds <= 0:
                self.dispose()
                self.onExpire()
                return
            self._scheduleTick(token)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            timer = threading.Timer(1.0, tick)
            timer.daemon = True
            self.intervalId = timer
            timer.start()
            return
        self.intervalId = loop.call_later(1.0, tick)

    def dispose(self) -> None:
        if self.intervalId:
            self._intervalToken += 1
            self.intervalId.cancel()
            self.intervalId = None
