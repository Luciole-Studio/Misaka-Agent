"""Translated from pi's ``modes/interactive/components/status-indicator.ts`` (v1.3.0)."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any, Literal

from misaka.modes.interactive.theme.theme import theme
from misaka.ui.tui import Loader, LoaderIndicatorOptions, truncateToWidth
from misaka.utils.js import jsTrimEnd

from .countdown_timer import CountdownTimer
from .keybinding_hints import keyText

type StatusIndicatorKind = Literal["working", "retry", "compaction", "branchSummary"]


class StatusIndicator(Loader):
    def __init__(
        self,
        kind: StatusIndicatorKind,
        ui: Any,
        spinnerColorFn: Callable[[str], str],
        messageColorFn: Callable[[str], str],
        message: str,
        indicator: LoaderIndicatorOptions | None = None,
    ) -> None:
        super().__init__(ui, spinnerColorFn, messageColorFn, message, indicator)
        self.kind: StatusIndicatorKind = kind

    def renderInBorder(self, width: int) -> str:
        rendered = super().render(width + 2)
        line = rendered[1] if len(rendered) > 1 else ""
        return truncateToWidth(jsTrimEnd(line[1:]) if line.startswith(" ") else jsTrimEnd(line), width, "")

    def renderSpinnerInBorder(self, width: int) -> str:
        return truncateToWidth(self.getRenderedIndicator(), width, "")

    def dispose(self) -> None:
        self.stop()


class WorkingStatusIndicator(StatusIndicator):
    def __init__(
        self,
        ui: Any,
        message: str,
        indicator: LoaderIndicatorOptions | None = None,
        colorFn: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__(
            "working",
            ui,
            colorFn if colorFn is not None else (lambda text: theme.fg("accent", text)),
            colorFn if colorFn is not None else (lambda text: theme.fg("muted", text)),
            message,
            indicator,
        )


class RetryStatusIndicator(StatusIndicator):
    def __init__(self, ui: Any, attempt: int, maxAttempts: int, delayMs: float) -> None:
        def retryMessage(seconds: int) -> str:
            return f"Retrying ({attempt}/{maxAttempts}) in {seconds}s... ({keyText('app.interrupt')} to cancel)"

        super().__init__(
            "retry",
            ui,
            lambda spinner: theme.fg("warning", spinner),
            lambda text: theme.fg("muted", text),
            retryMessage(math.ceil(delayMs / 1000)),
        )

        self.countdown: CountdownTimer | None = None

        def onExpire() -> None:
            self.countdown = None

        self.countdown = CountdownTimer(
            delayMs,
            ui,
            lambda seconds: self.setMessage(retryMessage(seconds)),
            onExpire,
        )

    def dispose(self) -> None:
        if self.countdown is not None:
            self.countdown.dispose()
        self.countdown = None
        super().dispose()


type CompactionStatusReason = Literal["manual", "threshold", "overflow"]


class CompactionStatusIndicator(StatusIndicator):
    def __init__(self, ui: Any, reason: CompactionStatusReason, colorFn: Callable[[str], str] | None = None) -> None:
        cancelHint = f"({keyText('app.interrupt')} to cancel)"
        label = (
            f"Compacting context... {cancelHint}"
            if reason == "manual"
            else f"{'Context overflow detected, ' if reason == 'overflow' else ''}Auto-compacting... {cancelHint}"
        )
        super().__init__(
            "compaction",
            ui,
            colorFn if colorFn is not None else (lambda spinner: theme.fg("accent", spinner)),
            colorFn if colorFn is not None else (lambda text: theme.fg("muted", text)),
            label,
        )


class BranchSummaryStatusIndicator(StatusIndicator):
    def __init__(self, ui: Any) -> None:
        super().__init__(
            "branchSummary",
            ui,
            lambda spinner: theme.fg("accent", spinner),
            lambda text: theme.fg("muted", text),
            f"Summarizing branch... ({keyText('app.interrupt')} to cancel)",
        )


class IdleStatus:
    def invalidate(self) -> None:
        # No cached state to invalidate.
        pass

    def render(self, width: int) -> list[str]:
        emptyLine = " " * width
        return [emptyLine, emptyLine]
