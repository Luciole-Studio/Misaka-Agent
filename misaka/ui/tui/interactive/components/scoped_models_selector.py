"""Interactive selector for session-scoped model enablement and ordering."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass

from misaka.ai.types import Model
from misaka.ui.tui import (
    Container,
    Input,
    Key,
    Spacer,
    Text,
    fuzzyFilter,
    getKeybindings,
    matchesKey,
)
from misaka.ui.tui.interactive.theme.theme import theme

from .dynamic_border import DynamicBorder
from .keybinding_hints import key_text

type EnabledIds = list[str] | None


def _invoke_callback(callback_result: object) -> None:
    if not inspect.isawaitable(callback_result):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(callback_result)
    else:
        loop.create_task(callback_result)


def is_enabled(enabledIds: EnabledIds, id: str) -> bool:
    return enabledIds is None or id in enabledIds


def toggle(enabledIds: EnabledIds, id: str) -> EnabledIds:
    if enabledIds is None:
        return [id]
    if id in enabledIds:
        return [entry for entry in enabledIds if entry != id]
    return [*enabledIds, id]


def enable_all(enabledIds: EnabledIds, allIds: list[str], targetIds: list[str] | None = None) -> EnabledIds:
    if enabledIds is None:
        return None
    targets = targetIds or allIds
    result = list(enabledIds)
    for id in targets:
        if id not in result:
            result.append(id)
    return None if len(result) == len(allIds) else result


def clear_all(enabledIds: EnabledIds, allIds: list[str], targetIds: list[str] | None = None) -> EnabledIds:
    if enabledIds is None:
        return [id for id in allIds if id not in set(targetIds or [])] if targetIds else []
    targets = set(targetIds or enabledIds)
    return [id for id in enabledIds if id not in targets]


def move(enabledIds: EnabledIds, id: str, delta: int) -> EnabledIds:
    if enabledIds is None:
        return None
    current = list(enabledIds)
    try:
        index = current.index(id)
    except ValueError:
        return current
    newIndex = index + delta
    if newIndex < 0 or newIndex >= len(current):
        return current
    current[index], current[newIndex] = current[newIndex], current[index]
    return current


def get_sorted_ids(enabledIds: EnabledIds, allIds: list[str]) -> list[str]:
    if enabledIds is None:
        return list(allIds)
    enabled = set(enabledIds)
    return [*enabledIds, *[id for id in allIds if id not in enabled]]


@dataclass(slots=True)
class ModelItem:
    fullId: str
    model: Model
    enabled: bool


@dataclass(slots=True)
class ModelsConfig:
    allModels: list[Model]
    enabledModelIds: list[str] | None


@dataclass(slots=True)
class ModelsCallbacks:
    onChange: Callable[[list[str] | None], object]
    onPersist: Callable[[list[str] | None], object]
    onCancel: Callable[[], None]


class ScopedModelsSelectorComponent(Container):
    def __init__(self, config: ModelsConfig, callbacks: ModelsCallbacks) -> None:
        super().__init__()
        self._focused = False
        self.callbacks = callbacks
        self.modelsById: dict[str, Model] = {}
        self.allIds: list[str] = []
        self.enabledIds: EnabledIds = None if config.enabledModelIds is None else list(config.enabledModelIds)
        self.filteredItems: list[ModelItem] = []
        self.selectedIndex = 0
        self.maxVisible = 8
        self.isDirty = False

        for model in config.allModels:
            fullId = f"{model.provider}/{model.id}"
            self.modelsById[fullId] = model
            self.allIds.append(fullId)

        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))
        self.addChild(Text(theme.fg("accent", theme.bold("Model Configuration")), 0, 0))
        self.addChild(
            Text(theme.fg("muted", f"Session-only. {key_text('app.models.save')} to save to settings."), 0, 0)
        )
        self.addChild(Spacer(1))

        self.searchInput = Input()
        self.addChild(self.searchInput)
        self.addChild(Spacer(1))

        self.listContainer = Container()
        self.addChild(self.listContainer)
        self.addChild(Spacer(1))

        self.footerText = Text("", 0, 0)
        self.addChild(self.footerText)
        self.addChild(DynamicBorder())

        self.filteredItems = self.buildItems()
        self.updateList()
        self.footerText.setText(self.getFooterText())

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.searchInput.focused = value

    def buildItems(self) -> list[ModelItem]:
        return [
            ModelItem(fullId=id, model=self.modelsById[id], enabled=is_enabled(self.enabledIds, id))
            for id in get_sorted_ids(self.enabledIds, self.allIds)
            if id in self.modelsById
        ]

    def getFooterText(self) -> str:
        enabledCount = len(self.enabledIds) if self.enabledIds is not None else len(self.allIds)
        allEnabled = self.enabledIds is None
        countText = "all enabled" if allEnabled else f"{enabledCount}/{len(self.allIds)} enabled"
        parts = [
            f"{key_text('tui.select.confirm')} toggle",
            f"{key_text('app.models.enableAll')} all",
            f"{key_text('app.models.clearAll')} clear",
            f"{key_text('app.models.toggleProvider')} provider",
            f"{key_text('app.models.reorderUp')}/{key_text('app.models.reorderDown')} reorder",
            f"{key_text('app.models.save')} save",
            countText,
        ]
        base = theme.fg("dim", f"  {' · '.join(parts)}")
        return base + theme.fg("warning", " (unsaved)") if self.isDirty else base

    def refresh(self) -> None:
        query = self.searchInput.getValue()
        items = self.buildItems()
        self.filteredItems = (
            fuzzyFilter(items, query, lambda item: f"{item.model.id} {item.model.provider}")
            if query
            else items
        )
        # Lower bound included on purpose: a negative index is a legal Python subscript,
        # so `filteredItems[-1]` would silently hand the last row to the toggle path.
        self.selectedIndex = max(0, min(self.selectedIndex, len(self.filteredItems) - 1))
        self.updateList()
        self.footerText.setText(self.getFooterText())

    def notifyChange(self) -> None:
        _invoke_callback(self.callbacks.onChange(None if self.enabledIds is None else list(self.enabledIds)))

    def updateList(self) -> None:
        self.listContainer.clear()
        if not self.filteredItems:
            self.listContainer.addChild(Text(theme.fg("muted", "  No matching models"), 0, 0))
            return

        startIndex = max(
            0,
            min(self.selectedIndex - (self.maxVisible // 2), len(self.filteredItems) - self.maxVisible),
        )
        endIndex = min(startIndex + self.maxVisible, len(self.filteredItems))
        allEnabled = self.enabledIds is None

        for index in range(startIndex, endIndex):
            item = self.filteredItems[index]
            isSelected = index == self.selectedIndex
            prefix = theme.fg("accent", "→ ") if isSelected else "  "
            modelText = theme.fg("accent", item.model.id) if isSelected else item.model.id
            providerBadge = theme.fg("muted", f" [{item.model.provider}]")
            status = "" if allEnabled else (theme.fg("success", " ✓") if item.enabled else theme.fg("dim", " ✗"))
            self.listContainer.addChild(Text(f"{prefix}{modelText}{providerBadge}{status}", 0, 0))

        if startIndex > 0 or endIndex < len(self.filteredItems):
            self.listContainer.addChild(
                Text(theme.fg("muted", f"  ({self.selectedIndex + 1}/{len(self.filteredItems)})"), 0, 0)
            )

        selected = self.filteredItems[self.selectedIndex]
        self.listContainer.addChild(Spacer(1))
        self.listContainer.addChild(Text(theme.fg("muted", f"  Model Name: {selected.model.name}"), 0, 0))

    def handleInput(self, data: str) -> None:
        kb = getKeybindings()

        if kb.matches(data, "tui.select.up"):
            if not self.filteredItems:
                return
            self.selectedIndex = len(self.filteredItems) - 1 if self.selectedIndex == 0 else self.selectedIndex - 1
            self.updateList()
            return
        if kb.matches(data, "tui.select.down"):
            if not self.filteredItems:
                return
            self.selectedIndex = 0 if self.selectedIndex == len(self.filteredItems) - 1 else self.selectedIndex + 1
            self.updateList()
            return

        reorderUp = kb.matches(data, "app.models.reorderUp")
        reorderDown = kb.matches(data, "app.models.reorderDown")
        if reorderUp or reorderDown:
            if self.enabledIds is None or not self.filteredItems:
                return
            item = self.filteredItems[self.selectedIndex]
            if is_enabled(self.enabledIds, item.fullId):
                delta = -1 if reorderUp else 1
                currentIndex = self.enabledIds.index(item.fullId)
                newIndex = currentIndex + delta
                if 0 <= newIndex < len(self.enabledIds):
                    self.enabledIds = move(self.enabledIds, item.fullId, delta)
                    self.isDirty = True
                    self.refresh()
                    # `selectedIndex` indexes the filtered view while the reorder moved the
                    # model in the *global* order, so the two only agree without a search
                    # query. Re-find the row that was just moved instead of stepping by delta.
                    self.selectedIndex = next(
                        (
                            index
                            for index, candidate in enumerate(self.filteredItems)
                            if candidate.fullId == item.fullId
                        ),
                        self.selectedIndex,
                    )
                    self.updateList()
                    self.notifyChange()
            return

        if kb.matches(data, "tui.select.confirm"):
            if not self.filteredItems:
                return
            item = self.filteredItems[self.selectedIndex]
            self.enabledIds = toggle(self.enabledIds, item.fullId)
            self.isDirty = True
            self.refresh()
            self.notifyChange()
            return

        if kb.matches(data, "app.models.enableAll"):
            targetIds = [item.fullId for item in self.filteredItems] if self.searchInput.getValue() else None
            self.enabledIds = enable_all(self.enabledIds, self.allIds, targetIds)
            self.isDirty = True
            self.refresh()
            self.notifyChange()
            return

        if kb.matches(data, "app.models.clearAll"):
            targetIds = [item.fullId for item in self.filteredItems] if self.searchInput.getValue() else None
            self.enabledIds = clear_all(self.enabledIds, self.allIds, targetIds)
            self.isDirty = True
            self.refresh()
            self.notifyChange()
            return

        if kb.matches(data, "app.models.toggleProvider"):
            if not self.filteredItems:
                return
            item = self.filteredItems[self.selectedIndex]
            providerIds = [id for id in self.allIds if self.modelsById[id].provider == item.model.provider]
            allProviderEnabled = all(is_enabled(self.enabledIds, id) for id in providerIds)
            self.enabledIds = (
                clear_all(self.enabledIds, self.allIds, providerIds)
                if allProviderEnabled
                else enable_all(self.enabledIds, self.allIds, providerIds)
            )
            self.isDirty = True
            self.refresh()
            self.notifyChange()
            return

        if kb.matches(data, "app.models.save"):
            _invoke_callback(self.callbacks.onPersist(None if self.enabledIds is None else list(self.enabledIds)))
            self.isDirty = False
            self.footerText.setText(self.getFooterText())
            return

        if matchesKey(data, Key.ctrl("c")):
            if self.searchInput.getValue():
                self.searchInput.setValue("")
                self.refresh()
            else:
                self.callbacks.onCancel()
            return
        if matchesKey(data, Key.escape):
            self.callbacks.onCancel()
            return

        self.searchInput.handleInput(data)
        self.refresh()

    def getSearchInput(self) -> Input:
        return self.searchInput


__all__ = [
    "ModelsCallbacks",
    "ModelsConfig",
    "ScopedModelsSelectorComponent",
]
