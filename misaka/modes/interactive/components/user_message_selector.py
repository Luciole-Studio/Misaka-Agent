"""User-message selection UI for session forking flows."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from misaka.tui import Container, Spacer, Text, getKeybindings, truncateToWidth

from misaka.modes.interactive.components.dynamic_border import DynamicBorder
from misaka.modes.interactive.theme.theme import theme


@dataclass(slots=True)
class UserMessageItem:
    id: str
    text: str
    timestamp: str | None = None


class UserMessageList:
    def __init__(self, messages: list[UserMessageItem], initialSelectedId: str | None = None) -> None:
        self.messages = messages
        initial_index = (
            next((index for index, message in enumerate(messages) if message.id == initialSelectedId), -1)
            if initialSelectedId is not None
            else -1
        )
        self.selectedIndex = initial_index if initial_index >= 0 else max(0, len(messages) - 1)
        self.maxVisible = 10
        self.onSelect: Callable[[str], None] | None = None
        self.onCancel: Callable[[], None] | None = None

    def invalidate(self) -> None:
        return None

    def render(self, width: int) -> list[str]:
        if not self.messages:
            return [theme.fg("muted", "  No user messages found")]

        lines: list[str] = []
        start_index = max(
            0,
            min(self.selectedIndex - (self.maxVisible // 2), len(self.messages) - self.maxVisible),
        )
        end_index = min(start_index + self.maxVisible, len(self.messages))

        for index in range(start_index, end_index):
            message = self.messages[index]
            is_selected = index == self.selectedIndex
            normalized = message.text.replace("\n", " ").strip()

            cursor = theme.fg("accent", "› ") if is_selected else "  "
            truncated = truncateToWidth(normalized, width - 2)
            lines.append(cursor + (theme.bold(truncated) if is_selected else truncated))

            position = f"  Message {index + 1} of {len(self.messages)}"
            lines.append(theme.fg("muted", position))
            lines.append("")

        if start_index > 0 or end_index < len(self.messages):
            lines.append(theme.fg("muted", f"  ({self.selectedIndex + 1}/{len(self.messages)})"))
        return lines

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()
        if kb.matches(keyData, "tui.select.up"):
            self.selectedIndex = len(self.messages) - 1 if self.selectedIndex == 0 else self.selectedIndex - 1
        elif kb.matches(keyData, "tui.select.down"):
            self.selectedIndex = 0 if self.selectedIndex == len(self.messages) - 1 else self.selectedIndex + 1
        elif kb.matches(keyData, "tui.select.confirm"):
            selected = self.messages[self.selectedIndex]
            if callable(self.onSelect):
                self.onSelect(selected.id)
        elif kb.matches(keyData, "tui.select.cancel") and callable(self.onCancel):
            self.onCancel()


class UserMessageSelectorComponent(Container):
    def __init__(
        self,
        messages: list[UserMessageItem],
        onSelect: Callable[[str], None],
        onCancel: Callable[[], None],
        initialSelectedId: str | None = None,
    ) -> None:
        super().__init__()
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

        self.messageList = UserMessageList(messages, initialSelectedId)
        self.messageList.onSelect = onSelect
        self.messageList.onCancel = onCancel
        self.addChild(self.messageList)

        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())

        if not messages:
            timer = threading.Timer(0.1, onCancel)
            timer.daemon = True
            timer.start()

    def handleInput(self, data: str) -> None:
        self.messageList.handleInput(data)

    def getMessageList(self) -> UserMessageList:
        return self.messageList


__all__ = ["UserMessageItem", "UserMessageSelectorComponent"]
