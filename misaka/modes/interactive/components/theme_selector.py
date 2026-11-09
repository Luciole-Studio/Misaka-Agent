"""Translated from pi's ``modes/interactive/components/theme-selector.ts`` (v1.1.0)."""

from __future__ import annotations

from collections.abc import Callable

from misaka.modes.interactive.theme.theme import getAvailableThemes, getSelectListTheme
from misaka.ui.tui import Container, SelectItem, SelectList, SelectListLayoutOptions

from .dynamic_border import DynamicBorder

THEME_SELECT_LIST_LAYOUT = SelectListLayoutOptions(
    minPrimaryColumnWidth=12,
    maxPrimaryColumnWidth=32,
)


class ThemeSelectorComponent(Container):
    """Component that renders a theme selector"""

    def __init__(
        self,
        currentTheme: str,
        onSelect: Callable[[str], None],
        onCancel: Callable[[], None],
        onPreview: Callable[[str], None],
    ) -> None:
        super().__init__()
        self.onPreview = onPreview

        # Get available themes and create select items
        themes = getAvailableThemes()
        themeItems = [
            SelectItem(value=name, label=name, description="(current)" if name == currentTheme else None)
            for name in themes
        ]

        # Add top border
        self.addChild(DynamicBorder())

        # Create selector
        self.selectList = SelectList(themeItems, 10, getSelectListTheme(), THEME_SELECT_LIST_LAYOUT)

        # Preselect current theme
        currentIndex = themes.index(currentTheme) if currentTheme in themes else -1
        if currentIndex != -1:
            self.selectList.setSelectedIndex(currentIndex)

        def select(item: SelectItem) -> None:
            onSelect(item.value)

        self.selectList.onSelect = select

        def cancel() -> None:
            onCancel()

        self.selectList.onCancel = cancel

        def selectionChange(item: SelectItem) -> None:
            self.onPreview(item.value)

        self.selectList.onSelectionChange = selectionChange

        self.addChild(self.selectList)

        # Add bottom border
        self.addChild(DynamicBorder())

    def getSelectList(self) -> SelectList:
        return self.selectList


__all__ = ["ThemeSelectorComponent"]
