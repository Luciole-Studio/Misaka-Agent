"""Text loader with optional spinner animation."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from misaka.ui.tui.tui import TUI

from .text import Text

DEFAULT_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
DEFAULT_INTERVAL_MS = 80


@dataclass(slots=True)
class LoaderIndicatorOptions:
    frames: list[str] | None = None
    intervalMs: int | None = None


class Loader(Text):
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
        self.intervalId: threading.Timer | None = None
        self.ui = ui
        self.renderIndicatorVerbatim = False
        self.spinnerColorFn = spinnerColorFn
        self.messageColorFn = messageColorFn
        self.message = message
        self._animationToken = 0
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

    def setIndicator(self, indicator: LoaderIndicatorOptions | None = None) -> None:
        self.renderIndicatorVerbatim = indicator is not None
        self.frames = [*(indicator.frames if indicator and indicator.frames is not None else DEFAULT_FRAMES)]
        interval_ms = indicator.intervalMs if indicator is not None else None
        self.intervalMs = interval_ms if interval_ms is not None and interval_ms > 0 else DEFAULT_INTERVAL_MS
        self.currentFrame = 0
        self.start()

    def restartAnimation(self) -> None:
        self.stop()
        if len(self.frames) <= 1:
            return
        token = self._animationToken
        self._scheduleNextFrame(token)

    def _scheduleNextFrame(self, token: int) -> None:
        def tick() -> None:
            if token != self._animationToken:
                return
            self.currentFrame = (self.currentFrame + 1) % len(self.frames)
            self.updateDisplay()
            self._scheduleNextFrame(token)

        timer = threading.Timer(self.intervalMs / 1000, tick)
        timer.daemon = True
        self.intervalId = timer
        timer.start()

    def updateDisplay(self) -> None:
        frame = self.frames[self.currentFrame] if 0 <= self.currentFrame < len(self.frames) else ""
        rendered_frame = frame if self.renderIndicatorVerbatim else self.spinnerColorFn(frame)
        indicator = f"{rendered_frame} " if frame else ""
        self.setText(f"{indicator}{self.messageColorFn(self.message)}")
        if self.ui is not None:
            self.ui.requestRender()


__all__ = ["Loader", "LoaderIndicatorOptions"]
