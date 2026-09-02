"""Reusable countdown timer for interactive dialogs."""

from __future__ import annotations

import asyncio
import math
import threading
from typing import Any


class CountdownTimer:
    def __init__(
        self,
        timeoutMs: int,
        tui: Any | None,
        onTick: Any,
        onExpire: Any,
    ) -> None:
        self._tui = tui
        self._onTick = onTick
        self._onExpire = onExpire
        # The ticks run on a bare thread, but the callbacks end up resolving asyncio
        # futures (extension dialog timeouts). `Future.set_result` off the loop thread only
        # queues the wakeup without writing the self-pipe, so a loop parked in select() --
        # exactly the "user walked away" case a timeout exists for -- would not wake until
        # the next keypress. Hand the callbacks back to the loop, like terminal._on_loop.
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._remainingSeconds = math.ceil(timeoutMs / 1000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._onTick(self._remainingSeconds)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _dispatch(self, callback: Any, *args: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            callback(*args)
            return
        try:
            loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:  # loop shut down between the check and the call
            callback(*args)

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            self._remainingSeconds -= 1
            self._dispatch(self._onTick, self._remainingSeconds)
            if self._tui is not None:
                request_render = getattr(self._tui, "requestRender", None)
                if callable(request_render):
                    request_render()
            if self._remainingSeconds <= 0:
                self.dispose()
                self._dispatch(self._onExpire)
                return

    def dispose(self) -> None:
        self._stop.set()
        self._thread = None


__all__ = ["CountdownTimer"]
