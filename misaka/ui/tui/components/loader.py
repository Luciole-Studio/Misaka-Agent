"""Translated from pi-tui's ``components/loader.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass

from misaka.ui.tui.tui import TUI

from .text import Text


@dataclass(slots=True)
class LoaderIndicatorOptions:
    frames: list[str] | None = None
    """Animation frames. Use an empty array to hide the indicator."""
    intervalMs: int | None = None
    """Frame interval in milliseconds for animated indicators."""


DEFAULT_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
DEFAULT_INTERVAL_MS = 80


class Loader(Text):
    """Loader component that updates with an optional spinning animation."""

    def __init__(
        self,
        ui: TUI,
        spinnerColorFn: Callable[[str], str],
        messageColorFn: Callable[[str], str],
        message: str = "Loading...",
        indicator: LoaderIndicatorOptions | None = None,
    ) -> None:
        super().__init__("", 1, 0)
        self.frames = [*DEFAULT_FRAMES]
        self.intervalMs = DEFAULT_INTERVAL_MS
        self.currentFrame = 0
        # MISAKA fork: `setInterval` is a timer that re-arms itself (see `_scheduleNextFrame`), so this holds the
        # pending one; `_animationToken` drops a tick already under way when `stop` runs.
        self.intervalId: asyncio.TimerHandle | threading.Timer | None = None
        self._animationToken = 0
        self.ui: TUI | None = None
        self.renderIndicatorVerbatim = False
        self.message = "Loading..."
        self.ui = ui
        self.spinnerColorFn = spinnerColorFn
        self.messageColorFn = messageColorFn
        self.message = message
        self.setIndicator(indicator)

    def render(self, width: int) -> list[str]:
        return ["", *super().render(width)]

    def start(self) -> None:
        self.updateDisplay()
        self.restartAnimation()

    def stop(self) -> None:
        self._animationToken += 1
        if self.intervalId is not None:
            self.intervalId.cancel()
            self.intervalId = None

    def setMessage(self, message: str) -> None:
        self.message = message
        self.updateDisplay()

    def invalidate(self) -> None:
        super().invalidate()
        self.updateDisplay()

    def setIndicator(self, indicator: LoaderIndicatorOptions | None = None) -> None:
        self.renderIndicatorVerbatim = indicator is not None
        self.frames = [*indicator.frames] if indicator is not None and indicator.frames is not None else [*DEFAULT_FRAMES]
        self.intervalMs = (
            indicator.intervalMs
            if indicator is not None and indicator.intervalMs and indicator.intervalMs > 0
            else DEFAULT_INTERVAL_MS
        )
        self.currentFrame = 0
        self.start()

    def restartAnimation(self) -> None:
        self.stop()
        if len(self.frames) <= 1:
            return
        self._scheduleNextFrame(self._animationToken)

    def _scheduleNextFrame(self, token: int) -> None:
        """MISAKA fork: ``setInterval`` as a timer on the running event loop that re-arms itself (a daemon
        ``threading.Timer`` when no loop runs), so a frame is drawn on the thread that owns the TUI."""

        def tick() -> None:
            if token != self._animationToken:
                return
            self.currentFrame = (self.currentFrame + 1) % len(self.frames)
            self.updateDisplay()
            self._scheduleNextFrame(token)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            timer = threading.Timer(self.intervalMs / 1000, tick)
            timer.daemon = True
            self.intervalId = timer
            timer.start()
            return
        self.intervalId = loop.call_later(self.intervalMs / 1000, tick)

    def getRenderedIndicator(self) -> str:
        frame = self.frames[self.currentFrame] if self.currentFrame < len(self.frames) else ""
        return frame if self.renderIndicatorVerbatim else self.spinnerColorFn(frame)

    def updateDisplay(self) -> None:
        renderedFrame = self.getRenderedIndicator()
        indicator = f"{renderedFrame} " if len(renderedFrame) > 0 else ""
        self.setText(f"{indicator}{self.messageColorFn(self.message)}")
        if self.ui is not None:
            self.ui.requestRender()


__all__ = ["Loader", "LoaderIndicatorOptions"]
