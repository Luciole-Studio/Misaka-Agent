"""Translated from pi's ``modes/interactive/components/skill-invocation-message.ts`` (v1.1.0).

MISAKA fork (D1): the content is not wrapped in a ``MouseRegion``; only the fullscreen renderer delivers clicks.
"""

from __future__ import annotations

from misaka.core.agent_session import ParsedSkillBlock
from misaka.modes.interactive.theme.theme import getMarkdownTheme, theme
from misaka.ui.tui import (
    Box,
    Container,
    DefaultTextStyle,
    Markdown,
    MarkdownTheme,
    Text,
)

from .keybinding_hints import keyText


class SkillInvocationMessageComponent(Box):
    """Component that renders a skill invocation message with collapsed/expanded state.
    Uses same background color as custom messages for visual consistency.
    Only renders the skill block itself - user message is rendered separately.
    """

    def __init__(
        self, skillBlock: ParsedSkillBlock, markdownTheme: MarkdownTheme | None = None, outputPad: int = 1
    ) -> None:
        super().__init__(outputPad, 1, lambda t: theme.bg("customMessageBg", t))
        self.expanded = False
        self.skillBlock = skillBlock
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

        if self.expanded:
            # Expanded: label + skill name header + full content
            label = theme.fg("customMessageLabel", "\x1b[1m[skill]\x1b[22m")
            content.addChild(Text(label, 0, 0))
            header = f"**{self.skillBlock.name}**\n\n"
            content.addChild(
                Markdown(
                    header + self.skillBlock.content,
                    0,
                    0,
                    self.markdownTheme,
                    DefaultTextStyle(color=lambda text: theme.fg("customMessageText", text)),
                )
            )
        else:
            # Collapsed: single line - [skill] name (hint to expand)
            line = (
                theme.fg("customMessageLabel", "\x1b[1m[skill]\x1b[22m ")
                + theme.fg("customMessageText", self.skillBlock.name)
                + theme.fg("dim", f" ({keyText('app.tools.expand')} to expand)")
            )
            content.addChild(Text(line, 0, 0))

        self.addChild(content)
