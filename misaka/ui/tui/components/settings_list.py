"""Interactive settings list with optional search and submenus."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from misaka.ui.tui.fuzzy import fuzzyFilter
from misaka.ui.tui.keybindings import getKeybindings
from misaka.ui.tui.tui import Component
from misaka.ui.tui.utils import truncateToWidth, visibleWidth, wrapTextWithAnsi

from .input import Input

type SubmenuFactory = Callable[[str, Callable[[str | None], None]], Component]


@dataclass(slots=True)
class SettingItem:
    id: str
    label: str
    description: str | None = None
    currentValue: str = ""
    values: list[str] | None = None
    submenu: SubmenuFactory | None = None


@dataclass(slots=True)
class SettingsListTheme:
    label: Callable[[str, bool], str]
    value: Callable[[str, bool], str]
    description: Callable[[str], str]
    cursor: str
    hint: Callable[[str], str]


@dataclass(slots=True)
class SettingsListOptions:
    enableSearch: bool = False


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
        self.items = items
        self.filteredItems = items
        self.theme = theme
        self.selectedIndex = 0
        self.maxVisible = maxVisible
        self.onChange = onChange
        self.onCancel = onCancel
        self.searchEnabled = (options or SettingsListOptions()).enableSearch
        self.searchInput = Input() if self.searchEnabled else None
        self.submenuComponent: Component | None = None
        self.submenuItemIndex: int | None = None

    def updateValue(self, id: str, newValue: str) -> None:
        item = next((entry for entry in self.items if entry.id == id), None)
        if item is not None:
            item.currentValue = newValue

    def invalidate(self) -> None:
        invalidate = getattr(self.submenuComponent, "invalidate", None)
        if callable(invalidate):
            invalidate()

    def render(self, width: int) -> list[str]:
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

        display_items = self.filteredItems if self.searchEnabled else self.items
        if len(display_items) == 0:
            lines.append(truncateToWidth(self.theme.hint("  No matching settings"), width))
            self.addHintLine(lines, width)
            return lines

        start_index = max(
            0,
            min(self.selectedIndex - (self.maxVisible // 2), len(display_items) - self.maxVisible),
        )
        end_index = min(start_index + self.maxVisible, len(display_items))
        max_label_width = min(30, max((visibleWidth(item.label) for item in self.items), default=0))

        for index in range(start_index, end_index):
            item = display_items[index]
            is_selected = index == self.selectedIndex
            prefix = self.theme.cursor if is_selected else "  "
            prefix_width = visibleWidth(prefix)
            label_padded = item.label + (" " * max(0, max_label_width - visibleWidth(item.label)))
            label_text = self.theme.label(label_padded, is_selected)
            separator = "  "
            used_width = prefix_width + max_label_width + visibleWidth(separator)
            value_max_width = width - used_width - 2
            value_text = self.theme.value(truncateToWidth(item.currentValue, value_max_width, ""), is_selected)
            lines.append(truncateToWidth(prefix + label_text + separator + value_text, width))

        if start_index > 0 or end_index < len(display_items):
            scroll_text = f"  ({self.selectedIndex + 1}/{len(display_items)})"
            lines.append(self.theme.hint(truncateToWidth(scroll_text, width - 2, "")))

        selected_item = display_items[self.selectedIndex] if 0 <= self.selectedIndex < len(display_items) else None
        if selected_item is not None and selected_item.description:
            lines.append("")
            for line in wrapTextWithAnsi(selected_item.description, width - 4):
                lines.append(self.theme.description(f"  {line}"))

        self.addHintLine(lines, width)
        return lines

    def handleInput(self, data: str) -> None:
        if self.submenuComponent is not None:
            handle_input = getattr(self.submenuComponent, "handleInput", None)
            if callable(handle_input):
                handle_input(data)
            return

        kb = getKeybindings()
        display_items = self.filteredItems if self.searchEnabled else self.items

        if kb.matches(data, "tui.select.up"):
            if len(display_items) == 0:
                return
            self.selectedIndex = len(display_items) - 1 if self.selectedIndex == 0 else self.selectedIndex - 1
        elif kb.matches(data, "tui.select.down"):
            if len(display_items) == 0:
                return
            self.selectedIndex = 0 if self.selectedIndex == len(display_items) - 1 else self.selectedIndex + 1
        elif kb.matches(data, "tui.select.confirm") or (
                data == " " and (not self.searchEnabled or not (self.searchInput and self.searchInput.getValue()))):
            self.activateItem()
        elif kb.matches(data, "tui.select.cancel"):
            self.onCancel()
        elif self.searchEnabled and self.searchInput is not None:
            self.searchInput.handleInput(data)
            self.applyFilter(self.searchInput.getValue())

    def activateItem(self) -> None:
        source = self.filteredItems if self.searchEnabled else self.items
        if not (0 <= self.selectedIndex < len(source)):
            return
        item = source[self.selectedIndex]
        if item.submenu is not None:
            self.submenuItemIndex = self.selectedIndex
            self.submenuComponent = item.submenu(item.currentValue, self._finishSubmenu(item))
            return

        if item.values:
            current_index = item.values.index(item.currentValue) if item.currentValue in item.values else -1
            next_index = (current_index + 1) % len(item.values)
            new_value = item.values[next_index]
            item.currentValue = new_value
            self.onChange(item.id, new_value)

    def _finishSubmenu(self, item: SettingItem) -> Callable[[str | None], None]:
        def done(selectedValue: str | None = None) -> None:
            if selectedValue is not None:
                item.currentValue = selectedValue
                self.onChange(item.id, selectedValue)
            self.closeSubmenu()

        return done

    def closeSubmenu(self) -> None:
        self.submenuComponent = None
        if self.submenuItemIndex is not None:
            self.selectedIndex = self.submenuItemIndex
            self.submenuItemIndex = None

    def applyFilter(self, query: str) -> None:
        self.filteredItems = fuzzyFilter(self.items, query, lambda item: item.label)
        self.selectedIndex = 0

    def addHintLine(self, lines: list[str], width: int) -> None:
        lines.append("")
        hint = (
            "  Type to search · Enter/Space to change · Esc to cancel"
            if self.searchEnabled
            else "  Enter/Space to change · Esc to cancel"
        )
        lines.append(truncateToWidth(self.theme.hint(hint), width))


__all__ = [
    "SettingItem",
    "SettingsList",
    "SettingsListOptions",
    "SettingsListTheme",
]
