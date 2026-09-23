"""Bordered selector for interactive thinking-level changes."""

from __future__ import annotations

from collections.abc import Callable

from misaka.agent.types import ThinkingLevel
from misaka.ui.tui import (
    Container,
    SelectItem,
    SelectList,
    SelectListLayoutOptions,
    Spacer,
    Text,
    matchesKey,
)
from misaka.ui.tui.interactive.components.dynamic_border import DynamicBorder
from misaka.ui.tui.interactive.theme.theme import get_select_list_theme, theme

THINKING_SELECT_LIST_LAYOUT = SelectListLayoutOptions(minPrimaryColumnWidth=12, maxPrimaryColumnWidth=32)

# Pi b03a367a4f, thinking-selector.ts: LEVEL_DESCRIPTIONS (word-for-word).
LEVEL_DESCRIPTIONS: dict[ThinkingLevel, str] = {
    "off": "No reasoning",
    "minimal": "Very brief reasoning (~1k tokens)",
    "low": "Light reasoning (~2k tokens)",
    "medium": "Moderate reasoning (~8k tokens)",
    "high": "Deep reasoning (~16k tokens)",
    "xhigh": "Extra-high reasoning (~32k tokens)",
    "max": "Maximum reasoning",
}


class ThinkingSelectorComponent(Container):
    def __init__(
        self,
        currentLevel: ThinkingLevel,
        availableLevels: list[ThinkingLevel],
        onSelect: Callable[[ThinkingLevel], None],
        onCancel: Callable[[], None],
        onSelectAsDefault: Callable[[ThinkingLevel], None] | None = None,
        defaultThinkingLevel: ThinkingLevel | None = None,
    ) -> None:
        super().__init__()

        items = [
            SelectItem(
                value=level,
                label=level,
                description=(
                    f"{LEVEL_DESCRIPTIONS[level]} · default"
                    if level == defaultThinkingLevel
                    else LEVEL_DESCRIPTIONS[level]
                ),
            )
            for level in availableLevels
        ]
        self.onSelectAsDefault = onSelectAsDefault

        self.addChild(DynamicBorder())
        self.selectList = SelectList(items, len(items), get_select_list_theme(), THINKING_SELECT_LIST_LAYOUT)
        current_index = next((index for index, item in enumerate(items) if item.value == currentLevel), -1)
        if current_index >= 0:
            self.selectList.setSelectedIndex(current_index)
        self.selectList.onSelect = lambda item: onSelect(item.value)
        self.selectList.onCancel = onCancel
        self.addChild(self.selectList)
        if self.onSelectAsDefault is not None:
            self.addChild(Spacer(1))
            self.addChild(Text(theme.fg("dim", "  Enter to select · Ctrl+S to set as default · Esc to cancel"), 0, 0))
        self.addChild(DynamicBorder())

    def handleInput(self, data: str) -> None:
        if matchesKey(data, "ctrl+s") and self.onSelectAsDefault is not None:
            selected = self.selectList.getSelectedItem()
            if selected is not None:
                self.onSelectAsDefault(selected.value)
            return
        self.selectList.handleInput(data)

    def getSelectList(self) -> SelectList:
        return self.selectList


__all__ = [
    "ThinkingSelectorComponent",
]
