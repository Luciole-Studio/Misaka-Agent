"""Compaction-summary renderer for interactive chat transcripts."""

from __future__ import annotations

from misaka.ui.tui import Box, DefaultTextStyle, Markdown, Spacer, Text

from misaka.core.messages import CompactionSummaryMessage
from misaka.ui.tui.interactive.components.keybinding_hints import key_text
from misaka.ui.tui.interactive.theme.theme import get_markdown_theme, theme


class CompactionSummaryMessageComponent(Box):
    def __init__(self, message: CompactionSummaryMessage, markdownTheme=None) -> None:  # noqa: ANN001
        super().__init__(1, 1, lambda content: theme.bg("customMessageBg", content))
        self.expanded = False
        self.message = message
        self.markdownTheme = markdownTheme or get_markdown_theme()
        self.updateDisplay()

    def setExpanded(self, expanded: bool) -> None:
        self.expanded = expanded
        self.updateDisplay()

    def invalidate(self) -> None:
        super().invalidate()
        self.updateDisplay()

    def updateDisplay(self) -> None:
        self.clear()
        token_str = format(self.message.tokensBefore, ",")
        label = theme.fg("customMessageLabel", "\x1b[1m[compaction]\x1b[22m")
        self.addChild(Text(label, 0, 0))
        self.addChild(Spacer(1))
        if self.expanded:
            header = f"**Compacted from {token_str} tokens**\n\n"
            self.addChild(
                Markdown(
                    header + self.message.summary,
                    0,
                    0,
                    self.markdownTheme,
                    DefaultTextStyle(color=lambda text: theme.fg("customMessageText", text)),
                )
            )
            return

        self.addChild(
            Text(
                theme.fg("customMessageText", f"Compacted from {token_str} tokens (")
                + theme.fg("dim", key_text("app.tools.expand"))
                + theme.fg("customMessageText", " to expand)"),
                0,
                0,
            )
        )


__all__ = ["CompactionSummaryMessageComponent"]
