"""Translated from pi's ``modes/interactive/components/extension-input.ts`` (v1.1.0).

Simple text input component for extensions.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from misaka.modes.interactive.theme.theme import theme
from misaka.ui.tui import Container, Input, Spacer, Text, getKeybindings

from .countdown_timer import CountdownTimer
from .dynamic_border import DynamicBorder
from .keybinding_hints import keyHint


class ExtensionInputOptions(TypedDict, total=False):
    tui: Any
    timeout: float | None
    initialValue: str | None
    description: str | None


class ExtensionInputComponent(Container):
    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.input.focused = value

    def __init__(
        self,
        title: str,
        _placeholder: str | None,
        onSubmit: Callable[[str], None],
        onCancel: Callable[[], None],
        opts: ExtensionInputOptions | None = None,
    ) -> None:
        super().__init__()
        self.countdown: CountdownTimer | None = None
        # Focusable implementation - propagate to input for IME cursor positioning
        self._focused = False

        self.onSubmitCallback = onSubmit
        self.onCancelCallback = onCancel
        self.baseTitle = title

        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))

        self.titleText = Text(theme.fg("accent", title), 1, 0)
        self.addChild(self.titleText)
        description = opts.get("description") if opts else None
        if description:
            self.addChild(Spacer(1))
            self.addChild(Text(theme.fg("text", description), 1, 0))
        self.addChild(Spacer(1))

        timeout = opts.get("timeout") if opts else None
        tui = opts.get("tui") if opts else None
        if timeout and timeout > 0 and tui:

            def onTick(s: int) -> None:
                self.titleText.setText(theme.fg("accent", f"{self.baseTitle} ({s}s)"))

            self.countdown = CountdownTimer(timeout, tui, onTick, lambda: self.onCancelCallback())

        self.input = Input()
        initialValue = opts.get("initialValue") if opts else None
        if initialValue:
            self.input.setValue(initialValue)
        self.addChild(self.input)
        self.addChild(Spacer(1))
        self.addChild(Text(f"{keyHint('tui.select.confirm', 'submit')}  {keyHint('tui.select.cancel', 'cancel')}", 1, 0))
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()
        if kb.matches(keyData, "tui.select.confirm") or keyData == "\n":
            self.onSubmitCallback(self.input.getValue())
        elif kb.matches(keyData, "tui.select.cancel"):
            self.onCancelCallback()
        else:
            self.input.handleInput(keyData)

    def dispose(self) -> None:
        if self.countdown is not None:
            self.countdown.dispose()


__all__ = ["ExtensionInputComponent", "ExtensionInputOptions"]
