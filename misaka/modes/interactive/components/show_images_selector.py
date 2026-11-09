"""Translated from pi's ``modes/interactive/components/show-images-selector.ts`` (v1.1.0)."""

from __future__ import annotations

from collections.abc import Callable

from misaka.modes.interactive.theme.theme import getSelectListTheme
from misaka.ui.tui import Container, SelectItem, SelectList, SelectListLayoutOptions

from .dynamic_border import DynamicBorder

SHOW_IMAGES_SELECT_LIST_LAYOUT = SelectListLayoutOptions(
    minPrimaryColumnWidth=12,
    maxPrimaryColumnWidth=32,
)


class ShowImagesSelectorComponent(Container):
    """Component that renders a show images selector with borders"""

    def __init__(self, currentValue: bool, onSelect: Callable[[bool], None], onCancel: Callable[[], None]) -> None:
        super().__init__()

        items = [
            SelectItem(value="yes", label="Yes", description="Show images inline in terminal"),
            SelectItem(value="no", label="No", description="Show text placeholder instead"),
        ]

        # Add top border
        self.addChild(DynamicBorder())

        # Create selector
        self.selectList = SelectList(items, 5, getSelectListTheme(), SHOW_IMAGES_SELECT_LIST_LAYOUT)

        # Preselect current value
        self.selectList.setSelectedIndex(0 if currentValue else 1)

        def select(item: SelectItem) -> None:
            onSelect(item.value == "yes")

        self.selectList.onSelect = select

        def cancel() -> None:
            onCancel()

        self.selectList.onCancel = cancel

        self.addChild(self.selectList)

        # Add bottom border
        self.addChild(DynamicBorder())

    def getSelectList(self) -> SelectList:
        return self.selectList


__all__ = ["ShowImagesSelectorComponent"]
