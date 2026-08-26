"""Bordered selector for interactive thinking-level changes."""

from __future__ import annotations

from collections.abc import Callable

from misaka.agent.types import ThinkingLevel
from misaka.ui.tui import Container, SelectItem, SelectList, SelectListLayoutOptions

from misaka.ui.tui.interactive.components.dynamic_border import DynamicBorder
from misaka.ui.tui.interactive.theme.theme import get_select_list_theme

THINKING_SELECT_LIST_LAYOUT = SelectListLayoutOptions(minPrimaryColumnWidth=12, maxPrimaryColumnWidth=32)

# Budget-based models: the token counts are real (`thinking.budget_tokens`).
# There is no xhigh budget tier, so it clamps to high.
LEVEL_DESCRIPTIONS: dict[ThinkingLevel, str] = {
    "off": "No reasoning",
    "minimal": "Very brief reasoning (~1k tokens)",
    "low": "Light reasoning (~2k tokens)",
    "medium": "Moderate reasoning (~8k tokens)",
    "high": "Deep reasoning (~16k tokens)",
    "xhigh": "Maximum reasoning (budget clamps to high, ~16k)",
}

# Adaptive models send an `output_config.effort` keyword instead of a budget,
# so token counts would be misleading here; minimal and low both map to "low".
ADAPTIVE_LEVEL_DESCRIPTIONS: dict[ThinkingLevel, str] = {
    "off": "No reasoning",
    "minimal": 'Adaptive effort "low" (same as low)',
    "low": 'Adaptive effort "low"',
    "medium": 'Adaptive effort "medium"',
    "high": 'Adaptive effort "high"',
    "xhigh": "Adaptive effort from model map",
}


class ThinkingSelectorComponent(Container):
    def __init__(
        self,
        currentLevel: ThinkingLevel,
        availableLevels: list[ThinkingLevel],
        onSelect: Callable[[ThinkingLevel], None],
        onCancel: Callable[[], None],
        descriptions: dict[ThinkingLevel, str] | None = None,
    ) -> None:
        super().__init__()

        descriptions = descriptions or LEVEL_DESCRIPTIONS
        items = [
            SelectItem(value=level, label=level, description=descriptions[level])
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
