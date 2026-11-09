"""Searchable interactive model selector.

Translated from pi's ``modes/interactive/components/model-selector.ts``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from misaka.ai.models import modelsAreEqual
from misaka.ai.types import Model
from misaka.core.model_runtime import ModelRuntime
from misaka.modes.interactive.model_catalog_refresh import refreshModelCatalogs
from misaka.modes.interactive.model_search import (
    ModelSearchItem,
    getModelSelectorSearchText,
)
from misaka.modes.interactive.theme.theme import theme
from misaka.ui.tui import (
    Container,
    Input,
    Spacer,
    Text,
    fuzzyFilter,
    getKeybindings,
)
from misaka.utils.collation import localeSortKey
from misaka.utils.js import AbortController, jsTrim

from .dynamic_border import DynamicBorder
from .keybinding_hints import keyDisplayText, keyHint


@dataclass(slots=True)
class ModelItem:
    provider: str
    id: str
    model: Model


@dataclass(slots=True)
class ScopedModelItem:
    model: Model
    thinkingLevel: str | None = None


@dataclass(slots=True)
class DefaultModelReference:
    provider: str
    id: str

type ModelScope = Literal["all", "scoped"]


class ModelSelectorComponent(Container):
    """Component that renders a model selector with search"""

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.searchInput.focused = value

    def _request_render(self) -> None:
        requestRender = getattr(self.tui, "requestRender", None)
        if callable(requestRender):
            requestRender()

    def __init__(
        self,
        tui: Any,
        currentModel: Model | None,
        modelRuntime: ModelRuntime,
        scopedModels: Sequence[ScopedModelItem],
        onSelect: Callable[[Model], None],
        onCancel: Callable[[], None],
        initialSearchInput: str | None = None,
        onSelectAsDefault: Callable[[Model], None] | None = None,
        defaultModel: DefaultModelReference | None = None,
    ) -> None:
        super().__init__()
        # Focusable implementation - propagate to searchInput for IME cursor positioning
        self._focused = False
        self.allModels: list[ModelItem] = []
        self.scopedModelItems: list[ModelItem] = []
        self.activeModels: list[ModelItem] = []
        self.filteredModels: list[ModelItem] = []
        self.selectedIndex = 0
        self.errorMessage: str | None = None
        self.refreshStatusMessage = "Refreshing model catalogs…"
        self.refreshStatusSuccess = False
        self.scope: ModelScope = "all"
        self.scopeText: Text | None = None
        self.scopeHintText: Text | None = None
        self.refreshAbortController = AbortController()
        self.refreshTimeout: asyncio.TimerHandle | None = None
        self.closed = False

        self.tui = tui
        self.currentModel = currentModel
        self.modelRuntime = modelRuntime
        self.scopedModels = list(scopedModels)
        self.defaultModel = defaultModel
        self.scope = "scoped" if scopedModels else "all"
        self.onSelectCallback = onSelect
        self.onSelectAsDefaultCallback = onSelectAsDefault
        self.onCancelCallback = onCancel

        # Add top border
        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))

        # Add hint about model filtering
        if scopedModels:
            self.scopeText = Text(self.getScopeText(), 0, 0)
            self.addChild(self.scopeText)
            self.scopeHintText = Text(self.getScopeHintText(), 0, 0)
            self.addChild(self.scopeHintText)
        else:
            hintText = "Only showing models from configured providers. Use /login to add providers."
            self.addChild(Text(theme.fg("warning", hintText), 0, 0))
        self.addChild(Spacer(1))

        # Create search input
        self.searchInput = Input()
        if initialSearchInput:
            self.searchInput.setValue(initialSearchInput)

        def onSubmit(_value: str) -> None:
            # Enter on search input selects the first filtered item
            if self.selectedIndex < len(self.filteredModels):
                self.handleSelect(self.filteredModels[self.selectedIndex].model)

        self.searchInput.onSubmit = onSubmit
        self.addChild(self.searchInput)

        self.addChild(Spacer(1))

        # Create list container
        self.listContainer = Container()
        self.addChild(self.listContainer)

        self.addChild(Spacer(1))

        # Hint
        if self.onSelectAsDefaultCallback is not None:
            self.addChild(
                Text(
                    theme.fg(
                        "dim",
                        f"  {keyDisplayText('tui.select.confirm')} to select · {keyDisplayText('app.models.save')} to set as "
                        f"default · {keyDisplayText('tui.select.cancel')} to cancel",
                    ),
                    0,
                    0,
                )
            )

        # Add bottom border
        self.addChild(DynamicBorder())

        # Render the current snapshot immediately, then refresh in the background.
        self.loadModelsFromSnapshot()
        if initialSearchInput:
            self.filterModels(initialSearchInput)
        else:
            self.updateList()
        self._request_render()
        # MISAKA fork: a background refresh needs a running event loop; without one (a
        # selector built outside the app) the snapshot is all there is.
        try:
            asyncio.get_running_loop().create_task(self.refreshModels())
        except RuntimeError:
            pass

    def loadModelsFromSnapshot(self) -> None:
        models = [
            ModelItem(provider=model.provider, id=model.id, model=model)
            for model in self.modelRuntime.getAvailableSnapshot()
        ]
        self.allModels = self.sortModels(models)
        refreshedScoped: list[ScopedModelItem] = []
        for scoped in self.scopedModels:
            refreshed = self.modelRuntime.getModel(scoped.model.provider, scoped.model.id)
            refreshedScoped.append(
                ScopedModelItem(model=refreshed, thinkingLevel=scoped.thinkingLevel) if refreshed else scoped
            )
        self.scopedModels = refreshedScoped
        self.scopedModelItems = [
            ModelItem(provider=scoped.model.provider, id=scoped.model.id, model=scoped.model)
            for scoped in self.scopedModels
        ]
        self.activeModels = self.scopedModelItems if self.scope == "scoped" else self.allModels
        self.filteredModels = self.activeModels
        currentIndex = next(
            (index for index, item in enumerate(self.filteredModels) if modelsAreEqual(self.currentModel, item.model)),
            -1,
        )
        self.selectedIndex = (
            currentIndex if currentIndex >= 0 else min(self.selectedIndex, max(0, len(self.filteredModels) - 1))
        )

    async def refreshModels(self) -> None:
        timeoutMs = 15_000
        timedOut = False

        def onTimeout() -> None:
            nonlocal timedOut
            timedOut = True
            self.refreshAbortController.abort()

        self.refreshTimeout = asyncio.get_running_loop().call_later(timeoutMs / 1000, onTimeout)
        try:
            result = await refreshModelCatalogs(self.modelRuntime, self.refreshAbortController.signal)
            if self.closed:
                return
            self.refreshStatusMessage = ""
            if result.aborted and timedOut:
                self.errorMessage = "Model refresh timed out; showing cached models."
            elif len(result.errors) == 1:
                self.errorMessage = f"Could not refresh {next(iter(result.errors))}; showing cached models."
            elif len(result.errors) > 1:
                self.errorMessage = (
                    f"Could not refresh {len(result.errors)} model catalogs ({', '.join(result.errors)}); "
                    "showing cached models."
                )
            else:
                self.errorMessage = self.modelRuntime.getError()
                if not self.errorMessage:
                    self.refreshStatusMessage = "Model catalogs refreshed."
                    self.refreshStatusSuccess = True
            self.loadModelsFromSnapshot()
            self.filterModels(self.searchInput.getValue())
            self._request_render()
        except Exception as error:  # noqa: BLE001 - catalog failures stay in the selector
            if self.closed:
                return
            self.refreshStatusMessage = ""
            self.errorMessage = (
                "Model refresh timed out; showing cached models."
                if timedOut
                else f"Could not refresh model catalogs: {error}"
            )
            self.updateList()
            self._request_render()
        finally:
            if self.refreshTimeout is not None:
                self.refreshTimeout.cancel()

    def dispose(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.refreshTimeout is not None:
            self.refreshTimeout.cancel()
        self.refreshAbortController.abort()

    def sortModels(self, models: list[ModelItem]) -> list[ModelItem]:
        # Sort: current model first, default model second, then by provider.
        return sorted(
            models,
            key=lambda item: (
                0 if modelsAreEqual(self.currentModel, item.model) else 1,
                0 if self.isDefaultModel(item.model) else 1,
                localeSortKey(item.provider),
            ),
        )

    def getScopeText(self) -> str:
        allText = theme.fg("accent", "all") if self.scope == "all" else theme.fg("muted", "all")
        scopedText = theme.fg("accent", "scoped") if self.scope == "scoped" else theme.fg("muted", "scoped")
        return f"{theme.fg('muted', 'Scope: ')}{allText}{theme.fg('muted', ' | ')}{scopedText}"

    def getScopeHintText(self) -> str:
        return keyHint("tui.input.tab", "scope") + theme.fg("muted", " (all/scoped)")

    def isDefaultModel(self, model: Model) -> bool:
        return (
            self.defaultModel is not None
            and self.defaultModel.provider == model.provider
            and self.defaultModel.id == model.id
        )

    def isDefaultSearch(self, query: str) -> bool:
        normalized = jsTrim(query).lower()
        return len(normalized) > 0 and "default".startswith(normalized)

    def setScope(self, scope: ModelScope) -> None:
        if self.scope == scope:
            return
        self.scope = scope
        self.activeModels = self.scopedModelItems if self.scope == "scoped" else self.allModels
        currentIndex = next(
            (index for index, item in enumerate(self.activeModels) if modelsAreEqual(self.currentModel, item.model)),
            -1,
        )
        self.selectedIndex = max(currentIndex, 0)
        self.filterModels(self.searchInput.getValue())
        if self.scopeText is not None:
            self.scopeText.setText(self.getScopeText())

    def filterModels(self, query: str) -> None:
        if query:

            def searchText(item: ModelItem) -> str:
                defaultText = " default" if self.isDefaultModel(item.model) else ""
                return (
                    getModelSelectorSearchText(ModelSearchItem(id=item.id, provider=item.provider, name=item.model.name))
                    + defaultText
                )

            filtered = fuzzyFilter(self.activeModels, query, searchText)
            if self.isDefaultSearch(query):
                defaultItems = [item for item in self.activeModels if self.isDefaultModel(item.model)]
                defaultKeys = {f"{item.provider}\0{item.id}" for item in defaultItems}
                self.filteredModels = [
                    *defaultItems,
                    *[item for item in filtered if f"{item.provider}\0{item.id}" not in defaultKeys],
                ]
            else:
                self.filteredModels = filtered
        else:
            self.filteredModels = self.activeModels
        # When filtering by a query, move the selector to the top row so the best
        # match is highlighted. When the query is cleared, keep the current position
        # clamped to the (restored) list length.
        self.selectedIndex = 0 if query else min(self.selectedIndex, max(0, len(self.filteredModels) - 1))
        self.updateList()

    def updateList(self) -> None:
        self.listContainer.clear()

        maxVisible = 10
        startIndex = max(
            0,
            min(self.selectedIndex - (maxVisible // 2), len(self.filteredModels) - maxVisible),
        )
        endIndex = min(startIndex + maxVisible, len(self.filteredModels))

        # Show visible slice of filtered models
        for index in range(startIndex, endIndex):
            item = self.filteredModels[index]

            isSelected = index == self.selectedIndex
            isCurrent = modelsAreEqual(self.currentModel, item.model)
            isDefault = self.isDefaultModel(item.model)
            defaultBadge = theme.fg("muted", " · default") if isDefault else ""

            cursor = theme.fg("accent", "→ ") if isSelected else "  "
            currentMarker = theme.fg("accent", "✓ ") if isCurrent else "  "
            modelText = theme.fg("accent", item.id) if isSelected else item.id
            providerBadge = theme.fg("muted", f"[{item.provider}]")
            line = f"{cursor}{currentMarker}{modelText} {providerBadge}{defaultBadge}"

            self.listContainer.addChild(Text(line, 0, 0))

        # Add scroll indicator if needed
        if startIndex > 0 or endIndex < len(self.filteredModels):
            scrollInfo = theme.fg("muted", f"  ({self.selectedIndex + 1}/{len(self.filteredModels)})")
            self.listContainer.addChild(Text(scrollInfo, 0, 0))

        # Show error message or "no results" if empty
        if self.errorMessage:
            # Show error in red
            for line in self.errorMessage.split("\n"):
                self.listContainer.addChild(Text(theme.fg("error", line), 0, 0))
        elif not self.filteredModels:
            self.listContainer.addChild(Text(theme.fg("muted", "  No matching models"), 0, 0))
        else:
            selected = self.filteredModels[self.selectedIndex]
            self.listContainer.addChild(Spacer(1))
            self.listContainer.addChild(Text(theme.fg("muted", f"  Model Name: {selected.model.name}"), 0, 0))
        if self.refreshStatusMessage:
            self.listContainer.addChild(Spacer(1))
            self.listContainer.addChild(
                Text(
                    theme.fg("success" if self.refreshStatusSuccess else "muted", f"  {self.refreshStatusMessage}"),
                    0,
                    0,
                )
            )

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()
        if kb.matches(keyData, "tui.input.tab"):
            if self.scopedModelItems:
                nextScope: ModelScope = "scoped" if self.scope == "all" else "all"
                self.setScope(nextScope)
                if self.scopeHintText is not None:
                    self.scopeHintText.setText(self.getScopeHintText())
            return
        # Up arrow - wrap to bottom when at top
        if kb.matches(keyData, "tui.select.up"):
            if not self.filteredModels:
                return
            self.selectedIndex = len(self.filteredModels) - 1 if self.selectedIndex == 0 else self.selectedIndex - 1
            self.updateList()
        # Down arrow - wrap to top when at bottom
        elif kb.matches(keyData, "tui.select.down"):
            if not self.filteredModels:
                return
            self.selectedIndex = 0 if self.selectedIndex == len(self.filteredModels) - 1 else self.selectedIndex + 1
            self.updateList()
        # Enter
        elif kb.matches(keyData, "tui.select.confirm"):
            if self.selectedIndex < len(self.filteredModels):
                self.handleSelect(self.filteredModels[self.selectedIndex].model)
        # Escape or Ctrl+C
        elif kb.matches(keyData, "tui.select.cancel"):
            self.dispose()
            self.onCancelCallback()
        # Select and save as default
        elif kb.matches(keyData, "app.models.save") and self.onSelectAsDefaultCallback is not None:
            if self.selectedIndex < len(self.filteredModels):
                selectedModel = self.filteredModels[self.selectedIndex]
                self.dispose()
                self.onSelectAsDefaultCallback(selectedModel.model)
        # Pass everything else to search input
        else:
            self.searchInput.handleInput(keyData)
            self.filterModels(self.searchInput.getValue())

    def handleSelect(self, model: Model) -> None:
        self.dispose()
        self.onSelectCallback(model)

    def getSearchInput(self) -> Input:
        return self.searchInput


__all__ = ["DefaultModelReference", "ModelSelectorComponent", "ScopedModelItem"]
