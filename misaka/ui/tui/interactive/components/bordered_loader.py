"""Loader wrapped in bordered interactive chrome."""

from __future__ import annotations

from misaka.ui.tui import CancellableLoader, Container, Loader, Spacer, Text
from misaka.ui.tui.components import AbortController
from misaka.ui.tui.interactive.components.dynamic_border import DynamicBorder
from misaka.ui.tui.interactive.components.keybinding_hints import key_hint


class BorderedLoader(Container):
    def __init__(self, tui, theme, message: str, options: dict[str, bool] | None = None) -> None:
        super().__init__()
        self.cancellable = bool((options or {}).get("cancellable", True))
        def border_color(text: str) -> str:
            return theme.fg("border", text)
        self.addChild(DynamicBorder(border_color))
        self.signalController: AbortController | None = None
        if self.cancellable:
            self.loader: CancellableLoader | Loader = CancellableLoader(
                tui,
                lambda text: theme.fg("accent", text),
                lambda text: theme.fg("muted", text),
                message,
            )
        else:
            self.signalController = AbortController()
            self.loader = Loader(
                tui,
                lambda text: theme.fg("accent", text),
                lambda text: theme.fg("muted", text),
                message,
            )
        self.addChild(self.loader)
        if self.cancellable:
            self.addChild(Spacer(1))
            self.addChild(Text(key_hint("tui.select.cancel", "cancel"), 1, 0))
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder(border_color))

    @property
    def signal(self):
        if self.cancellable:
            return self.loader.signal
        return self.signalController.signal if self.signalController is not None else AbortController().signal

    @property
    def onAbort(self):
        return getattr(self.loader, "onAbort", None)

    @onAbort.setter
    def onAbort(self, fn) -> None:
        if self.cancellable:
            self.loader.onAbort = fn

    def handleInput(self, data: str) -> None:
        if self.cancellable:
            self.loader.handleInput(data)

    def dispose(self) -> None:
        dispose = getattr(self.loader, "dispose", None)
        if callable(dispose):
            dispose()
            return
        stop = getattr(self.loader, "stop", None)
        if callable(stop):
            stop()


__all__ = ["BorderedLoader"]
