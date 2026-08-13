"""Loader that exposes escape-driven cancellation."""

from __future__ import annotations

from dataclasses import dataclass, field

from misaka.tui.keybindings import getKeybindings

from .loader import Loader


@dataclass(slots=True)
class AbortSignal:
    aborted: bool = False


@dataclass(slots=True)
class AbortController:
    signal: AbortSignal = field(default_factory=AbortSignal)

    def abort(self) -> None:
        self.signal.aborted = True


class CancellableLoader(Loader):
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.abortController = AbortController()
        self.onAbort = None
        super().__init__(*args, **kwargs)

    @property
    def signal(self) -> AbortSignal:
        return self.abortController.signal

    @property
    def aborted(self) -> bool:
        return self.abortController.signal.aborted

    def handleInput(self, data: str) -> None:
        kb = getKeybindings()
        if kb.matches(data, "tui.select.cancel"):
            self.abortController.abort()
            if callable(self.onAbort):
                self.onAbort()

    def dispose(self) -> None:
        self.stop()


__all__ = ["CancellableLoader"]
