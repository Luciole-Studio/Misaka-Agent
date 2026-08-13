"""Reusable countdown timer for interactive dialogs."""

from __future__ import annotations

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
        self._remainingSeconds = math.ceil(timeoutMs / 1000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._onTick(self._remainingSeconds)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(1.0):
            self._remainingSeconds -= 1
            self._onTick(self._remainingSeconds)
            if self._tui is not None:
                request_render = getattr(self._tui, "requestRender", None)
                if callable(request_render):
                    request_render()
            if self._remainingSeconds <= 0:
                self.dispose()
                self._onExpire()
                return

    def dispose(self) -> None:
        self._stop.set()
        self._thread = None


__all__ = ["CountdownTimer"]
