"""Translated from pi-tui's ``components/settings-list.ts`` (v1.1.0).

MISAKA fork: no alt screen (D1), so ``mousePressedIndex`` and ``handleMouse`` are not ported; only the alt screen
produces mouse events.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypedDict

from misaka.ui.tui.fuzzy import fuzzyFilter
from misaka.ui.tui.keybindings import getKeybindings
from misaka.ui.tui.tui import Component
from misaka.ui.tui.utils import truncateToWidth, visibleWidth, wrapTextWithAnsi
from misaka.utils.js import jsIndex

from .input import Input


class SubmenuDoneOptions(TypedDict, total=False):
    """MISAKA fork: a name for the inline type of ``done``'s ``options`` in ``SettingItem.submenu``."""

    navigateTo: str


@dataclass(slots=True, kw_only=True)
class SettingItem:
    id: str
    """Unique identifier for this setting"""
    label: str
    """Display label (left side)"""
    description: str | None = None
    """Optional description shown when selected"""
    currentValue: str
    """Current value to display (right side)"""
    values: list[str] | None = None
    """If provided, Enter/Space cycles through these values"""
    submenu: Callable[[str, Callable[[str | None, SubmenuDoneOptions | None], None]], Component] | None = None
    """If provided, Enter opens this submenu. Receives current value and done callback.
    done() accepts an optional selectedValue and an optional navigateTo id to move the cursor after close."""


@dataclass(slots=True)
class SettingsListTheme:
    label: Callable[[str, bool], str]
    value: Callable[[str, bool], str]
    description: Callable[[str], str]
    cursor: str
    hint: Callable[[str], str]


@dataclass(slots=True)
class SettingsListOptions:
    enableSearch: bool | None = None


class _VisibleRange(TypedDict):
    startIndex: int
    endIndex: int


class SettingsList(Component):
    def __init__(
        self,
        items: list[SettingItem],
        maxVisible: int,
        theme: SettingsListTheme,
        onChange: Callable[[str, str], None],
        onCancel: Callable[[], None],
        options: SettingsListOptions | None = None,
    ) -> None:
        if options is None:
            options = SettingsListOptions()
        self.selectedIndex = 0
        self.searchInput: Input | None = None

        # Submenu state
        self.submenuComponent: Component | None = None
        self.submenuItemIndex: int | None = None
        self.navigateAfterClose: str | None = None

        self.items = items
        self.filteredItems = items
        self.maxVisible = maxVisible
        self.theme = theme
        self.onChange = onChange
        self.onCancel = onCancel
        self.searchEnabled = options.enableSearch if options.enableSearch is not None else False
        if self.searchEnabled:
            self.searchInput = Input()

    def updateValue(self, id: str, newValue: str) -> None:
        """Update an item's currentValue"""
        item = next((i for i in self.items if i.id == id), None)
        if item is not None:
            item.currentValue = newValue

    def selectItem(self, id: str) -> None:
        """Move selection to the item with the given id (no-op if not found)."""
        items = self.filteredItems if self.searchEnabled else self.items
        index = next((index for index, i in enumerate(items) if i.id == id), -1)
        if index != -1:
            self.selectedIndex = index

    def invalidate(self) -> None:
        invalidate = getattr(self.submenuComponent, "invalidate", None)
        if callable(invalidate):
            invalidate()

    def render(self, width: int) -> list[str]:
        # If submenu is active, render it instead
        if self.submenuComponent is not None:
            return self.submenuComponent.render(width)

        return self.renderMainList(width)

    def renderMainList(self, width: int) -> list[str]:
        lines: list[str] = []

        if self.searchEnabled and self.searchInput is not None:
            lines.extend(self.searchInput.render(width))
            lines.append("")

        if len(self.items) == 0:
            lines.append(self.theme.hint("  No settings available"))
            if self.searchEnabled:
                self.addHintLine(lines, width)
            return lines

        displayItems = self.getDisplayItems()
        if len(displayItems) == 0:
            lines.append(truncateToWidth(self.theme.hint("  No matching settings"), width))
            self.addHintLine(lines, width)
            return lines

        # Calculate visible range with scrolling
        visibleRange = self.getVisibleRange(displayItems)
        startIndex, endIndex = visibleRange["startIndex"], visibleRange["endIndex"]

        # Calculate max label width for alignment
        maxLabelWidth = min(36, max(visibleWidth(item.label) for item in self.items))

        # Render visible items
        for i in range(startIndex, endIndex):
            item = jsIndex(displayItems, i)
            if item is None:
                continue

            isSelected = i == self.selectedIndex
            prefix = self.theme.cursor if isSelected else "  "
            prefixWidth = visibleWidth(prefix)

            # Pad label to align values
            labelPadded = item.label + " " * max(0, maxLabelWidth - visibleWidth(item.label))
            labelText = self.theme.label(labelPadded, isSelected)

            # Calculate space for value
            separator = "  "
            usedWidth = prefixWidth + maxLabelWidth + visibleWidth(separator)
            valueMaxWidth = width - usedWidth - 2

            valueText = self.theme.value(truncateToWidth(item.currentValue, valueMaxWidth, ""), isSelected)

            lines.append(truncateToWidth(prefix + labelText + separator + valueText, width))

        # Add scroll indicator if needed
        if startIndex > 0 or endIndex < len(displayItems):
            scrollText = f"  ({self.selectedIndex + 1}/{len(displayItems)})"
            lines.append(self.theme.hint(truncateToWidth(scrollText, width - 2, "")))

        # Add description for selected item
        selectedItem = jsIndex(displayItems, self.selectedIndex)
        if selectedItem is not None and selectedItem.description:
            lines.append("")
            wrappedDesc = wrapTextWithAnsi(selectedItem.description, width - 4)
            for line in wrappedDesc:
                lines.append(self.theme.description(f"  {line}"))

        # Add hint
        self.addHintLine(lines, width)

        return lines

    def handleInput(self, data: str) -> None:
        # If submenu is active, delegate all input to it
        # The submenu's onCancel (triggered by escape) will call done() which closes it
        if self.submenuComponent is not None:
            handleInput = getattr(self.submenuComponent, "handleInput", None)
            if callable(handleInput):
                handleInput(data)
            return

        # Main list input handling
        kb = getKeybindings()
        displayItems = self.getDisplayItems()
        if kb.matches(data, "tui.select.up"):
            if len(displayItems) == 0:
                return
            self.selectedIndex = len(displayItems) - 1 if self.selectedIndex == 0 else self.selectedIndex - 1
        elif kb.matches(data, "tui.select.down"):
            if len(displayItems) == 0:
                return
            self.selectedIndex = 0 if self.selectedIndex == len(displayItems) - 1 else self.selectedIndex + 1
        elif kb.matches(data, "tui.select.confirm") or (
            data == " "
            and (not self.searchEnabled or (self.searchInput is not None and len(self.searchInput.getValue()) == 0))
        ):
            self.activateItem()
        elif kb.matches(data, "tui.select.cancel"):
            self.onCancel()
        elif self.searchEnabled and self.searchInput is not None:
            self.searchInput.handleInput(data)
            self.applyFilter(self.searchInput.getValue())

    def getDisplayItems(self) -> list[SettingItem]:
        return self.filteredItems if self.searchEnabled else self.items

    def getVisibleRange(self, displayItems: Sequence[SettingItem]) -> _VisibleRange:
        startIndex = max(
            0,
            min(self.selectedIndex - self.maxVisible // 2, len(displayItems) - self.maxVisible),
        )
        return {"startIndex": startIndex, "endIndex": min(startIndex + self.maxVisible, len(displayItems))}

    def activateItem(self) -> None:
        item = jsIndex(self.getDisplayItems(), self.selectedIndex)
        if item is None:
            return

        if item.submenu:
            # Open submenu, passing current value so it can pre-select correctly
            self.submenuItemIndex = self.selectedIndex

            def done(selectedValue: str | None = None, options: SubmenuDoneOptions | None = None) -> None:
                if selectedValue is not None:
                    item.currentValue = selectedValue
                    self.onChange(item.id, selectedValue)
                if options and options.get("navigateTo"):
                    self.navigateAfterClose = options["navigateTo"]
                self.closeSubmenu()

            self.submenuComponent = item.submenu(item.currentValue, done)
        elif item.values and len(item.values) > 0:
            # Cycle through values
            currentIndex = item.values.index(item.currentValue) if item.currentValue in item.values else -1
            nextIndex = (currentIndex + 1) % len(item.values)
            newValue = item.values[nextIndex]
            item.currentValue = newValue
            self.onChange(item.id, newValue)

    def closeSubmenu(self) -> None:
        self.submenuComponent = None
        if self.navigateAfterClose is not None:
            id = self.navigateAfterClose
            self.navigateAfterClose = None
            self.submenuItemIndex = None
            self.selectItem(id)
            # Open the target item's submenu automatically
            self.activateItem()
        elif self.submenuItemIndex is not None:
            # Restore selection to the item that opened the submenu
            self.selectedIndex = self.submenuItemIndex
            self.submenuItemIndex = None

    def applyFilter(self, query: str) -> None:
        self.filteredItems = fuzzyFilter(self.items, query, lambda item: item.label)
        self.selectedIndex = 0

    def addHintLine(self, lines: list[str], width: int) -> None:
        lines.append("")
        lines.append(
            truncateToWidth(
                self.theme.hint(
                    "  Type to search · Enter/Space to change · Esc to cancel"
                    if self.searchEnabled
                    else "  Enter/Space to change · Esc to cancel",
                ),
                width,
            ),
        )


__all__ = [
    "SettingItem",
    "SettingsList",
    "SettingsListOptions",
    "SettingsListTheme",
    "SubmenuDoneOptions",
]
