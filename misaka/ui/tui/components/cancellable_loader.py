"""Translated from pi-tui's ``components/cancellable-loader.ts`` (v1.1.0)."""

from __future__ import annotations

from collections.abc import Callable

from misaka.ui.tui.keybindings import getKeybindings
from misaka.ui.tui.tui import TUI
from misaka.utils.js import AbortController, AbortSignal

from .loader import Loader, LoaderIndicatorOptions


class CancellableLoader(Loader):
    """Loader that can be cancelled with Escape.
    Extends Loader with an AbortSignal for cancelling async operations.

    @example
    loader = CancellableLoader(tui, cyan, dim, "Working...")
    loader.onAbort = lambda: done(None)
    doWork(loader.signal).then(done)
    """

    def __init__(
        self,
        ui: TUI,
        spinnerColorFn: Callable[[str], str],
        messageColorFn: Callable[[str], str],
        message: str = "Loading...",
        indicator: LoaderIndicatorOptions | None = None,
    ) -> None:
        super().__init__(ui, spinnerColorFn, messageColorFn, message, indicator)
        self.abortController = AbortController()

        self.onAbort: Callable[[], None] | None = None
        """Called when user presses Escape"""

    @property
    def signal(self) -> AbortSignal:
        """AbortSignal that is aborted when user presses Escape"""
        return self.abortController.signal

    @property
    def aborted(self) -> bool:
        """Whether the loader was aborted"""
        return self.abortController.signal.aborted

    def handleInput(self, data: str) -> None:
        kb = getKeybindings()
        if kb.matches(data, "tui.select.cancel"):
            self.abortController.abort()
            if self.onAbort is not None:
                self.onAbort()

    def dispose(self) -> None:
        self.stop()


__all__ = ["CancellableLoader"]
