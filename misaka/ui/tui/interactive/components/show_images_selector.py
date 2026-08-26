"""Bordered selector for the interactive show-images setting."""

from __future__ import annotations

from collections.abc import Callable

from misaka.ui.tui import Container, SelectItem, SelectList, SelectListLayoutOptions
from misaka.ui.tui.interactive.components.dynamic_border import DynamicBorder
from misaka.ui.tui.interactive.theme.theme import get_select_list_theme

SHOW_IMAGES_SELECT_LIST_LAYOUT = SelectListLayoutOptions(minPrimaryColumnWidth=12, maxPrimaryColumnWidth=32)


class ShowImagesSelectorComponent(Container):
    def __init__(self, currentValue: bool, onSelect: Callable[[bool], None], onCancel: Callable[[], None]) -> None:
        super().__init__()
        items = [
            SelectItem(value="yes", label="Yes", description="Show images inline in terminal"),
            SelectItem(value="no", label="No", description="Show text placeholder instead"),
        ]

        self.addChild(DynamicBorder())
        self.selectList = SelectList(items, 5, get_select_list_theme(), SHOW_IMAGES_SELECT_LIST_LAYOUT)
        self.selectList.setSelectedIndex(0 if currentValue else 1)
        self.selectList.onSelect = lambda item: onSelect(item.value == "yes")
        self.selectList.onCancel = onCancel
        self.addChild(self.selectList)
        self.addChild(DynamicBorder())

    def handleInput(self, data: str) -> None:
        self.selectList.handleInput(data)

    def getSelectList(self) -> SelectList:
        return self.selectList


__all__ = ["ShowImagesSelectorComponent"]
