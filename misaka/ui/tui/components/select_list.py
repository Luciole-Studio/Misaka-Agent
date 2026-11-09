"""Translated from pi-tui's ``components/select-list.ts`` (v1.1.0).

MISAKA fork: no alt screen (D1), so ``mousePressedIndex`` and ``handleMouse`` are not ported; only the alt screen
produces mouse events.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypedDict

from misaka.ui.tui.keybindings import getKeybindings
from misaka.ui.tui.tui import Component
from misaka.ui.tui.utils import truncateToWidth, visibleWidth
from misaka.utils import js_regexp
from misaka.utils.js import jsIndex, jsTrim
from misaka.utils.js_regexp import RegExp

DEFAULT_PRIMARY_COLUMN_WIDTH = 32
PRIMARY_COLUMN_GAP = 2
MIN_DESCRIPTION_WIDTH = 10


def normalizeToSingleLine(text: str) -> str:
    return jsTrim(js_regexp.replace(text, RegExp(r"[\r\n]+", "g"), " "))


def clamp(value: int, min: int, max: int) -> int:
    return builtins.max(min, builtins.min(value, max))


@dataclass(slots=True)
class SelectItem:
    value: str
    label: str
    description: str | None = None


@dataclass(slots=True)
class SelectListTheme:
    selectedPrefix: Callable[[str], str]
    selectedText: Callable[[str], str]
    description: Callable[[str], str]
    scrollInfo: Callable[[str], str]
    noMatch: Callable[[str], str]


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
    truncatePrimary: Callable[[SelectListTruncatePrimaryContext], str] | None = None


class _VisibleRange(TypedDict):
    startIndex: int
    endIndex: int


class _PrimaryColumnBounds(TypedDict):
    min: int
    max: int


class SelectList(Component):
    def __init__(
        self,
        items: list[SelectItem],
        maxVisible: int,
        theme: SelectListTheme,
        layout: SelectListLayoutOptions | None = None,
    ) -> None:
        self.selectedIndex: int = 0

        self.onSelect: Callable[[SelectItem], None] | None = None
        self.onCancel: Callable[[], None] | None = None
        self.onSelectionChange: Callable[[SelectItem], None] | None = None

        self.items = items
        self.filteredItems = items
        self.maxVisible = maxVisible
        self.theme = theme
        self.layout = layout if layout is not None else SelectListLayoutOptions()

    def setFilter(self, filter: str) -> None:
        self.filteredItems = [item for item in self.items if item.value.lower().startswith(filter.lower())]
        # Reset selection when filter changes
        self.selectedIndex = 0

    def setSelectedIndex(self, index: int) -> None:
        self.selectedIndex = max(0, min(index, len(self.filteredItems) - 1))

    def invalidate(self) -> None:
        # No cached state to invalidate currently
        pass

    def render(self, width: int) -> list[str]:
        lines: list[str] = []

        # If no items match filter, show message
        if len(self.filteredItems) == 0:
            lines.append(self.theme.noMatch("  No matching commands"))
            return lines

        primaryColumnWidth = self.getPrimaryColumnWidth()

        # Calculate visible range with scrolling
        visibleRange = self.getVisibleRange()
        startIndex, endIndex = visibleRange["startIndex"], visibleRange["endIndex"]

        # Render visible items
        for i in range(startIndex, endIndex):
            item = jsIndex(self.filteredItems, i)
            if item is None:
                continue

            isSelected = i == self.selectedIndex
            descriptionSingleLine = normalizeToSingleLine(item.description) if item.description else None
            lines.append(self.renderItem(item, isSelected, width, descriptionSingleLine, primaryColumnWidth))

        # Add scroll indicators if needed
        if startIndex > 0 or endIndex < len(self.filteredItems):
            scrollText = f"  ({self.selectedIndex + 1}/{len(self.filteredItems)})"
            # Truncate if too long for terminal
            lines.append(self.theme.scrollInfo(truncateToWidth(scrollText, width - 2, "")))

        return lines

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()
        # Up arrow - wrap to bottom when at top
        if kb.matches(keyData, "tui.select.up"):
            self.selectedIndex = len(self.filteredItems) - 1 if self.selectedIndex == 0 else self.selectedIndex - 1
            self.notifySelectionChange()
        # Down arrow - wrap to top when at bottom
        elif kb.matches(keyData, "tui.select.down"):
            self.selectedIndex = 0 if self.selectedIndex == len(self.filteredItems) - 1 else self.selectedIndex + 1
            self.notifySelectionChange()
        # Enter
        elif kb.matches(keyData, "tui.select.confirm"):
            selectedItem = jsIndex(self.filteredItems, self.selectedIndex)
            if selectedItem is not None and self.onSelect:
                self.onSelect(selectedItem)
        # Escape or Ctrl+C
        elif kb.matches(keyData, "tui.select.cancel"):
            if self.onCancel:
                self.onCancel()

    def getVisibleRange(self) -> _VisibleRange:
        startIndex = max(
            0,
            min(self.selectedIndex - self.maxVisible // 2, len(self.filteredItems) - self.maxVisible),
        )
        return {
            "startIndex": startIndex,
            "endIndex": min(startIndex + self.maxVisible, len(self.filteredItems)),
        }

    def renderItem(
        self,
        item: SelectItem,
        isSelected: bool,
        width: int,
        descriptionSingleLine: str | None,
        primaryColumnWidth: int,
    ) -> str:
        prefix = "→ " if isSelected else "  "
        prefixWidth = visibleWidth(prefix)

        if descriptionSingleLine and width > 40:
            effectivePrimaryColumnWidth = max(1, min(primaryColumnWidth, width - prefixWidth - 4))
            maxPrimaryWidth = max(1, effectivePrimaryColumnWidth - PRIMARY_COLUMN_GAP)
            truncatedValue = self.truncatePrimary(item, isSelected, maxPrimaryWidth, effectivePrimaryColumnWidth)
            truncatedValueWidth = visibleWidth(truncatedValue)
            spacing = " " * max(1, effectivePrimaryColumnWidth - truncatedValueWidth)
            descriptionStart = prefixWidth + truncatedValueWidth + len(spacing)
            remainingWidth = width - descriptionStart - 2  # -2 for safety

            if remainingWidth > MIN_DESCRIPTION_WIDTH:
                truncatedDesc = truncateToWidth(descriptionSingleLine, remainingWidth, "")
                if isSelected:
                    return self.theme.selectedText(f"{prefix}{truncatedValue}{spacing}{truncatedDesc}")

                descText = self.theme.description(spacing + truncatedDesc)
                return prefix + truncatedValue + descText

        maxWidth = width - prefixWidth - 2
        truncatedValue = self.truncatePrimary(item, isSelected, maxWidth, maxWidth)
        if isSelected:
            return self.theme.selectedText(f"{prefix}{truncatedValue}")

        return prefix + truncatedValue

    def getPrimaryColumnWidth(self) -> int:
        bounds = self.getPrimaryColumnBounds()
        widestPrimary = 0
        for item in self.filteredItems:
            widestPrimary = max(widestPrimary, visibleWidth(self.getDisplayValue(item)) + PRIMARY_COLUMN_GAP)

        return clamp(widestPrimary, bounds["min"], bounds["max"])

    def getPrimaryColumnBounds(self) -> _PrimaryColumnBounds:
        minWidth, maxWidth = self.layout.minPrimaryColumnWidth, self.layout.maxPrimaryColumnWidth
        rawMin = minWidth if minWidth is not None else maxWidth if maxWidth is not None else DEFAULT_PRIMARY_COLUMN_WIDTH
        rawMax = maxWidth if maxWidth is not None else minWidth if minWidth is not None else DEFAULT_PRIMARY_COLUMN_WIDTH

        return {
            "min": max(1, min(rawMin, rawMax)),
            "max": max(1, max(rawMin, rawMax)),
        }

    def truncatePrimary(self, item: SelectItem, isSelected: bool, maxWidth: int, columnWidth: int) -> str:
        displayValue = self.getDisplayValue(item)
        truncatedValue = (
            self.layout.truncatePrimary(
                SelectListTruncatePrimaryContext(
                    text=displayValue,
                    maxWidth=maxWidth,
                    columnWidth=columnWidth,
                    item=item,
                    isSelected=isSelected,
                )
            )
            if self.layout.truncatePrimary
            else truncateToWidth(displayValue, maxWidth, "")
        )

        return truncateToWidth(truncatedValue, maxWidth, "")

    def getDisplayValue(self, item: SelectItem) -> str:
        return item.label or item.value

    def notifySelectionChange(self) -> None:
        selectedItem = jsIndex(self.filteredItems, self.selectedIndex)
        if selectedItem is not None and self.onSelectionChange:
            self.onSelectionChange(selectedItem)

    def getSelectedItem(self) -> SelectItem | None:
        return jsIndex(self.filteredItems, self.selectedIndex)


__all__ = [
    "SelectItem",
    "SelectList",
    "SelectListLayoutOptions",
    "SelectListTheme",
    "SelectListTruncatePrimaryContext",
]
