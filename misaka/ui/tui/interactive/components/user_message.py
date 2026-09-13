"""User-message renderer for interactive chat transcripts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from misaka.core.extensions.types import MarkdownTransformer
from misaka.ui.tui import Box, Container, DefaultTextStyle, Markdown, MarkdownTheme
from misaka.ui.tui.interactive.components.markdown_transform import (
    create_markdown_transform,
)
from misaka.ui.tui.interactive.theme.theme import get_markdown_theme, theme


def _bubble_markdown_theme(base: MarkdownTheme) -> MarkdownTheme:
    """The user-message bubble is filled with the accent colour, so the markdown theme's
    rose/accent headings, inline code and list bullets are the same colour as the background
    and vanish into it. Render them in the bubble's own text colour instead -- bold, so they
    still stand out from the body -- rather than putting a coloured box behind them."""
    body = lambda content: theme.fg("userMessageText", content)
    strong = lambda content: theme.bold(theme.fg("userMessageText", content))
    return replace(base, heading=strong, code=strong, listBullet=body)

OSC133_ZONE_START = "\x1b]133;A\x07"
OSC133_ZONE_END = "\x1b]133;B\x07"
OSC133_ZONE_FINAL = "\x1b]133;C\x07"


class UserMessageComponent(Container):
    def __init__(
        self,
        text: str,
        markdownTheme: MarkdownTheme | None = None,
        outputPad: int = 1,
        markdownTransformers: Sequence[MarkdownTransformer] = (),
    ) -> None:
        super().__init__()
        self.text = text
        self.markdownTheme = markdownTheme or get_markdown_theme()
        self.outputPad = outputPad
        self.markdownTransformers = tuple(markdownTransformers)
        self.rebuild()

    def setOutputPad(self, padding: int) -> None:
        self.outputPad = padding
        self.rebuild()

    def rebuild(self) -> None:
        self.clear()
        self.contentBox = Box(self.outputPad, 1, lambda content: theme.bg("userMessageBg", content))
        self.contentBox.addChild(
            Markdown(
                self.text,
                0,
                0,
                _bubble_markdown_theme(self.markdownTheme),
                DefaultTextStyle(color=lambda content: theme.fg("userMessageText", content)),
                transform=create_markdown_transform("user", lambda: False, self.markdownTransformers),
            )
        )
        self.addChild(self.contentBox)

    def render(self, width: int) -> list[str]:
        lines = list(super().render(width))
        if not lines:
            return lines
        lines[0] = OSC133_ZONE_START + lines[0]
        lines[-1] = OSC133_ZONE_END + OSC133_ZONE_FINAL + lines[-1]
        return lines


__all__ = ["UserMessageComponent"]
