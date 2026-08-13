"""Bordered selector for interactive thinking-level changes."""

from __future__ import annotations

from collections.abc import Callable

from misaka.agent.types import ThinkingLevel
from misaka.tui import Container, SelectItem, SelectList, SelectListLayoutOptions

from misaka.modes.interactive.components.dynamic_border import DynamicBorder
from misaka.modes.interactive.theme.theme import get_select_list_theme

THINKING_SELECT_LIST_LAYOUT = SelectListLayoutOptions(minPrimaryColumnWidth=12, maxPrimaryColumnWidth=32)

LEVEL_DESCRIPTIONS: dict[ThinkingLevel, str] = {
    "off": "No reasoning",
    "minimal": "Very brief reasoning (~1k tokens)",
    "low": "Light reasoning (~2k tokens)",
    "medium": "Moderate reasoning (~8k tokens)",
    "high": "Deep reasoning (~16k tokens)",
    "xhigh": "Maximum reasoning (~32k tokens)",
}


class ThinkingSelectorComponent(Container):
    def __init__(
        self,
        currentLevel: ThinkingLevel,
        availableLevels: list[ThinkingLevel],
        onSelect: Callable[[ThinkingLevel], None],
        onCancel: Callable[[], None],
    ) -> None:
        super().__init__()

        items = [
            SelectItem(value=level, label=level, description=LEVEL_DESCRIPTIONS[level])
            for level in availableLevels
        ]

        self.addChild(DynamicBorder())
        self.selectList = SelectList(items, len(items), get_select_list_theme(), THINKING_SELECT_LIST_LAYOUT)
        current_index = next((index for index, item in enumerate(items) if item.value == currentLevel), -1)
        if current_index >= 0:
            self.selectList.setSelectedIndex(current_index)
        self.selectList.onSelect = lambda item: onSelect(item.value)
        self.selectList.onCancel = onCancel
        self.addChild(self.selectList)
        self.addChild(DynamicBorder())

    def handleInput(self, data: str) -> None:
        self.selectList.handleInput(data)

    def getSelectList(self) -> SelectList:
        return self.selectList


__all__ = [
    "ThinkingSelectorComponent",
]
