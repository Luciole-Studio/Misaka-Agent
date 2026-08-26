"""Generic bordered selector used by interactive extension dialogs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from misaka.ui.tui import Container, Spacer, Text, getKeybindings
from misaka.ui.tui.interactive.theme.theme import theme

from .countdown_timer import CountdownTimer
from .dynamic_border import DynamicBorder
from .keybinding_hints import key_hint, raw_key_hint


class ExtensionSelectorComponent(Container):
    def __init__(
        self,
        title: str,
        options: list[str],
        onSelect: Callable[[str], None],
        onCancel: Callable[[], None],
        opts: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.options = options
        self.selectedIndex = 0
        self.onSelectCallback = onSelect
        self.onCancelCallback = onCancel
        self.onToggleToolsExpanded = (opts or {}).get("onToggleToolsExpanded")
        self.baseTitle = title
        self.countdown: CountdownTimer | None = None

        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))

        self.titleText = Text(theme.fg("accent", theme.bold(title)), 1, 0)
        self.addChild(self.titleText)
        self.addChild(Spacer(1))

        timeout = (opts or {}).get("timeout")
        tui = (opts or {}).get("tui")
        if isinstance(timeout, int) and timeout > 0 and tui is not None:
            self.countdown = CountdownTimer(
                timeout,
                tui,
                lambda seconds: self.titleText.setText(
                    theme.fg("accent", theme.bold(f"{self.baseTitle} ({seconds}s)"))
                ),
                self.onCancelCallback,
            )

        self.listContainer = Container()
        self.addChild(self.listContainer)

        self.addChild(Spacer(1))
        self.addChild(
            Text(
                raw_key_hint("↑↓", "navigate")
                + "  "
                + key_hint("tui.select.confirm", "select")
                + "  "
                + key_hint("tui.select.cancel", "cancel"),
                1,
                0,
            )
        )
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())

        self.updateList()

    def updateList(self) -> None:
        self.listContainer.clear()
        for index, option in enumerate(self.options):
            is_selected = index == self.selectedIndex
            text = (
                theme.fg("accent", "→ ") + theme.fg("accent", option)
                if is_selected
                else f"  {theme.fg('text', option)}"
            )
            self.listContainer.addChild(Text(text, 1, 0))

    def handleInput(self, data: str) -> None:
        kb = getKeybindings()
        if kb.matches(data, "app.tools.expand"):
            callback = self.onToggleToolsExpanded
            if callable(callback):
                callback()
        elif kb.matches(data, "tui.select.up") or data == "k":
            self.selectedIndex = max(0, self.selectedIndex - 1)
            self.updateList()
        elif kb.matches(data, "tui.select.down") or data == "j":
            self.selectedIndex = min(len(self.options) - 1, self.selectedIndex + 1)
            self.updateList()
        elif kb.matches(data, "tui.select.confirm") or data == "\n":
            selected = self.options[self.selectedIndex] if 0 <= self.selectedIndex < len(self.options) else None
            if selected:
                self.onSelectCallback(selected)
        elif kb.matches(data, "tui.select.cancel"):
            self.onCancelCallback()

    def dispose(self) -> None:
        if self.countdown is not None:
            self.countdown.dispose()


__all__ = ["ExtensionSelectorComponent"]
