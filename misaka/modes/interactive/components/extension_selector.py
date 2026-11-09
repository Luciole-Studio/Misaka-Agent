"""Translated from pi's ``modes/interactive/components/extension-selector.ts`` (v1.1.0).

Generic selector component for extensions.
Displays a list of string options with keyboard navigation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from misaka.modes.interactive.theme.theme import theme
from misaka.ui.tui import Container, Spacer, Text, getKeybindings

from .countdown_timer import CountdownTimer
from .dynamic_border import DynamicBorder
from .keybinding_hints import keyHint, rawKeyHint


class ExtensionSelectorOptions(TypedDict, total=False):
    tui: Any
    timeout: float | None
    onToggleToolsExpanded: Callable[[], None] | None
    description: str | None


class ExtensionSelectorComponent(Container):
    def __init__(
        self,
        title: str,
        options: list[str],
        onSelect: Callable[[str], None],
        onCancel: Callable[[], None],
        opts: ExtensionSelectorOptions | None = None,
    ) -> None:
        super().__init__()
        self.options = options
        self.selectedIndex = 0
        self.onSelectCallback = onSelect
        self.onCancelCallback = onCancel
        self.onToggleToolsExpanded = opts.get("onToggleToolsExpanded") if opts else None
        self.baseTitle = title
        self.countdown: CountdownTimer | None = None

        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))

        self.titleText = Text(theme.fg("accent", theme.bold(title)), 1, 0)
        self.addChild(self.titleText)
        description = opts.get("description") if opts else None
        if description:
            self.addChild(Spacer(1))
            self.addChild(Text(theme.fg("text", description), 1, 0))
        self.addChild(Spacer(1))

        timeout = opts.get("timeout") if opts else None
        tui = opts.get("tui") if opts else None
        if timeout and timeout > 0 and tui:
            self.countdown = CountdownTimer(
                timeout,
                tui,
                lambda seconds: self.titleText.setText(
                    theme.fg("accent", theme.bold(f"{self.baseTitle} ({seconds}s)"))
                ),
                lambda: self.onCancelCallback(),
            )

        self.listContainer = Container()
        self.addChild(self.listContainer)

        self.addChild(Spacer(1))
        self.addChild(
            Text(
                rawKeyHint("↑↓", "navigate")
                + "  "
                + keyHint("tui.select.confirm", "select")
                + "  "
                + keyHint("tui.select.cancel", "cancel"),
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
            isSelected = index == self.selectedIndex
            text = (
                theme.fg("accent", "→ ") + theme.fg("accent", option)
                if isSelected
                else f"  {theme.fg('text', option)}"
            )
            self.listContainer.addChild(Text(text, 1, 0))

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()
        if kb.matches(keyData, "app.tools.expand"):
            callback = self.onToggleToolsExpanded
            if callable(callback):
                callback()
        elif kb.matches(keyData, "tui.select.up") or keyData == "k":
            self.selectedIndex = max(0, self.selectedIndex - 1)
            self.updateList()
        elif kb.matches(keyData, "tui.select.down") or keyData == "j":
            self.selectedIndex = min(len(self.options) - 1, self.selectedIndex + 1)
            self.updateList()
        elif kb.matches(keyData, "tui.select.confirm") or keyData == "\n":
            selected = self.options[self.selectedIndex] if 0 <= self.selectedIndex < len(self.options) else None
            if selected:
                self.onSelectCallback(selected)
        elif kb.matches(keyData, "tui.select.cancel"):
            self.onCancelCallback()

    def dispose(self) -> None:
        if self.countdown is not None:
            self.countdown.dispose()


__all__ = ["ExtensionSelectorComponent", "ExtensionSelectorOptions"]
