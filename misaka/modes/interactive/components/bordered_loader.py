"""Translated from pi's ``modes/interactive/components/bordered-loader.ts`` (v1.1.0)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypedDict, cast

from misaka.ui.tui import CancellableLoader, Container, Loader, Spacer, Text
from misaka.utils.js import AbortController, AbortSignal

from .dynamic_border import DynamicBorder
from .keybinding_hints import keyHint

if TYPE_CHECKING:
    from misaka.modes.interactive.theme.theme import Theme


class BorderedLoaderOptions(TypedDict, total=False):
    """MISAKA fork: a name for the inline type of ``options``."""

    cancellable: bool


class BorderedLoader(Container):
    """Loader wrapped with borders for extension UI"""

    def __init__(self, tui: Any, theme: Theme, message: str, options: BorderedLoaderOptions | None = None) -> None:
        super().__init__()
        cancellable = options.get("cancellable") if options is not None else None
        self.cancellable: bool = True if cancellable is None else cancellable
        self.signalController: AbortController | None = None

        def borderColor(s: str) -> str:
            return theme.fg("border", s)

        self.addChild(DynamicBorder(borderColor))
        if self.cancellable:
            self.loader: CancellableLoader | Loader = CancellableLoader(
                tui,
                lambda s: theme.fg("accent", s),
                lambda s: theme.fg("muted", s),
                message,
            )
        else:
            self.signalController = AbortController()
            self.loader = Loader(
                tui,
                lambda s: theme.fg("accent", s),
                lambda s: theme.fg("muted", s),
                message,
            )
        self.addChild(self.loader)
        if self.cancellable:
            self.addChild(Spacer(1))
            self.addChild(Text(keyHint("tui.select.cancel", "cancel"), 1, 0))
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder(borderColor))

    @property
    def signal(self) -> AbortSignal:
        if self.cancellable:
            return cast(CancellableLoader, self.loader).signal
        return self.signalController.signal if self.signalController is not None else AbortController().signal

    @property
    def onAbort(self) -> Callable[[], None] | None:
        """MISAKA fork: a Python property needs a getter."""
        return cast(CancellableLoader, self.loader).onAbort if self.cancellable else None

    @onAbort.setter
    def onAbort(self, fn: Callable[[], None] | None) -> None:
        if self.cancellable:
            cast(CancellableLoader, self.loader).onAbort = fn

    def handleInput(self, data: str) -> None:
        if self.cancellable:
            cast(CancellableLoader, self.loader).handleInput(data)

    def dispose(self) -> None:
        dispose = getattr(self.loader, "dispose", None)
        if callable(dispose):
            dispose()
        else:
            stop = getattr(self.loader, "stop", None)
            if callable(stop):
                stop()

