"""Translated from pi's ``modes/interactive/components/user-message.ts`` (v1.1.0)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from misaka.core.extensions.types import MarkdownTransformer
from misaka.modes.interactive.theme.theme import getMarkdownTheme, theme
from misaka.ui.tui import (
    Container,
    DefaultTextStyle,
    Markdown,
    MarkdownOptions,
    MarkdownTheme,
)

from .markdown_transform import createMarkdownTransform

OSC133_ZONE_START = "\x1b]133;A\x07"
OSC133_ZONE_END = "\x1b]133;B\x07"
OSC133_ZONE_FINAL = "\x1b]133;C\x07"


def _bubbleMarkdownTheme(base: MarkdownTheme) -> MarkdownTheme:
    """MISAKA fork: the user-message bubble is filled with the accent colour, so the markdown theme's rose/accent
    headings, inline code and list bullets are the same colour as the background and vanish into it. Render them in
    the bubble's own text colour instead -- bold, so they still stand out from the body."""

    def body(content: str) -> str:
        return theme.fg("userMessageText", content)

    def strong(content: str) -> str:
        return theme.bold(theme.fg("userMessageText", content))

    return replace(base, heading=strong, code=strong, listBullet=body)


class UserMessageComponent(Container):
    """Component that renders a user message"""

    def __init__(
        self,
        text: str,
        markdownTheme: MarkdownTheme | None = None,
        outputPad: int = 1,
        markdownTransformers: Sequence[MarkdownTransformer] = (),
    ) -> None:
        super().__init__()
        self.text = text
        self.markdownTheme = markdownTheme if markdownTheme is not None else getMarkdownTheme()
        self.outputPad = outputPad
        self.markdownTransformers = markdownTransformers
        self.rebuild()

    def setOutputPad(self, padding: int) -> None:
        self.outputPad = padding
        self.rebuild()

    def rebuild(self) -> None:
        self.clear()
        # The Markdown pads and colors its own background: a Box around it would keep a second full-width copy of every
        # line, with identical output.
        self.addChild(
            Markdown(
                self.text,
                self.outputPad,
                1,
                # MISAKA fork: the bubble theme (see `_bubbleMarkdownTheme`).
                _bubbleMarkdownTheme(self.markdownTheme),
                DefaultTextStyle(
                    color=lambda content: theme.fg("userMessageText", content),
                    bgColor=lambda content: theme.bg("userMessageBg", content),
                ),
                MarkdownOptions(
                    preserveOrderedListMarkers=True,
                    preserveBackslashEscapes=True,
                    transform=createMarkdownTransform("user", False, self.markdownTransformers),
                ),
            )
        )

    def render(self, width: int) -> list[str]:
        lines = super().render(width)
        if len(lines) == 0:
            return lines

        lines[0] = OSC133_ZONE_START + lines[0]
        lines[len(lines) - 1] = OSC133_ZONE_END + OSC133_ZONE_FINAL + lines[len(lines) - 1]
        return lines
