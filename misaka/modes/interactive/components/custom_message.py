"""Translated from pi's ``modes/interactive/components/custom-message.ts`` (v1.1.0).

MISAKA fork: the message is read with ``read_field``. A custom message reaches the default renderer in either shape:
the ``CustomMessage`` the type promises, or the plain dict an extension handed to ``sendMessage`` (see
``misaka/core/network/messages.py``'s agent-messages payload).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from misaka.core.extensions.types import MessageRenderer
from misaka.core.messages import CustomMessage
from misaka.modes.interactive.theme.theme import getMarkdownTheme, theme
from misaka.ui.tui import (
    Box,
    Component,
    Container,
    DefaultTextStyle,
    Markdown,
    MarkdownTheme,
    Spacer,
    Text,
)
from misaka.utils.values import read_field


class CustomMessageComponent(Container):
    """Component that renders a custom message entry from extensions.
    Uses distinct styling to differentiate from user messages.
    """

    def __init__(
        self,
        message: CustomMessage[object] | Mapping[str, Any],
        customRenderer: MessageRenderer[Any] | None = None,
        markdownTheme: MarkdownTheme | None = None,
        outputPad: int = 1,
    ) -> None:
        super().__init__()
        self.customComponent: Component | None = None
        self._expanded = False
        self.message = message
        self.customRenderer = customRenderer
        self.markdownTheme = markdownTheme if markdownTheme is not None else getMarkdownTheme()
        self.outputPad = outputPad

        self.addChild(Spacer(1))

        # Create box with purple background (used for default rendering)
        self.box = Box(1, 1, lambda t: theme.bg("customMessageBg", t))

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
        # Remove previous content component
        if self.customComponent is not None:
            self.removeChild(self.customComponent)
            self.customComponent = None
        self.removeChild(self.box)

        # Try custom renderer first - it handles its own styling
        if self.customRenderer is not None:
            try:
                component = self.customRenderer(
                    cast("CustomMessage[Any]", self.message),
                    {"expanded": self._expanded, "outputPad": self.outputPad},
                    theme,
                )
                if component is not None:
                    # Custom renderer provides its own styled component
                    self.customComponent = component
                    self.addChild(component)
                    return
            except Exception:  # noqa: BLE001, S110 - fall through to default rendering
                pass

        # Default rendering uses our box
        self.addChild(self.box)
        self.box.clear()
        self.box.setPaddingX(self.outputPad)

        # Default rendering: label + content
        label = theme.fg("customMessageLabel", f"\x1b[1m[{read_field(self.message, 'customType')}]\x1b[22m")
        self.box.addChild(Text(label, 0, 0))
        self.box.addChild(Spacer(1))

        # Extract text content
        content = read_field(self.message, "content")
        if isinstance(content, str):
            text = content
        else:
            text = "\n".join(read_field(c, "text") for c in content if read_field(c, "type") == "text")

        self.box.addChild(
            Markdown(
                text,
                0,
                0,
                self.markdownTheme,
                DefaultTextStyle(color=lambda text: theme.fg("customMessageText", text)),
            )
        )
