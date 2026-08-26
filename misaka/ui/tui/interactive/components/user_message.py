"""User-message renderer for interactive chat transcripts."""

from __future__ import annotations

from misaka.ui.tui import Box, Container, DefaultTextStyle, Markdown, MarkdownTheme
from misaka.ui.tui.interactive.theme.theme import get_markdown_theme, theme

OSC133_ZONE_START = "\x1b]133;A\x07"
OSC133_ZONE_END = "\x1b]133;B\x07"
OSC133_ZONE_FINAL = "\x1b]133;C\x07"


class UserMessageComponent(Container):
    def __init__(self, text: str, markdownTheme: MarkdownTheme | None = None) -> None:
        super().__init__()
        self.contentBox = Box(1, 1, lambda content: theme.bg("userMessageBg", content))
        self.contentBox.addChild(
            Markdown(
                text,
                0,
                0,
                markdownTheme or get_markdown_theme(),
                DefaultTextStyle(color=lambda content: theme.fg("userMessageText", content)),
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
