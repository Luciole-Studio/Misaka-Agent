"""Searchable provider selector for login/logout flows.

Translated from pi's ``coding-agent/src/modes/interactive/components/oauth-selector.ts``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from misaka.ai.auth.types import AuthCheck
from misaka.modes.interactive.theme.theme import theme
from misaka.ui.tui import (
    Container,
    Input,
    Spacer,
    TruncatedText,
    fuzzyFilter,
    getKeybindings,
)

from .dynamic_border import DynamicBorder

_ENV_NAMES = re.compile(r"[A-Z][A-Z0-9_]*(?:, [A-Z][A-Z0-9_]*)*")


@dataclass(slots=True)
class AuthSelectorProvider:
    id: str
    name: str
    authType: Literal["oauth", "api_key"]
    # The provider's ApiKeyAuth or OAuthAuth.
    method: Any = None
    status: AuthCheck | None = None
    # Whether the provider's OAuth sign-in is backed by a subscription. `False` labels it as an
    # account; unset keeps the "subscription" label.
    subscription: bool | None = None


def formatAuthSelectorProviderType(authType: str, subscription: bool | None = None) -> str:
    if authType == "api_key":
        return "API key"
    return "account" if subscription is False else "subscription"


def formatAuthSelectorProviderStatus(provider: AuthSelectorProvider) -> str:
    """Themed suffix describing whether and how a login option is configured, for example " ✓ configured"."""
    if not provider.status:
        return theme.fg("muted", " • not configured")
    if provider.status.type != provider.authType:
        label = f"{formatAuthSelectorProviderType(provider.status.type, provider.subscription)} configured"
        return theme.fg("muted", " • ") + theme.fg("warning", label)
    if not provider.status.source or provider.status.source in ("OAuth", "stored credential"):
        return theme.fg("success", " ✓ configured")
    source = f"env: {provider.status.source}" if _ENV_NAMES.fullmatch(provider.status.source) else provider.status.source
    return theme.fg("success", f" ✓ {source}")


class OAuthSelectorComponent(Container):
    """Component that renders an auth provider selector."""

    @property
    def focused(self) -> bool:
        return self._focused

    @focused.setter
    def focused(self, value: bool) -> None:
        self._focused = value
        self.searchInput.focused = value

    def __init__(
        self,
        mode: Literal["login", "logout"],
        providers: list[AuthSelectorProvider],
        onSelect: Callable[[str, str], Any],
        onCancel: Callable[[], None],
        initialSearchInput: str | None = None,
    ) -> None:
        super().__init__()
        self._focused = False
        self.mode = mode
        self.allProviders = providers
        self.filteredProviders = providers
        self.selectedIndex = 0
        self.showAuthTypeLabels = len({provider.authType for provider in providers}) > 1
        self.onSelectCallback = onSelect
        self.onCancelCallback = onCancel

        self.addChild(DynamicBorder())
        self.addChild(Spacer(1))
        title = "Select provider to configure:" if mode == "login" else "Select provider to logout:"
        self.addChild(TruncatedText(theme.fg("accent", theme.bold(title)), 1, 0))
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

        self.filterProviders(initialSearchInput or "")

    def filterProviders(self, query: str) -> None:
        self.filteredProviders = (
            fuzzyFilter(
                self.allProviders,
                query,
                lambda provider: (
                    f"{provider.name} {provider.id} {provider.authType} "
                    f"{getattr(provider.method, 'name', None) or ''}"
                ),
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
            statusIndicator = formatAuthSelectorProviderStatus(provider)
            authTypeLabel = (
                theme.fg("muted", f" [{formatAuthSelectorProviderType(provider.authType, provider.subscription)}]")
                if self.showAuthTypeLabels
                else ""
            )
            line = (
                theme.fg("accent", "→ ") + theme.fg("accent", provider.name) + authTypeLabel + statusIndicator
                if isSelected
                else f"  {theme.fg('text', provider.name)}{authTypeLabel}{statusIndicator}"
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

    def _submit_selected(self) -> None:
        selected = self.filteredProviders[self.selectedIndex] if self.filteredProviders else None
        if selected is not None:
            self.onSelectCallback(selected.id, selected.authType)

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


__all__ = [
    "AuthSelectorProvider",
    "OAuthSelectorComponent",
    "formatAuthSelectorProviderStatus",
    "formatAuthSelectorProviderType",
]
