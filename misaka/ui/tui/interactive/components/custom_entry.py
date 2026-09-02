"""Renderer for extension-defined custom session entries."""

from __future__ import annotations

from typing import Any

from misaka.core.extensions.types import EntryRenderer
from misaka.core.session_manager import CustomEntry
from misaka.ui.tui import Box, Container, Spacer, Text
from misaka.ui.tui.interactive.theme.theme import theme


class CustomEntryComponent(Container):
    def __init__(self, entry: CustomEntry, renderer: EntryRenderer[Any]) -> None:
        super().__init__()
        self.entry = entry
        self.renderer = renderer
        self.customComponent: Any | None = None
        self._expanded = False
        self.rebuild()

    def hasContent(self) -> bool:
        return self.customComponent is not None

    def setExpanded(self, expanded: bool) -> None:
        if self._expanded != expanded:
            self._expanded = expanded
            self.rebuild()

    def invalidate(self) -> None:
        super().invalidate()
        self.rebuild()

    def rebuild(self) -> None:
        self.clear()
        self.customComponent = None
        try:
            component = self.renderer(self.entry, {"expanded": self._expanded}, theme)
        except Exception as error:  # noqa: BLE001 - renderer failures stay visible in the transcript
            box = Box(1, 1, lambda text: theme.bg("customMessageBg", text))
            box.addChild(
                Text(
                    theme.fg(
                        "error",
                        f"[{self.entry.get('customType', '')}] renderer failed: {error}",
                    ),
                    0,
                    0,
                )
            )
            component = box
        if component is None:
            return
        self.customComponent = component
        self.addChild(Spacer(1))
        self.addChild(component)


__all__ = ["CustomEntryComponent"]
