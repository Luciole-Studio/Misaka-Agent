"""Translated from pi's ``modes/interactive/components/custom-entry.ts`` (v1.1.0)."""

from __future__ import annotations

from typing import Any

from misaka.core.extensions.types import EntryRenderer
from misaka.core.session_manager import CustomEntry
from misaka.modes.interactive.theme.theme import theme
from misaka.ui.tui import Box, Component, Container, Spacer, Text
from misaka.utils.js import errorMessage


class CustomEntryComponent(Container):
    """Component that renders a custom session entry from extensions.
    The host owns transcript spacing; renderer output should provide only its content.
    """

    def __init__(self, entry: CustomEntry, renderer: EntryRenderer[Any], outputPad: int = 1) -> None:
        super().__init__()
        self.customComponent: Component | None = None
        self._expanded = False
        self.entry = entry
        self.renderer = renderer
        self.outputPad = outputPad
        self.rebuild()

    def hasContent(self) -> bool:
        return self.customComponent is not None

    def setExpanded(self, expanded: bool) -> None:
        if self._expanded != expanded:
            self._expanded = expanded
            self.rebuild()

    def setOutputPad(self, outputPad: int) -> None:
        self.outputPad = outputPad
        self.rebuild()

    def invalidate(self) -> None:
        super().invalidate()
        self.rebuild()

    def rebuild(self) -> None:
        self.clear()
        self.customComponent = None

        component: Component | None
        try:
            component = self.renderer(self.entry, {"expanded": self._expanded}, theme)
        except Exception as error:  # noqa: BLE001 - renderer failures stay visible in the transcript
            message = errorMessage(error)
            box = Box(self.outputPad, 1, lambda text: theme.bg("customMessageBg", text))
            # MISAKA fork: a custom entry is a dict.
            box.addChild(Text(theme.fg("error", f"[{self.entry.get('customType')}] renderer failed: {message}"), 0, 0))
            component = box

        if component is None:
            return

        self.customComponent = component
        self.addChild(Spacer(1))
        self.addChild(component)
