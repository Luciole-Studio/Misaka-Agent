"""Translated from pi's ``modes/interactive/components/user-message-selector.ts`` (v1.1.0)."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from misaka.modes.interactive.theme.theme import theme
from misaka.ui.tui import Container, Spacer, Text, getKeybindings, truncateToWidth
from misaka.utils.js import jsIndex, jsTrim, setTimeout

from .dynamic_border import DynamicBorder


@dataclass(slots=True)
class UserMessageItem:
    id: str  # Entry ID in the session
    text: str  # The message text
    timestamp: str | None = None  # Optional timestamp if available


class UserMessageList:
    """Custom user message list component with selection"""

    def __init__(self, messages: list[UserMessageItem], initialSelectedId: str | None = None) -> None:
        self.onSelect: Callable[[str], None] | None = None
        self.onCancel: Callable[[], None] | None = None
        self.maxVisible: int = 10  # Max messages visible

        # Store messages in chronological order (oldest to newest)
        self.messages = messages
        initialIndex = (
            next((index for index, message in enumerate(messages) if message.id == initialSelectedId), -1)
            if initialSelectedId
            else -1
        )
        # Start with selected message if provided, else default to the most recent
        self.selectedIndex: int = initialIndex if initialIndex >= 0 else max(0, len(messages) - 1)

    def invalidate(self) -> None:
        # No cached state to invalidate currently
        pass

    def render(self, width: int) -> list[str]:
        lines: list[str] = []

        if len(self.messages) == 0:
            lines.append(theme.fg("muted", "  No user messages found"))
            return lines

        # Calculate visible range with scrolling
        startIndex = max(
            0,
            min(self.selectedIndex - math.floor(self.maxVisible / 2), len(self.messages) - self.maxVisible),
        )
        endIndex = min(startIndex + self.maxVisible, len(self.messages))

        # Render visible messages (2 lines per message + blank line)
        for i in range(startIndex, endIndex):
            message = self.messages[i]
            isSelected = i == self.selectedIndex

            # Normalize message to single line
            normalizedMessage = jsTrim(message.text.replace("\n", " "))

            # First line: cursor + message
            cursor = theme.fg("accent", "› ") if isSelected else "  "
            maxMsgWidth = width - 2  # Account for cursor (2 chars)
            truncatedMsg = truncateToWidth(normalizedMessage, maxMsgWidth)
            messageLine = cursor + (theme.bold(truncatedMsg) if isSelected else truncatedMsg)

            lines.append(messageLine)

            # Second line: metadata (position in history)
            position = i + 1
            metadata = f"  Message {position} of {len(self.messages)}"
            metadataLine = theme.fg("muted", metadata)
            lines.append(metadataLine)
            lines.append("")  # Blank line between messages

        # Add scroll indicator if needed
        if startIndex > 0 or endIndex < len(self.messages):
            scrollInfo = theme.fg("muted", f"  ({self.selectedIndex + 1}/{len(self.messages)})")
            lines.append(scrollInfo)

        return lines

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()
        # Up arrow - go to previous (older) message, wrap to bottom when at top
        if kb.matches(keyData, "tui.select.up"):
            self.selectedIndex = len(self.messages) - 1 if self.selectedIndex == 0 else self.selectedIndex - 1
        # Down arrow - go to next (newer) message, wrap to top when at bottom
        elif kb.matches(keyData, "tui.select.down"):
            self.selectedIndex = 0 if self.selectedIndex == len(self.messages) - 1 else self.selectedIndex + 1
        # Enter - select message and branch
        elif kb.matches(keyData, "tui.select.confirm"):
            selected = jsIndex(self.messages, self.selectedIndex)
            if selected and self.onSelect:
                self.onSelect(selected.id)
        # Escape - cancel
        elif kb.matches(keyData, "tui.select.cancel"):  # noqa: SIM102 - upstream nests the callback check
            if self.onCancel:
                self.onCancel()


class UserMessageSelectorComponent(Container):
    """Component that renders a user message selector for branching"""

    def __init__(
        self,
        messages: list[UserMessageItem],
        onSelect: Callable[[str], None],
        onCancel: Callable[[], None],
        initialSelectedId: str | None = None,
    ) -> None:
        super().__init__()

        # Add header
        self.addChild(Spacer(1))
        self.addChild(Text(theme.bold("Fork from Message"), 1, 0))
        self.addChild(
            Text(
                theme.fg("muted", "Select a user message to copy the active path up to that point into a new session"),
                1,
                0,
            )
        )
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))

        # Create message list
        self.messageList = UserMessageList(messages, initialSelectedId)
        self.messageList.onSelect = onSelect
        self.messageList.onCancel = onCancel

        self.addChild(self.messageList)

        # Add bottom border
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())

        # Auto-cancel if no messages
        if len(messages) == 0:
            setTimeout(lambda: onCancel(), 100)

    def getMessageList(self) -> UserMessageList:
        return self.messageList


__all__ = ["UserMessageItem", "UserMessageSelectorComponent"]
