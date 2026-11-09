"""Translated from pi's ``modes/interactive/components/auth-url.ts`` (v1.1.0)."""

from __future__ import annotations

import sys
from typing import Any

from misaka.modes.interactive.theme.theme import theme
from misaka.ui.tui import Container, Text, hyperlink
from misaka.utils import clipboard
from misaka.utils.js import errorMessage

from .keybinding_hints import keyHint


class AuthUrlComponent(Container):
    """A sign-in URL with a click hint and a copy hint. Hosts call `copy()` when `app.message.copy` is
    pressed, since a long URL wraps and often cannot be selected or clicked as a whole (SSH, tmux)."""

    def __init__(self, tui: Any, url: str) -> None:
        super().__init__()
        self.tui = tui
        self.url = url
        self.addChild(Text(theme.fg("accent", hyperlink(url, url)), 1, 0))
        self.hint = Text("", 1, 0)
        self.addChild(self.hint)
        self.setHint(keyHint("app.message.copy", "to copy"))

    def setHint(self, suffix: str) -> None:
        clickHint = "Cmd+click to open" if sys.platform == "darwin" else "Ctrl+click to open"
        self.hint.setText(f"{theme.fg('dim', hyperlink(clickHint, self.url))} {theme.fg('dim', '•')} {suffix}")
        self.tui.requestRender()

    async def copy(self) -> None:
        try:
            await clipboard.copyToClipboard(self.url)
            self.setHint(theme.fg("success", "Copied URL to clipboard"))
        except Exception as error:  # noqa: BLE001 - upstream `catch (error)` shows any failure in the hint
            self.setHint(theme.fg("error", errorMessage(error)))


__all__ = ["AuthUrlComponent"]
