"""Generic bordered input used by interactive extension dialogs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from misaka.ui.tui import Container, Input, Spacer, Text, getKeybindings
from misaka.ui.tui.interactive.theme.theme import theme

from .countdown_timer import CountdownTimer
from .dynamic_border import DynamicBorder
from .keybinding_hints import key_hint


class ExtensionInputComponent(Container):
    def __init__(
        self,
        title: str,
        placeholder: str | None,
        onSubmit: Callable[[str], None],
        onCancel: Callable[[], None],
        opts: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._focused = False
        self.onSubmitCallback = onSubmit
        self.onCancelCallback = onCancel
        self.baseTitle = title
        self.countdown: CountdownTimer | None = None

        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))

        self.titleText = Text(theme.fg("accent", title), 1, 0)
        self.addChild(self.titleText)
        self.addChild(Spacer(1))

        timeout = (opts or {}).get("timeout")
        tui = (opts or {}).get("tui")
        if isinstance(timeout, int) and timeout > 0 and tui is not None:
            self.countdown = CountdownTimer(
                timeout,
                tui,
                lambda seconds: self.titleText.setText(theme.fg("accent", f"{self.baseTitle} ({seconds}s)")),
                self.onCancelCallback,
            )

        self.input = Input()
        self.addChild(self.input)
        self.addChild(Spacer(1))
        self.addChild(
            Text(
                f"{key_hint('tui.select.confirm', 'submit')}  {key_hint('tui.select.cancel', 'cancel')}",
                1,
                0,
            )
        )
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.input.focused = value

    def handleInput(self, data: str) -> None:
        kb = getKeybindings()
        if kb.matches(data, "tui.select.confirm") or data == "\n":
            self.onSubmitCallback(self.input.getValue())
        elif kb.matches(data, "tui.select.cancel"):
            self.onCancelCallback()
        else:
            self.input.handleInput(data)

    def dispose(self) -> None:
        if self.countdown is not None:
            self.countdown.dispose()


__all__ = ["ExtensionInputComponent"]
