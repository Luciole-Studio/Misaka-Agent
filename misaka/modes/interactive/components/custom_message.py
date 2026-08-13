"""Renderer for extension-defined custom messages."""

from __future__ import annotations

from typing import Any

from misaka.tui import Box, Container, DefaultTextStyle, Markdown, Spacer, Text

from misaka.core.extensions.types import MessageRenderer
from misaka.core.messages import CustomMessage
from misaka.modes.interactive.theme.theme import get_markdown_theme, theme

class CustomMessageComponent(Container):
    def __init__(
        self,
        message: CustomMessage[object],
        customRenderer: MessageRenderer[Any] | None = None,
        markdownTheme=None,  # noqa: ANN001
    ) -> None:
        super().__init__()
        self.message = message
        self.customRenderer = customRenderer
        self.markdownTheme = get_markdown_theme() if markdownTheme is None else markdownTheme
        self.customComponent: Any | None = None
        self._expanded = False
        self.addChild(Spacer(1))
        self.box = Box(1, 1, lambda content: theme.bg("customMessageBg", content))
        self.rebuild()

    def setExpanded(self, expanded: bool) -> None:
        if self._expanded != expanded:
            self._expanded = expanded
            self.rebuild()

    def invalidate(self) -> None:
        super().invalidate()
        self.rebuild()

    def rebuild(self) -> None:
        if self.customComponent is not None:
            self.removeChild(self.customComponent)
            self.customComponent = None
        self.removeChild(self.box)

        if self.customRenderer is not None:
            try:
                component = self.customRenderer(self.message, {"expanded": self._expanded}, theme)
            except Exception:
                component = None
            if component is not None:
                self.customComponent = component
                self.addChild(component)
                return

        self.addChild(self.box)
        self.box.clear()
        label = theme.fg("customMessageLabel", f"\x1b[1m[{self.message.customType}]\x1b[22m")
        self.box.addChild(Text(label, 0, 0))
        self.box.addChild(Spacer(1))

        if isinstance(self.message.content, str):
            text = self.message.content
        else:
            text = "\n".join(
                block.text for block in self.message.content if getattr(block, "type", None) == "text"
            )
        self.box.addChild(
            Markdown(
                text,
                0,
                0,
                self.markdownTheme,
                DefaultTextStyle(color=lambda content: theme.fg("customMessageText", content)),
            )
        )


__all__ = ["CustomMessageComponent"]
