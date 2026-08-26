"""Filterable selection list with aligned descriptions."""

from __future__ import annotations

import re
from dataclasses import dataclass

from misaka.ui.tui.keybindings import getKeybindings
from misaka.ui.tui.utils import truncateToWidth, visibleWidth

DEFAULT_PRIMARY_COLUMN_WIDTH = 32
PRIMARY_COLUMN_GAP = 2
MIN_DESCRIPTION_WIDTH = 10


def _normalize_to_single_line(text: str) -> str:
    return re.sub(r"[\r\n]+", " ", text).strip()


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(value, maximum))


@dataclass(slots=True)
class SelectItem:
    value: str
    label: str
    description: str | None = None


@dataclass(slots=True)
class SelectListTheme:
    selectedPrefix: callable
    selectedText: callable
    description: callable
    scrollInfo: callable
    noMatch: callable


@dataclass(slots=True)
class SelectListTruncatePrimaryContext:
    text: str
    maxWidth: int
    columnWidth: int
    item: SelectItem
    isSelected: bool


@dataclass(slots=True)
class SelectListLayoutOptions:
    minPrimaryColumnWidth: int | None = None
    maxPrimaryColumnWidth: int | None = None
    truncatePrimary: callable | None = None


class SelectList:
    def __init__(
        self,
        items: list[SelectItem],
        maxVisible: int,
        theme: SelectListTheme,
        layout: SelectListLayoutOptions | None = None,
    ) -> None:
        self.items = items
        self.filteredItems = items
        self.selectedIndex = 0
        self.maxVisible = maxVisible
        self.theme = theme
        self.layout = layout or SelectListLayoutOptions()
        self.onSelect = None
        self.onCancel = None
        self.onSelectionChange = None

    def setFilter(self, filter: str) -> None:
        lowered = filter.lower()
        self.filteredItems = [item for item in self.items if item.value.lower().startswith(lowered)]
        self.selectedIndex = 0

    def setSelectedIndex(self, index: int) -> None:
        self.selectedIndex = max(0, min(index, len(self.filteredItems) - 1))

    def invalidate(self) -> None:
        return None

    def render(self, width: int) -> list[str]:
        lines: list[str] = []
        if len(self.filteredItems) == 0:
            lines.append(self.theme.noMatch("  No matching commands"))
            return lines

        primary_column_width = self.getPrimaryColumnWidth()
        start_index = max(
            0,
            min(self.selectedIndex - (self.maxVisible // 2), len(self.filteredItems) - self.maxVisible),
        )
        end_index = min(start_index + self.maxVisible, len(self.filteredItems))

        for index in range(start_index, end_index):
            item = self.filteredItems[index]
            description_single_line = _normalize_to_single_line(item.description) if item.description else None
            lines.append(
                self.renderItem(item, index == self.selectedIndex, width, description_single_line, primary_column_width)
            )

        if start_index > 0 or end_index < len(self.filteredItems):
            scroll_text = f"  ({self.selectedIndex + 1}/{len(self.filteredItems)})"
            lines.append(self.theme.scrollInfo(truncateToWidth(scroll_text, width - 2, "")))

        return lines

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()
        if kb.matches(keyData, "tui.select.up"):
            self.selectedIndex = len(self.filteredItems) - 1 if self.selectedIndex == 0 else self.selectedIndex - 1
            self.notifySelectionChange()
        elif kb.matches(keyData, "tui.select.down"):
            self.selectedIndex = 0 if self.selectedIndex == len(self.filteredItems) - 1 else self.selectedIndex + 1
            self.notifySelectionChange()
        elif kb.matches(keyData, "tui.select.confirm"):
            selected_item = self.filteredItems[self.selectedIndex] if self.filteredItems else None
            if selected_item is not None and callable(self.onSelect):
                self.onSelect(selected_item)
        elif kb.matches(keyData, "tui.select.cancel"):
            if callable(self.onCancel):
                self.onCancel()

    def renderItem(
        self,
        item: SelectItem,
        isSelected: bool,
        width: int,
        descriptionSingleLine: str | None,
        primaryColumnWidth: int,
    ) -> str:
        prefix = "→ " if isSelected else "  "
        prefix_width = visibleWidth(prefix)

        if descriptionSingleLine and width > 40:
            effective_primary_column_width = max(1, min(primaryColumnWidth, width - prefix_width - 4))
            max_primary_width = max(1, effective_primary_column_width - PRIMARY_COLUMN_GAP)
            truncated_value = self.truncatePrimary(item, isSelected, max_primary_width, effective_primary_column_width)
            truncated_value_width = visibleWidth(truncated_value)
            spacing = " " * max(1, effective_primary_column_width - truncated_value_width)
            description_start = prefix_width + truncated_value_width + len(spacing)
            remaining_width = width - description_start - 2

            if remaining_width > MIN_DESCRIPTION_WIDTH:
                truncated_desc = truncateToWidth(descriptionSingleLine, remaining_width, "")
                if isSelected:
                    return self.theme.selectedText(f"{prefix}{truncated_value}{spacing}{truncated_desc}")
                desc_text = self.theme.description(spacing + truncated_desc)
                return prefix + truncated_value + desc_text

        max_width = width - prefix_width - 2
        truncated_value = self.truncatePrimary(item, isSelected, max_width, max_width)
        if isSelected:
            return self.theme.selectedText(f"{prefix}{truncated_value}")
        return prefix + truncated_value

    def getPrimaryColumnWidth(self) -> int:
        bounds = self.getPrimaryColumnBounds()
        widest_primary = 0
        for item in self.filteredItems:
            widest_primary = max(widest_primary, visibleWidth(self.getDisplayValue(item)) + PRIMARY_COLUMN_GAP)
        return _clamp(widest_primary, bounds["min"], bounds["max"])

    def getPrimaryColumnBounds(self) -> dict[str, int]:
        raw_min = (
            self.layout.minPrimaryColumnWidth
            if self.layout.minPrimaryColumnWidth is not None
            else self.layout.maxPrimaryColumnWidth
            if self.layout.maxPrimaryColumnWidth is not None
            else DEFAULT_PRIMARY_COLUMN_WIDTH
        )
        raw_max = (
            self.layout.maxPrimaryColumnWidth
            if self.layout.maxPrimaryColumnWidth is not None
            else self.layout.minPrimaryColumnWidth
            if self.layout.minPrimaryColumnWidth is not None
            else DEFAULT_PRIMARY_COLUMN_WIDTH
        )
        return {"min": max(1, min(raw_min, raw_max)), "max": max(1, max(raw_min, raw_max))}

    def truncatePrimary(self, item: SelectItem, isSelected: bool, maxWidth: int, columnWidth: int) -> str:
        display_value = self.getDisplayValue(item)
        if self.layout.truncatePrimary is not None:
            truncated_value = self.layout.truncatePrimary(
                SelectListTruncatePrimaryContext(
                    text=display_value,
                    maxWidth=maxWidth,
                    columnWidth=columnWidth,
                    item=item,
                    isSelected=isSelected,
                )
            )
        else:
            truncated_value = truncateToWidth(display_value, maxWidth, "")
        return truncateToWidth(truncated_value, maxWidth, "")

    def getDisplayValue(self, item: SelectItem) -> str:
        return item.label or item.value

    def notifySelectionChange(self) -> None:
        selected_item = self.filteredItems[self.selectedIndex] if self.filteredItems else None
        if selected_item is not None and callable(self.onSelectionChange):
            self.onSelectionChange(selected_item)

    def getSelectedItem(self) -> SelectItem | None:
        return self.filteredItems[self.selectedIndex] if self.filteredItems else None


__all__ = [
    "SelectItem",
    "SelectList",
    "SelectListLayoutOptions",
    "SelectListTheme",
    "SelectListTruncatePrimaryContext",
]
