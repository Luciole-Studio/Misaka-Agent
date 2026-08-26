"""Bordered theme selector for interactive theme switching."""

from __future__ import annotations

from collections.abc import Callable

from misaka.ui.tui import Container, SelectItem, SelectList, SelectListLayoutOptions
from misaka.ui.tui.interactive.components.dynamic_border import DynamicBorder
from misaka.ui.tui.interactive.theme.theme import (
    get_available_themes,
    get_select_list_theme,
)

THEME_SELECT_LIST_LAYOUT = SelectListLayoutOptions(minPrimaryColumnWidth=12, maxPrimaryColumnWidth=32)


class ThemeSelectorComponent(Container):
    def __init__(
        self,
        currentTheme: str,
        onSelect: Callable[[str], None],
        onCancel: Callable[[], None],
        onPreview: Callable[[str], None],
    ) -> None:
        super().__init__()
        themes = get_available_themes()
        items = [
            SelectItem(
                value=name,
                label=name,
                description="(current)" if name == currentTheme else None,
            )
            for name in themes
        ]

        self.addChild(DynamicBorder())
        self.selectList = SelectList(items, 10, get_select_list_theme(), THEME_SELECT_LIST_LAYOUT)
        current_index = themes.index(currentTheme) if currentTheme in themes else -1
        if current_index >= 0:
            self.selectList.setSelectedIndex(current_index)
        self.selectList.onSelect = lambda item: onSelect(item.value)
        self.selectList.onCancel = onCancel
        self.selectList.onSelectionChange = lambda item: onPreview(item.value)
        self.addChild(self.selectList)
        self.addChild(DynamicBorder())

    def handleInput(self, data: str) -> None:
        self.selectList.handleInput(data)

    def getSelectList(self) -> SelectList:
        return self.selectList


__all__ = ["ThemeSelectorComponent"]
