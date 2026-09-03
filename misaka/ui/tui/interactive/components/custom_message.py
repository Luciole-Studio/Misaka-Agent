"""Renderer for extension-defined custom messages."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from misaka.core.extensions.types import MessageRenderer
from misaka.core.messages import CustomMessage
from misaka.ui.tui import Box, Container, DefaultTextStyle, Markdown, Spacer, Text
from misaka.ui.tui.interactive.theme.theme import get_markdown_theme, theme
from misaka.utils.values import read_field


class CustomMessageComponent(Container):
    def __init__(
        self,
        message: CustomMessage[object] | Mapping[str, Any],
        customRenderer: MessageRenderer[Any] | None = None,
        markdownTheme=None,
        outputPad: int = 1,
    ) -> None:
        super().__init__()
        self.message = message
        self.customRenderer = customRenderer
        self.markdownTheme = get_markdown_theme() if markdownTheme is None else markdownTheme
        self.outputPad = outputPad
        self.customComponent: Any | None = None
        self._expanded = False
        self.addChild(Spacer(1))
        self.box = Box(1, 1, lambda content: theme.bg("customMessageBg", content))
        self.rebuild()

    def setExpanded(self, expanded: bool) -> None:
        if self._expanded != expanded:
            self._expanded = expanded
            self.rebuild()

    def setOutputPad(self, outputPad: int) -> None:
        if self.outputPad != outputPad:
            self.outputPad = outputPad
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
                component = self.customRenderer(
                    self.message,
                    {"expanded": self._expanded, "outputPad": self.outputPad},
                    theme,
                )
            except Exception:  # noqa: BLE001 - a failing custom renderer falls back to the default
                component = None
            if component is not None:
                self.customComponent = component
                self.addChild(component)
                return

        self.addChild(self.box)
        self.box.clear()
        # A custom message reaches the default renderer in either shape: the CustomMessage the
        # type hint promises, or the plain dict an extension handed to ``sendMessage`` (see
        # misaka/core/network/messages.py's agent-messages payload). Every other reader of this
        # message -- interactive_mode's dispatch immediately above this component -- already
        # uses read_field for exactly that reason; attribute access here raised a bare
        # AttributeError that the pane surfaced to the user verbatim.
        custom_type = str(read_field(self.message, "customType", ""))
        label = theme.fg("customMessageLabel", f"\x1b[1m[{custom_type}]\x1b[22m")
        self.box.addChild(Text(label, 0, 0))
        self.box.addChild(Spacer(1))

        content = read_field(self.message, "content", "")
        if isinstance(content, str):
            text = content
        else:
            text = "\n".join(
                str(read_field(block, "text", ""))
                for block in content
                if read_field(block, "type") == "text"
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
