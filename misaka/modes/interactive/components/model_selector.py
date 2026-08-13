"""Searchable interactive model selector."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from misaka.ai.models import modelsAreEqual
from misaka.ai.types import Model
from misaka.tui import Container, Input, Spacer, Text, fuzzyFilter, getKeybindings

from misaka.core.model_registry import ModelRegistry
from misaka.core.settings_manager import SettingsManager
from misaka.modes.interactive.theme.theme import theme

from .dynamic_border import DynamicBorder
from .keybinding_hints import keyHint

type ModelScope = Literal["all", "scoped"]


@dataclass(slots=True)
class ModelItem:
    provider: str
    id: str
    model: Model


@dataclass(slots=True)
class ScopedModelItem:
    model: Model
    thinkingLevel: str | None = None


class ModelSelectorComponent(Container):
    def __init__(
        self,
        tui: Any,
        currentModel: Model | None,
        settingsManager: SettingsManager,
        modelRegistry: ModelRegistry,
        scopedModels: list[ScopedModelItem],
        onSelect: Callable[[Model], None],
        onCancel: Callable[[], None],
        initialSearchInput: str | None = None,
    ) -> None:
        super().__init__()
        self._focused = False
        self.tui = tui
        self.currentModel = currentModel
        self.settingsManager = settingsManager
        self.modelRegistry = modelRegistry
        self.scopedModels = list(scopedModels)
        self.scope: ModelScope = "scoped" if scopedModels else "all"
        self.onSelectCallback = onSelect
        self.onCancelCallback = onCancel
        self.errorMessage: str | None = None
        self.allModels: list[ModelItem] = []
        self.scopedModelItems: list[ModelItem] = []
        self.activeModels: list[ModelItem] = []
        self.filteredModels: list[ModelItem] = []
        self.selectedIndex = 0

        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))

        self.scopeText: Text | None = None
        self.scopeHintText: Text | None = None
        if scopedModels:
            self.scopeText = Text(self.getScopeText(), 0, 0)
            self.addChild(self.scopeText)
            self.scopeHintText = Text(self.getScopeHintText(), 0, 0)
            self.addChild(self.scopeHintText)
        else:
            self.addChild(
                Text(
                    theme.fg("warning", "Only showing models from configured providers. Use /login to add providers."),
                    0,
                    0,
                )
            )
        self.addChild(Spacer(1))

        self.searchInput = Input()
        if initialSearchInput:
            self.searchInput.setValue(initialSearchInput)
        self.searchInput.onSubmit = lambda _value: self._submit_selected()
        self.addChild(self.searchInput)
        self.addChild(Spacer(1))

        self.listContainer = Container()
        self.addChild(self.listContainer)
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())

        self.loadModels()
        if initialSearchInput:
            self.filterModels(initialSearchInput)
        else:
            self.updateList()
        self._request_render()

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.searchInput.focused = value

    def _request_render(self) -> None:
        request_render = getattr(self.tui, "requestRender", None)
        if callable(request_render):
            request_render()

    def loadModels(self) -> None:
        self.modelRegistry.refresh()
        self.errorMessage = self.modelRegistry.getError()

        try:
            models = [
                ModelItem(provider=model.provider, id=model.id, model=model)
                for model in self.modelRegistry.getAvailable()
            ]
        except Exception as error:  # noqa: BLE001
            self.allModels = []
            self.scopedModelItems = []
            self.activeModels = []
            self.filteredModels = []
            self.errorMessage = str(error)
            return

        self.allModels = self.sortModels(models)
        refreshed_scoped: list[ScopedModelItem] = []
        for scoped in self.scopedModels:
            refreshed = self.modelRegistry.find(scoped.model.provider, scoped.model.id)
            refreshed_scoped.append(
                ScopedModelItem(model=refreshed or scoped.model, thinkingLevel=scoped.thinkingLevel)
            )
        self.scopedModels = refreshed_scoped
        self.scopedModelItems = [
            ModelItem(provider=scoped.model.provider, id=scoped.model.id, model=scoped.model)
            for scoped in self.scopedModels
        ]
        self.activeModels = self.scopedModelItems if self.scope == "scoped" else self.allModels
        self.filteredModels = self.activeModels
        current_index = next(
            (index for index, item in enumerate(self.filteredModels) if modelsAreEqual(self.currentModel, item.model)),
            -1,
        )
        self.selectedIndex = (
            current_index
            if current_index >= 0
            else min(self.selectedIndex, max(0, len(self.filteredModels) - 1))
        )

    def sortModels(self, models: list[ModelItem]) -> list[ModelItem]:
        return sorted(
            models,
            key=lambda item: (0 if modelsAreEqual(self.currentModel, item.model) else 1, item.provider),
        )

    def getScopeText(self) -> str:
        allText = theme.fg("accent", "all") if self.scope == "all" else theme.fg("muted", "all")
        scopedText = theme.fg("accent", "scoped") if self.scope == "scoped" else theme.fg("muted", "scoped")
        return f'{theme.fg("muted", "Scope: ")}{allText}{theme.fg("muted", " | ")}{scopedText}'

    def getScopeHintText(self) -> str:
        return keyHint("tui.input.tab", "scope") + theme.fg("muted", " (all/scoped)")

    def setScope(self, scope: ModelScope) -> None:
        if self.scope == scope:
            return
        self.scope = scope
        self.activeModels = self.scopedModelItems if scope == "scoped" else self.allModels
        current_index = next(
            (index for index, item in enumerate(self.activeModels) if modelsAreEqual(self.currentModel, item.model)),
            -1,
        )
        self.selectedIndex = current_index if current_index >= 0 else 0
        self.filterModels(self.searchInput.getValue())
        if self.scopeText is not None:
            self.scopeText.setText(self.getScopeText())
        if self.scopeHintText is not None:
            self.scopeHintText.setText(self.getScopeHintText())

    def filterModels(self, query: str) -> None:
        self.filteredModels = (
            fuzzyFilter(
                self.activeModels,
                query,
                lambda item: f"{item.id} {item.provider} {item.provider}/{item.id} {item.provider} {item.id}",
            )
            if query
            else self.activeModels
        )
        self.selectedIndex = min(self.selectedIndex, max(0, len(self.filteredModels) - 1))
        self.updateList()

    def updateList(self) -> None:
        self.listContainer.clear()
        maxVisible = 10
        startIndex = max(
            0,
            min(self.selectedIndex - (maxVisible // 2), len(self.filteredModels) - maxVisible),
        )
        endIndex = min(startIndex + maxVisible, len(self.filteredModels))

        for index in range(startIndex, endIndex):
            item = self.filteredModels[index]
            isSelected = index == self.selectedIndex
            isCurrent = modelsAreEqual(self.currentModel, item.model)
            providerBadge = theme.fg("muted", f"[{item.provider}]")
            checkmark = theme.fg("success", " ✓") if isCurrent else ""
            line = (
                f"{theme.fg('accent', '→ ')}{theme.fg('accent', item.id)} {providerBadge}{checkmark}"
                if isSelected
                else f"  {item.id} {providerBadge}{checkmark}"
            )
            self.listContainer.addChild(Text(line, 0, 0))

        if startIndex > 0 or endIndex < len(self.filteredModels):
            self.listContainer.addChild(
                Text(theme.fg("muted", f"  ({self.selectedIndex + 1}/{len(self.filteredModels)})"), 0, 0)
            )

        if self.errorMessage:
            for line in self.errorMessage.splitlines() or [self.errorMessage]:
                self.listContainer.addChild(Text(theme.fg("error", line), 0, 0))
            return

        if not self.filteredModels:
            self.listContainer.addChild(Text(theme.fg("muted", "  No matching models"), 0, 0))
            return

        selected = self.filteredModels[self.selectedIndex]
        self.listContainer.addChild(Spacer(1))
        self.listContainer.addChild(Text(theme.fg("muted", f"  Model Name: {selected.model.name}"), 0, 0))

    def _submit_selected(self) -> None:
        selected = self.filteredModels[self.selectedIndex] if self.filteredModels else None
        if selected is not None:
            self.handleSelect(selected.model)

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()
        if kb.matches(keyData, "tui.input.tab"):
            if self.scopedModelItems:
                self.setScope("all" if self.scope == "scoped" else "scoped")
            return
        if kb.matches(keyData, "tui.select.up"):
            if not self.filteredModels:
                return
            self.selectedIndex = len(self.filteredModels) - 1 if self.selectedIndex == 0 else self.selectedIndex - 1
            self.updateList()
            return
        if kb.matches(keyData, "tui.select.down"):
            if not self.filteredModels:
                return
            self.selectedIndex = 0 if self.selectedIndex == len(self.filteredModels) - 1 else self.selectedIndex + 1
            self.updateList()
            return
        if kb.matches(keyData, "tui.select.confirm"):
            self._submit_selected()
            return
        if kb.matches(keyData, "tui.select.cancel"):
            self.onCancelCallback()
            return
        self.searchInput.handleInput(keyData)
        self.filterModels(self.searchInput.getValue())

    def handleSelect(self, model: Model) -> None:
        self.settingsManager.setDefaultModelAndProvider(model.provider, model.id)
        self.onSelectCallback(model)

    def getSearchInput(self) -> Input:
        return self.searchInput


__all__ = ["ModelSelectorComponent"]
