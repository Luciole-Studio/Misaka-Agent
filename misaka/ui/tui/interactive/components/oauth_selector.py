"""Searchable provider selector for login/logout flows."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from misaka.tui import Container, Input, Spacer, TruncatedText, fuzzyFilter, getKeybindings

from misaka.core.auth_storage import AuthStatus, AuthStorage
from misaka.modes.interactive.theme.theme import theme

from .dynamic_border import DynamicBorder


@dataclass(slots=True)
class AuthSelectorProvider:
    id: str
    name: str
    authType: Literal["oauth", "api_key"]


class OAuthSelectorComponent(Container):
    def __init__(
        self,
        mode: Literal["login", "logout"],
        authStorage: AuthStorage,
        providers: list[AuthSelectorProvider],
        onSelect: Callable[[str], None],
        onCancel: Callable[[], None],
        getAuthStatus: Callable[[str], AuthStatus] | None = None,
    ) -> None:
        super().__init__()
        self._focused = False
        self.mode = mode
        self.authStorage = authStorage
        self.getAuthStatus = getAuthStatus or authStorage.getAuthStatus
        self.allProviders = providers
        self.filteredProviders = providers
        self.selectedIndex = 0
        self.onSelectCallback = onSelect
        self.onCancelCallback = onCancel

        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))
        title = "Select provider to configure:" if mode == "login" else "Select provider to logout:"
        self.addChild(TruncatedText(theme.fg("accent", theme.bold(title)), 1, 0))
        self.addChild(Spacer(1))

        self.searchInput = Input()
        self.searchInput.onSubmit = lambda _value: self._submit_selected()
        self.addChild(self.searchInput)
        self.addChild(Spacer(1))

        self.listContainer = Container()
        self.addChild(self.listContainer)
        self.addChild(Spacer(1))
        self.addChild(DynamicBorder())

        self.filterProviders("")

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.searchInput.focused = value

    def filterProviders(self, query: str) -> None:
        self.filteredProviders = (
            fuzzyFilter(
                self.allProviders,
                query,
                lambda provider: f"{provider.name} {provider.id} {provider.authType}",
            )
            if query
            else self.allProviders
        )
        self.selectedIndex = max(0, min(self.selectedIndex, max(0, len(self.filteredProviders) - 1)))
        self.updateList()

    def updateList(self) -> None:
        self.listContainer.clear()
        maxVisible = 8
        startIndex = max(
            0,
            min(self.selectedIndex - (maxVisible // 2), len(self.filteredProviders) - maxVisible),
        )
        endIndex = min(startIndex + maxVisible, len(self.filteredProviders))

        for index in range(startIndex, endIndex):
            provider = self.filteredProviders[index]
            isSelected = index == self.selectedIndex
            statusIndicator = self.formatStatusIndicator(provider)
            line = (
                theme.fg("accent", "→ ") + theme.fg("accent", provider.name) + statusIndicator
                if isSelected
                else f"  {theme.fg('text', provider.name)}{statusIndicator}"
            )
            self.listContainer.addChild(TruncatedText(line, 1, 0))

        if startIndex > 0 or endIndex < len(self.filteredProviders):
            self.listContainer.addChild(
                TruncatedText(theme.fg("muted", f"  ({self.selectedIndex + 1}/{len(self.filteredProviders)})"), 1, 0)
            )

        if not self.filteredProviders:
            if not self.allProviders:
                message = (
                    "No providers available"
                    if self.mode == "login"
                    else "No providers logged in. Use /login first."
                )
            else:
                message = "No matching providers"
            self.listContainer.addChild(TruncatedText(theme.fg("muted", f"  {message}"), 1, 0))

    def formatStatusIndicator(self, provider: AuthSelectorProvider) -> str:
        credential = self.authStorage.get(provider.id)
        if isinstance(credential, dict) and credential.get("type") == provider.authType:
            return theme.fg("success", " ✓ configured")
        if credential:
            label = "subscription configured" if credential.get("type") == "oauth" else "API key configured"
            return theme.fg("muted", " • ") + theme.fg("warning", label)
        if provider.authType != "api_key":
            return theme.fg("muted", " • unconfigured")

        status = self.getAuthStatus(provider.id)
        match status.source:
            case "environment":
                return theme.fg("success", f" ✓ env: {status.label or 'API key'}")
            case "runtime":
                return theme.fg("success", " ✓ runtime API key")
            case "fallback":
                return theme.fg("success", " ✓ custom API key")
            case "models_json_key":
                return theme.fg("success", " ✓ key in models.json")
            case "models_json_command":
                return theme.fg("success", " ✓ command in models.json")
            case _:
                return theme.fg("muted", " • unconfigured")

    def _submit_selected(self) -> None:
        selected = self.filteredProviders[self.selectedIndex] if self.filteredProviders else None
        if selected is not None:
            self.onSelectCallback(selected.id)

    def handleInput(self, keyData: str) -> None:
        kb = getKeybindings()
        if kb.matches(keyData, "tui.select.up"):
            if not self.filteredProviders:
                return
            self.selectedIndex = max(0, self.selectedIndex - 1)
            self.updateList()
            return
        if kb.matches(keyData, "tui.select.down"):
            if not self.filteredProviders:
                return
            self.selectedIndex = min(len(self.filteredProviders) - 1, self.selectedIndex + 1)
            self.updateList()
            return
        if kb.matches(keyData, "tui.select.confirm"):
            self._submit_selected()
            return
        if kb.matches(keyData, "tui.select.cancel"):
            self.onCancelCallback()
            return

        self.searchInput.handleInput(keyData)
        self.filterProviders(self.searchInput.getValue())


__all__ = ["AuthSelectorProvider", "OAuthSelectorComponent"]
