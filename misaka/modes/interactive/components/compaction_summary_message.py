"""Translated from pi's ``modes/interactive/components/compaction-summary-message.ts`` (v1.1.0).

MISAKA fork (D1): the content is not wrapped in a ``MouseRegion``; only the fullscreen renderer delivers clicks.
"""

from __future__ import annotations

from misaka.core.messages import CompactionSummaryMessage
from misaka.modes.interactive.theme.theme import getMarkdownTheme, theme
from misaka.ui.tui import (
    Box,
    Container,
    DefaultTextStyle,
    Markdown,
    MarkdownTheme,
    Spacer,
    Text,
)

from .keybinding_hints import keyText


class CompactionSummaryMessageComponent(Box):
    """Component that renders a compaction message with collapsed/expanded state.
    Uses same background color as custom messages for visual consistency.
    """

    def __init__(
        self, message: CompactionSummaryMessage, markdownTheme: MarkdownTheme | None = None, outputPad: int = 1
    ) -> None:
        super().__init__(outputPad, 1, lambda t: theme.bg("customMessageBg", t))
        self.expanded = False
        self.message = message
        self.markdownTheme = markdownTheme if markdownTheme is not None else getMarkdownTheme()
        self.updateDisplay()

    def setExpanded(self, expanded: bool) -> None:
        self.expanded = expanded
        self.updateDisplay()

    def setOutputPad(self, outputPad: int) -> None:
        self.setPaddingX(outputPad)

    def invalidate(self) -> None:
        super().invalidate()
        self.updateDisplay()

    def updateDisplay(self) -> None:
        self.clear()
        content = Container()

        # toLocaleString() in the default (en-US) locale.
        tokenStr = format(self.message.tokensBefore, ",")
        label = theme.fg("customMessageLabel", "\x1b[1m[compaction]\x1b[22m")
        content.addChild(Text(label, 0, 0))
        content.addChild(Spacer(1))

        if self.expanded:
            header = f"**Compacted from {tokenStr} tokens**\n\n"
            content.addChild(
                Markdown(
                    header + self.message.summary,
                    0,
                    0,
                    self.markdownTheme,
                    DefaultTextStyle(color=lambda text: theme.fg("customMessageText", text)),
                )
            )
        else:
            content.addChild(
                Text(
                    theme.fg("customMessageText", f"Compacted from {tokenStr} tokens (")
                    + theme.fg("dim", keyText("app.tools.expand"))
                    + theme.fg("customMessageText", " to expand)"),
                    0,
                    0,
                )
            )

        self.addChild(content)
