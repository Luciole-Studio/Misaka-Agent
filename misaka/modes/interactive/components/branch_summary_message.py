"""Branch-summary renderer for interactive chat transcripts."""

from __future__ import annotations

from misaka.tui import Box, DefaultTextStyle, Markdown, Spacer, Text

from misaka.core.messages import BranchSummaryMessage
from misaka.modes.interactive.components.keybinding_hints import key_text
from misaka.modes.interactive.theme.theme import get_markdown_theme, theme


class BranchSummaryMessageComponent(Box):
    def __init__(self, message: BranchSummaryMessage, markdownTheme=None) -> None:  # noqa: ANN001
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
        label = theme.fg("customMessageLabel", "\x1b[1m[branch]\x1b[22m")
        self.addChild(Text(label, 0, 0))
        self.addChild(Spacer(1))
        if self.expanded:
            header = "**Branch Summary**\n\n"
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
                theme.fg("customMessageText", "Branch summary (")
                + theme.fg("dim", key_text("app.tools.expand"))
                + theme.fg("customMessageText", " to expand)"),
                0,
                0,
            )
        )


__all__ = ["BranchSummaryMessageComponent"]
