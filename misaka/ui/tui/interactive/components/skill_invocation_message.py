"""Renderer for collapsed and expanded skill invocation blocks."""

from __future__ import annotations

from misaka.core.agent_session import ParsedSkillBlock
from misaka.ui.tui import Box, DefaultTextStyle, Markdown, Text
from misaka.ui.tui.interactive.components.keybinding_hints import key_text
from misaka.ui.tui.interactive.theme.theme import get_markdown_theme, theme


class SkillInvocationMessageComponent(Box):
    def __init__(self, skillBlock: ParsedSkillBlock, markdownTheme=None) -> None:
        super().__init__(1, 1, lambda content: theme.bg("customMessageBg", content))
        self.expanded = False
        self.skillBlock = skillBlock
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
        if self.expanded:
            label = theme.fg("customMessageLabel", "\x1b[1m[skill]\x1b[22m")
            self.addChild(Text(label, 0, 0))
            header = f"**{self.skillBlock.name}**\n\n"
            self.addChild(
                Markdown(
                    header + self.skillBlock.content,
                    0,
                    0,
                    self.markdownTheme,
                    DefaultTextStyle(color=lambda text: theme.fg("customMessageText", text)),
                )
            )
            return

        line = (
            theme.fg("customMessageLabel", "\x1b[1m[skill]\x1b[22m ")
            + theme.fg("customMessageText", self.skillBlock.name)
            + theme.fg("dim", f" ({key_text('app.tools.expand')} to expand)")
        )
        self.addChild(Text(line, 0, 0))


__all__ = ["SkillInvocationMessageComponent"]
