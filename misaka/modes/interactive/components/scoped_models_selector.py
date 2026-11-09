"""Interactive selector for session-scoped model enablement and ordering.

Translated from pi's ``modes/interactive/components/scoped-models-selector.ts``.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from misaka.ai.types import Model
from misaka.modes.interactive.model_search import ModelSearchItem, getModelSearchText
from misaka.modes.interactive.theme.theme import theme
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

from .dynamic_border import DynamicBorder
from .keybinding_hints import keyDisplayText

# EnabledIds: None = all enabled (no filter), list[str] = explicit ordered list
type EnabledIds = list[str] | None

_UNSET: object = object()


def _invoke_callback(callback_result: object) -> None:
    if not inspect.isawaitable(callback_result):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(callback_result)
    else:
        loop.create_task(callback_result)


def isEnabled(enabledIds: EnabledIds, id: str) -> bool:
    return enabledIds is None or id in enabledIds


def normalizeEnabled(result: list[str], allIds: list[str]) -> EnabledIds:
    """Collapse an explicit list back to None (= all enabled) when it covers every available model."""
    return None if len(result) == len(allIds) and all(id in allIds for id in result) else result


def toggle(enabledIds: EnabledIds, allIds: list[str], id: str) -> EnabledIds:
    if enabledIds is None:
        return [modelId for modelId in allIds if modelId != id]
    if id in enabledIds:
        index = enabledIds.index(id)
        return [*enabledIds[:index], *enabledIds[index + 1:]]
    return normalizeEnabled([*enabledIds, id], allIds)


def enableAll(enabledIds: EnabledIds, allIds: list[str], targetIds: list[str] | None = None) -> EnabledIds:
    if enabledIds is None:
        return None  # Already all enabled
    targets = targetIds if targetIds is not None else allIds
    result = list(enabledIds)
    for id in targets:
        if id not in result:
            result.append(id)
    return normalizeEnabled(result, allIds)


def clearAll(enabledIds: EnabledIds, allIds: list[str], targetIds: list[str] | None = None) -> EnabledIds:
    if enabledIds is None:
        return [id for id in allIds if id not in targetIds] if targetIds is not None else []
    targets = set(targetIds if targetIds is not None else enabledIds)
    return [id for id in enabledIds if id not in targets]


def move(enabledIds: EnabledIds, id: str, delta: int) -> EnabledIds:
    if enabledIds is None:
        return None
    current = list(enabledIds)
    if id not in current:
        return current
    index = current.index(id)
    newIndex = index + delta
    if newIndex < 0 or newIndex >= len(current):
        return current
    result = list(current)
    result[index], result[newIndex] = result[newIndex], result[index]
    return result


def getSortedIds(enabledIds: EnabledIds, allIds: list[str]) -> list[str]:
    if enabledIds is None:
        return list(allIds)
    enabledSet = set(enabledIds)
    return [*enabledIds, *[id for id in allIds if id not in enabledSet]]


@dataclass(slots=True)
class ModelItem:
    fullId: str
    model: Model | None
    enabled: bool


@dataclass(slots=True)
class ModelsConfig:
    allModels: list[Model]
    enabledModelIds: list[str] | None
    refreshStatus: str | None = None


@dataclass(slots=True)
class ModelsCallbacks:
    # Called whenever the enabled model set or order changes (session-only, no persist)
    onChange: Callable[[list[str] | None], object]
    # Called when user wants to persist current selection to settings
    onPersist: Callable[[list[str] | None], object]
    onCancel: Callable[[], None]


class ScopedModelsSelectorComponent(Container):
    """Component for enabling/disabling models for Ctrl+P cycling.
    Changes are session-only until explicitly persisted with Ctrl+S.
    """

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.searchInput.focused = value

    def __init__(self, config: ModelsConfig, callbacks: ModelsCallbacks) -> None:
        super().__init__()
        self.modelsById: dict[str, Model] = {}
        self.allIds: list[str] = []
        self.enabledIds: EnabledIds = None
        self.filteredItems: list[ModelItem] = []
        self.selectedIndex = 0
        # Focusable implementation - propagate to searchInput for IME cursor positioning
        self._focused = False
        self.callbacks = callbacks
        self.maxVisible = 8
        self.isDirty = False
        self.refreshStatusText: Text | None = None

        for model in config.allModels:
            fullId = f"{model.provider}/{model.id}"
            self.modelsById[fullId] = model
            self.allIds.append(fullId)

        self.enabledIds = None if config.enabledModelIds is None else list(config.enabledModelIds)
        self.filteredItems = self.buildItems()

        # Header
        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))
        self.addChild(Text(theme.fg("accent", theme.bold("Model Configuration")), 0, 0))
        self.addChild(
            Text(theme.fg("muted", f"Session-only. {keyDisplayText('app.models.save')} to save to settings."), 0, 0)
        )
        self.addChild(Spacer(1))

        # Search input
        self.searchInput = Input()
        self.addChild(self.searchInput)
        self.addChild(Spacer(1))

        # List container
        self.listContainer = Container()
        self.addChild(self.listContainer)

        # Footer hint
        self.addChild(Spacer(1))
        if config.refreshStatus:
            self.refreshStatusText = Text(theme.fg("muted", f"  {config.refreshStatus}"), 0, 0)
            self.addChild(self.refreshStatusText)
        self.footerText = Text(self.getFooterText(), 0, 0)
        self.addChild(self.footerText)

        self.addChild(DynamicBorder())
        self.updateList()

    def updateModels(self, models: Sequence[Model], enabledModelIds: list[str] | None | object = _UNSET) -> None:
        selectedId = (
            self.filteredItems[self.selectedIndex].fullId if self.selectedIndex < len(self.filteredItems) else None
        )
        if enabledModelIds is not _UNSET:
            self.enabledIds = None if enabledModelIds is None else list(enabledModelIds)  # type: ignore[call-overload]
        self.modelsById.clear()
        self.allIds = []
        for model in models:
            fullId = f"{model.provider}/{model.id}"
            self.modelsById[fullId] = model
            self.allIds.append(fullId)
        self.refresh()
        refreshedIndex = (
            next((index for index, item in enumerate(self.filteredItems) if item.fullId == selectedId), -1)
            if selectedId
            else -1
        )
        if refreshedIndex >= 0:
            self.selectedIndex = refreshedIndex
            self.updateList()

    def setRefreshStatus(self, message: str, kind: Literal["muted", "success", "warning"]) -> None:
        if self.refreshStatusText is not None:
            self.refreshStatusText.setText(theme.fg(kind, f"  {message}"))

    def buildItems(self) -> list[ModelItem]:
        return [
            ModelItem(fullId=id, model=self.modelsById.get(id), enabled=isEnabled(self.enabledIds, id))
            for id in getSortedIds(self.enabledIds, self.allIds)
        ]

    def getFooterText(self) -> str:
        enabledCount = (
            len([id for id in self.enabledIds if id in self.modelsById])
            if self.enabledIds is not None
            else len(self.allIds)
        )
        unavailableCount = (
            len([id for id in self.enabledIds if id not in self.modelsById]) if self.enabledIds is not None else 0
        )
        allEnabled = self.enabledIds is None
        countText = (
            "all enabled"
            if allEnabled
            else f"{enabledCount}/{len(self.allIds)} enabled"
            + (f" · {unavailableCount} unavailable" if unavailableCount else "")
        )
        parts = [
            f"{keyDisplayText('tui.select.confirm')} toggle",
            f"{keyDisplayText('app.models.enableAll')} all",
            f"{keyDisplayText('app.models.clearAll')} clear",
            f"{keyDisplayText('app.models.toggleProvider')} provider",
            f"{keyDisplayText('app.models.reorderUp')}/{keyDisplayText('app.models.reorderDown')} reorder",
            f"{keyDisplayText('app.models.save')} save",
            countText,
        ]
        return (
            theme.fg("dim", f"  {' · '.join(parts)} ") + theme.fg("warning", "(unsaved)")
            if self.isDirty
            else theme.fg("dim", f"  {' · '.join(parts)}")
        )

    def refresh(self) -> None:
        query = self.searchInput.getValue()
        items = self.buildItems()
        self.filteredItems = (
            fuzzyFilter(
                items,
                query,
                lambda item: getModelSearchText(
                    ModelSearchItem(id=item.model.id, provider=item.model.provider, name=item.model.name)
                )
                if item.model is not None
                else item.fullId,
            )
            if query
            else items
        )
        self.selectedIndex = min(self.selectedIndex, max(0, len(self.filteredItems) - 1))
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
        for index in range(startIndex, endIndex):
            item = self.filteredItems[index]
            isSelected = index == self.selectedIndex
            prefix = theme.fg("accent", "→ ") if isSelected else "  "
            id = item.model.id if item.model is not None else item.fullId
            styledId = id if item.model is not None else theme.strikethrough(id)
            modelText = theme.fg("accent", styledId) if isSelected else styledId
            providerBadge = theme.fg("muted", f" [{item.model.provider}]" if item.model is not None else " [unavailable]")
            status = theme.fg("accent", "✓ ") if item.model is not None and item.enabled else "  "
            self.listContainer.addChild(Text(f"{prefix}{status}{modelText}{providerBadge}", 0, 0))

        # Add scroll indicator if needed
        if startIndex > 0 or endIndex < len(self.filteredItems):
            self.listContainer.addChild(
                Text(theme.fg("muted", f"  ({self.selectedIndex + 1}/{len(self.filteredItems)})"), 0, 0)
            )

        if self.filteredItems:
            selected = self.filteredItems[self.selectedIndex]
            self.listContainer.addChild(Spacer(1))
            self.listContainer.addChild(
                Text(
                    theme.fg(
                        "muted",
                        f"  {f'Model Name: {selected.model.name}' if selected.model is not None else 'Model unavailable'}",
                    ),
                    0,
                    0,
                )
            )

    def handleInput(self, data: str) -> None:
        kb = getKeybindings()

        # Navigation
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

        # Reorder enabled models
        reorderUp = kb.matches(data, "app.models.reorderUp")
        reorderDown = kb.matches(data, "app.models.reorderDown")
        if reorderUp or reorderDown:
            if self.enabledIds is None:
                return
            item = self.filteredItems[self.selectedIndex] if self.selectedIndex < len(self.filteredItems) else None
            if item is not None and isEnabled(self.enabledIds, item.fullId):
                delta = -1 if reorderUp else 1
                currentIndex = self.enabledIds.index(item.fullId)
                newIndex = currentIndex + delta
                # Only move if within bounds
                if 0 <= newIndex < len(self.enabledIds):
                    self.enabledIds = move(self.enabledIds, item.fullId, delta)
                    self.isDirty = True
                    self.selectedIndex += delta
                    self.refresh()
                    # MISAKA fork: `selectedIndex` indexes the filtered view while the move is in
                    # the global order; the two only agree without a search query, so the moved
                    # row is found again rather than stepped to by delta.
                    self.selectedIndex = next(
                        (index for index, candidate in enumerate(self.filteredItems) if candidate.fullId == item.fullId),
                        self.selectedIndex,
                    )
                    self.updateList()
                    self.notifyChange()
            return

        # Toggle on Enter
        if kb.matches(data, "tui.select.confirm"):
            item = self.filteredItems[self.selectedIndex] if self.selectedIndex < len(self.filteredItems) else None
            if item is not None:
                self.enabledIds = toggle(self.enabledIds, self.allIds, item.fullId)
                self.isDirty = True
                self.refresh()
                self.notifyChange()
            return

        # Enable all (filtered if search active, otherwise all)
        if kb.matches(data, "app.models.enableAll"):
            targetIds = [item.fullId for item in self.filteredItems] if self.searchInput.getValue() else None
            self.enabledIds = enableAll(self.enabledIds, self.allIds, targetIds)
            self.isDirty = True
            self.refresh()
            self.notifyChange()
            return

        # Clear all (filtered if search active, otherwise all)
        if kb.matches(data, "app.models.clearAll"):
            targetIds = [item.fullId for item in self.filteredItems] if self.searchInput.getValue() else None
            self.enabledIds = clearAll(self.enabledIds, self.allIds, targetIds)
            self.isDirty = True
            self.refresh()
            self.notifyChange()
            return

        # Toggle provider of current item
        if kb.matches(data, "app.models.toggleProvider"):
            item = self.filteredItems[self.selectedIndex] if self.selectedIndex < len(self.filteredItems) else None
            if item is not None and item.model is not None:
                provider = item.model.provider
                providerIds = [id for id in self.allIds if self.modelsById[id].provider == provider]
                allEnabled = all(isEnabled(self.enabledIds, id) for id in providerIds)
                self.enabledIds = (
                    clearAll(self.enabledIds, self.allIds, providerIds)
                    if allEnabled
                    else enableAll(self.enabledIds, self.allIds, providerIds)
                )
                self.isDirty = True
                self.refresh()
                self.notifyChange()
            return

        # Save/persist to settings
        if kb.matches(data, "app.models.save"):
            _invoke_callback(self.callbacks.onPersist(None if self.enabledIds is None else list(self.enabledIds)))
            self.isDirty = False
            self.footerText.setText(self.getFooterText())
            return

        # Ctrl+C - clear search or cancel if empty
        if matchesKey(data, Key.ctrl("c")):
            if self.searchInput.getValue():
                self.searchInput.setValue("")
                self.refresh()
            else:
                self.callbacks.onCancel()
            return

        # Escape - cancel
        if matchesKey(data, Key.escape):
            self.callbacks.onCancel()
            return

        # Pass everything else to search input
        self.searchInput.handleInput(data)
        self.refresh()

    def getSearchInput(self) -> Input:
        return self.searchInput


__all__ = [
    "ModelsCallbacks",
    "ModelsConfig",
    "ScopedModelsSelectorComponent",
]
