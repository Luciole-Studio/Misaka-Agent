"""Translated from pi's ``modes/interactive/components/thinking-selector.ts`` (v1.1.0)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from misaka.agent.types import ThinkingLevel
from misaka.modes.interactive.theme.theme import getSelectListTheme, theme
from misaka.ui.tui import (
    Container,
    Input,
    SelectItem,
    SelectList,
    SelectListLayoutOptions,
    Spacer,
    Text,
    fuzzyFilter,
    getKeybindings,
)

from .dynamic_border import DynamicBorder
from .keybinding_hints import keyDisplayText

THINKING_SELECT_LIST_LAYOUT = SelectListLayoutOptions(
    minPrimaryColumnWidth=12,
    maxPrimaryColumnWidth=32,
)

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
    """Component that renders a thinking level selector with borders"""

    selectList: SelectList

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.searchInput.focused = value

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
        self._focused = False
        self.onSelect = onSelect
        self.onCancel = onCancel
        self.onSelectAsDefault = onSelectAsDefault

        self.allItems: list[SelectItem] = [
            SelectItem(
                value=level,
                label=f"{'✓ ' if level == currentLevel else '  '}{level}",
                description=(
                    f"{LEVEL_DESCRIPTIONS[level]} · default" if level == defaultThinkingLevel else LEVEL_DESCRIPTIONS[level]
                ),
            )
            for level in availableLevels
        ]

        # Add top border
        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))
        self.addChild(Text("Thinking Level", 0, 0))
        self.addChild(Spacer(1))
        self.addChild(Text(f"{keyDisplayText('app.thinking.cycle')} cycles thinking levels in-session", 0, 0))
        self.addChild(Spacer(1))

        self.searchInput = Input()
        self.searchInput.onSubmit = lambda _value: self.selectList.handleInput("\r")
        self.addChild(self.searchInput)
        self.addChild(Spacer(1))

        # Create selector
        self.selectList = self.buildSelectList(self.allItems, currentLevel)
        self.selectListChildIndex = len(self.children)
        self.addChild(self.selectList)
        self.addChild(Spacer(1))
        self.addChild(
            Text(
                theme.fg(
                    "dim",
                    f"  {keyDisplayText('tui.select.confirm')} to select · {keyDisplayText('app.thinking.save')} to "
                    f"set as default · {keyDisplayText('tui.select.cancel')} to cancel",
                ),
                0,
                0,
            )
        )

        # Add bottom border
        self.addChild(DynamicBorder())

    def buildSelectList(self, items: list[SelectItem], preselect: ThinkingLevel | None = None) -> SelectList:
        list = SelectList(items, max(1, len(items)), getSelectListTheme(), THINKING_SELECT_LIST_LAYOUT)
        currentIndex = next((index for index, item in enumerate(items) if item.value == preselect), -1)
        if currentIndex != -1:
            list.setSelectedIndex(currentIndex)
        list.onSelect = lambda item: self.onSelect(item.value)  # type: ignore[arg-type]
        list.onCancel = lambda: self.onCancel()
        return list

    def applyFilter(self, query: str) -> None:
        filtered = (
            fuzzyFilter(
                self.allItems,
                query,
                lambda item: f"{item.value} {item.description if item.description is not None else ''}",
            )
            if query
            else self.allItems
        )
        selectedItem = self.selectList.getSelectedItem()
        selectedValue: Any = selectedItem.value if selectedItem else None
        newList = self.buildSelectList(filtered, selectedValue)
        self.children[self.selectListChildIndex] = newList
        self.selectList = newList

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()
        if kb.matches(keyData, "app.thinking.save") and self.onSelectAsDefault:
            item = self.selectList.getSelectedItem()
            if item:
                self.onSelectAsDefault(item.value)  # type: ignore[arg-type]
            return

        isNav = (
            kb.matches(keyData, "tui.select.up")
            or kb.matches(keyData, "tui.select.down")
            or kb.matches(keyData, "tui.select.confirm")
            or kb.matches(keyData, "tui.select.cancel")
        )
        if isNav:
            self.selectList.handleInput(keyData)
            return

        self.searchInput.handleInput(keyData)
        self.applyFilter(self.searchInput.getValue())

    def getSelectList(self) -> SelectList:
        return self.selectList


__all__ = [
    "ThinkingSelectorComponent",
]
