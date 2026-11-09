"""Translated from pi's ``modes/interactive/components/assistant-message.ts`` (v1.1.0).

MISAKA forks: a message and its content blocks are read with ``read_field``, since they arrive as models or as dicts;
D1: a thinking run cannot be clicked to collapse it (``MouseRegion`` and the per-run visibility overrides serve the
fullscreen renderer's mouse events).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from misaka.core.extensions.types import MarkdownTransformer
from misaka.modes.interactive.theme.theme import getMarkdownTheme, theme
from misaka.ui.tui import (
    Container,
    DefaultTextStyle,
    Markdown,
    MarkdownOptions,
    MarkdownTheme,
    Spacer,
    Text,
)
from misaka.utils.js import jsTrim
from misaka.utils.values import read_field

from .markdown_transform import createMarkdownTransform

OSC133_ZONE_START = "\x1b]133;A\x07"
OSC133_ZONE_END = "\x1b]133;B\x07"
OSC133_ZONE_FINAL = "\x1b]133;C\x07"


def _trimmedField(content: Any, name: str) -> str:
    """MISAKA fork: ``content.<name>.trim()`` for a model or a dict."""
    value = read_field(content, name, "")
    return jsTrim(value) if isinstance(value, str) else ""


def _isVisible(c: Any) -> bool:
    """``(c.type === "text" && c.text.trim()) || (c.type === "thinking" && c.thinking.trim())``"""
    type = read_field(c, "type")
    return bool((type == "text" and _trimmedField(c, "text")) or (type == "thinking" and _trimmedField(c, "thinking")))


class AssistantMessageComponent(Container):
    """Component that renders a complete assistant message"""

    def __init__(
        self,
        message: Any | None = None,
        hideThinkingBlock: bool = False,
        markdownTheme: MarkdownTheme | None = None,
        hiddenThinkingLabel: str = "Thinking...",
        outputPad: int = 1,
        markdownTransformers: Sequence[MarkdownTransformer] = (),
    ) -> None:
        super().__init__()
        self.lastMessage: Any | None = None
        self.hasToolCalls = False
        self.isStreaming = False

        self.hideThinkingBlock = hideThinkingBlock
        self.markdownTheme = markdownTheme if markdownTheme is not None else getMarkdownTheme()
        self.hiddenThinkingLabel = hiddenThinkingLabel
        self.outputPad = outputPad
        self.markdownTransformers = markdownTransformers

        # Container for text/thinking content
        self.contentContainer = Container()
        self.addChild(self.contentContainer)

        if message:
            self.updateContent(message)

    def invalidate(self) -> None:
        super().invalidate()
        if self.lastMessage:
            self.updateContent(self.lastMessage)

    def setHideThinkingBlock(self, hide: bool) -> None:
        self.hideThinkingBlock = hide
        if self.lastMessage:
            self.updateContent(self.lastMessage)

    def setHiddenThinkingLabel(self, label: str) -> None:
        self.hiddenThinkingLabel = label
        if self.lastMessage:
            self.updateContent(self.lastMessage)

    def setOutputPad(self, padding: int) -> None:
        self.outputPad = padding
        if self.lastMessage:
            self.updateContent(self.lastMessage)

    def render(self, width: int) -> list[str]:
        lines = super().render(width)
        if self.hasToolCalls or len(lines) == 0:
            return lines

        lines[0] = OSC133_ZONE_START + lines[0]
        lines[len(lines) - 1] = OSC133_ZONE_END + OSC133_ZONE_FINAL + lines[len(lines) - 1]
        return lines

    def updateContent(self, message: Any, isStreaming: bool | None = None) -> None:
        if isStreaming is None:
            isStreaming = self.isStreaming
        self.lastMessage = message
        self.isStreaming = isStreaming

        # Clear content container
        self.contentContainer.clear()

        content: list[Any] = list(read_field(message, "content", []) or [])
        hasVisibleContent = any(_isVisible(c) for c in content)

        if hasVisibleContent:
            self.contentContainer.addChild(Spacer(1))

        # Render content in order
        i = 0
        while i < len(content):
            c = content[i]
            if read_field(c, "type") == "text" and _trimmedField(c, "text"):
                # Assistant text messages with no background - trim the text
                # Set paddingY=0 to avoid extra spacing before tool executions
                self.contentContainer.addChild(
                    Markdown(
                        _trimmedField(c, "text"),
                        self.outputPad,
                        0,
                        self.markdownTheme,
                        None,
                        MarkdownOptions(
                            transform=createMarkdownTransform("assistant", self.isStreaming, self.markdownTransformers),
                        ),
                    )
                )
            elif read_field(c, "type") == "thinking":
                thinkingBlocks: list[str] = []
                while i < len(content):
                    thinkingContent = content[i]
                    if read_field(thinkingContent, "type") != "thinking":
                        break
                    thinking = _trimmedField(thinkingContent, "thinking")
                    if thinking:
                        thinkingBlocks.append(thinking)
                    i += 1
                i -= 1

                if len(thinkingBlocks) == 0:
                    i += 1
                    continue

                # Add spacing only when another visible assistant content block follows.
                # This avoids a superfluous blank line before separately-rendered tool execution blocks.
                hasVisibleContentAfter = any(_isVisible(after) for after in content[i + 1 :])

                # MISAKA fork: D1, no per-run visibility override.
                hidden = self.hideThinkingBlock
                thinkingComponent = (
                    Text(theme.italic(theme.fg("thinkingText", self.hiddenThinkingLabel)), self.outputPad, 0)
                    if hidden
                    else Markdown(
                        "\n\n".join(thinkingBlocks),
                        self.outputPad,
                        0,
                        self.markdownTheme,
                        DefaultTextStyle(
                            color=lambda text: theme.fg("thinkingText", text),
                            italic=True,
                        ),
                        MarkdownOptions(
                            transform=createMarkdownTransform(
                                "assistant-thinking",
                                self.isStreaming,
                                self.markdownTransformers,
                            ),
                        ),
                    )
                )
                self.contentContainer.addChild(thinkingComponent)
                if hasVisibleContentAfter:
                    self.contentContainer.addChild(Spacer(1))
            i += 1

        # Check if incomplete/failed - show after partial content.
        # For aborted/error tool calls, tool execution components show the error.
        # Length stops can happen before a tool call is complete, so surface them here too.
        hasToolCalls = any(read_field(c, "type") == "toolCall" for c in content)
        self.hasToolCalls = hasToolCalls
        stopReason = read_field(message, "stopReason")
        errorMessage = read_field(message, "errorMessage")
        if stopReason == "length":
            self.contentContainer.addChild(Spacer(1))
            self.contentContainer.addChild(
                Text(theme.fg("error", "Response was truncated before completion."), self.outputPad, 0)
            )
        elif not hasToolCalls:
            if stopReason == "aborted":
                abortMessage = (
                    errorMessage if errorMessage and errorMessage != "Request was aborted" else "Operation aborted"
                )
                self.contentContainer.addChild(Spacer(1))
                self.contentContainer.addChild(Text(theme.fg("error", abortMessage), self.outputPad, 0))
            elif stopReason == "error":
                errorMsg = errorMessage or "Unknown error"
                self.contentContainer.addChild(Spacer(1))
                self.contentContainer.addChild(Text(theme.fg("error", f"Error: {errorMsg}"), self.outputPad, 0))
