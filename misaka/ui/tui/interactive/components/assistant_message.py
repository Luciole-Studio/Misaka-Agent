"""Assistant-message renderer for interactive chat transcripts."""

from __future__ import annotations

from typing import Any

from misaka.ui.tui import (
    Container,
    DefaultTextStyle,
    Markdown,
    MarkdownTheme,
    Spacer,
    Text,
)
from misaka.ui.tui.interactive.theme.theme import get_markdown_theme, theme

OSC133_ZONE_START = "\x1b]133;A\x07"
OSC133_ZONE_END = "\x1b]133;B\x07"
OSC133_ZONE_FINAL = "\x1b]133;C\x07"


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _visible_content(block: Any) -> bool:
    block_type = _value(block, "type")
    if block_type == "text":
        text = _value(block, "text", "")
        return bool(text.strip()) if isinstance(text, str) else False
    if block_type == "thinking":
        thinking = _value(block, "thinking", "")
        return bool(thinking.strip()) if isinstance(thinking, str) else False
    return False


class AssistantMessageComponent(Container):
    def __init__(
        self,
        message: Any | None = None,
        hideThinkingBlock: bool = False,
        markdownTheme: MarkdownTheme | None = None,
        hiddenThinkingLabel: str = "Thinking...",
    ) -> None:
        super().__init__()
        self.contentContainer = Container()
        self.hideThinkingBlock = hideThinkingBlock
        self.markdownTheme = markdownTheme or get_markdown_theme()
        self.hiddenThinkingLabel = hiddenThinkingLabel
        self.lastMessage: Any | None = None
        self.hasToolCalls = False
        self.addChild(self.contentContainer)
        if message is not None:
            self.updateContent(message)

    def invalidate(self) -> None:
        super().invalidate()
        if self.lastMessage is not None:
            self.updateContent(self.lastMessage)

    def setHideThinkingBlock(self, hide: bool) -> None:
        self.hideThinkingBlock = hide
        if self.lastMessage is not None:
            self.updateContent(self.lastMessage)

    def setHiddenThinkingLabel(self, label: str) -> None:
        self.hiddenThinkingLabel = label
        if self.lastMessage is not None:
            self.updateContent(self.lastMessage)

    def render(self, width: int) -> list[str]:
        lines = list(super().render(width))
        if self.hasToolCalls or not lines:
            return lines
        lines[0] = OSC133_ZONE_START + lines[0]
        lines[-1] = OSC133_ZONE_END + OSC133_ZONE_FINAL + lines[-1]
        return lines

    def updateContent(self, message: Any) -> None:
        self.lastMessage = message

        content = list(_value(message, "content", []) or [])

        # Fast path: if the content is a single text block (the common
        # streaming case), update the existing Markdown via setText() instead
        # of tearing down and rebuilding the component tree.  This avoids a
        # thread-safety race where the background render timer can observe
        # the container in a partially-cleared state (between clear() and
        # addChild()), producing incorrect line counts that corrupt the
        # differential renderer.
        if (
            len(content) == 1
            and _value(content[0], "type") == "text"
            and not self.hasToolCalls
        ):
            text_value = _value(content[0], "text", "")
            text = text_value.strip() if isinstance(text_value, str) else ""
            if text:
                if (
                    hasattr(self, "_fast_markdown")
                    and self._fast_markdown is not None
                ):
                    # Update existing Markdown in-place (atomic under GIL).
                    self._fast_markdown.setText(text)
                    return
                else:
                    # First update -- create persistent children.
                    self.contentContainer.clear()
                    self._fast_markdown = Markdown(text, 1, 0, self.markdownTheme)
                    self.contentContainer.addChild(Spacer(1))
                    self.contentContainer.addChild(self._fast_markdown)
                    return
            # Empty text -- fall through to full rebuild.

        # Slow path: full rebuild for complex content (thinking blocks,
        # multiple text blocks, tool calls, errors, etc.).
        self._fast_markdown = None
        self.contentContainer.clear()

        has_visible_content = any(_visible_content(block) for block in content)
        if has_visible_content:
            self.contentContainer.addChild(Spacer(1))

        for index, block in enumerate(content):
            block_type = _value(block, "type")
            if block_type == "text":
                text_value = _value(block, "text", "")
                text = text_value.strip() if isinstance(text_value, str) else ""
                if text:
                    self.contentContainer.addChild(Markdown(text, 1, 0, self.markdownTheme))
            elif block_type == "thinking":
                thinking_value = _value(block, "thinking", "")
                thinking = thinking_value.strip() if isinstance(thinking_value, str) else ""
                if thinking:
                    has_visible_after = any(_visible_content(item) for item in content[index + 1 :])
                    if self.hideThinkingBlock:
                        self.contentContainer.addChild(
                            Text(theme.italic(theme.fg("thinkingText", self.hiddenThinkingLabel)), 1, 0)
                        )
                    else:
                        self.contentContainer.addChild(
                            Markdown(
                                thinking,
                                1,
                                0,
                                self.markdownTheme,
                                DefaultTextStyle(
                                    color=lambda value: theme.fg("thinkingText", value),
                                    italic=True,
                                ),
                            )
                        )
                    if has_visible_after:
                        self.contentContainer.addChild(Spacer(1))

        self.hasToolCalls = any(_value(block, "type") == "toolCall" for block in content)
        if self.hasToolCalls:
            return

        stop_reason = _value(message, "stopReason")
        error_message = _value(message, "errorMessage")
        if stop_reason == "aborted":
            abort_message = (
                str(error_message)
                if isinstance(error_message, str) and error_message and error_message != "Request was aborted"
                else "Operation aborted"
            )
            self.contentContainer.addChild(Spacer(1))
            self.contentContainer.addChild(Text(theme.fg("error", abort_message), 1, 0))
        elif stop_reason == "error":
            self.contentContainer.addChild(Spacer(1))
            self.contentContainer.addChild(Text(theme.fg("error", f"Error: {error_message or 'Unknown error'}"), 1, 0))


__all__ = ["AssistantMessageComponent"]
