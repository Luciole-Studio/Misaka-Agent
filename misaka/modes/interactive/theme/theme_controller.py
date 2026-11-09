"""Translated from pi's ``modes/interactive/theme/theme-controller.ts`` (v1.1.0).

MISAKA fork: everything that keeps the theme in sync with the terminal's reported colors and light/dark reports
serves pi's system theme and is not ported (D11): ``TERMINAL_QUERY_TIMEOUT_MS``, ``requestTerminalColors``,
``sameRgb``, ``sameTerminalColors``, the auto-sync state, ``rebindTui``, ``waitForTerminalColors``,
``disableAutoSync``, ``dispose``, ``queryTerminalColors``, ``applyTerminalColors``, ``reapplyForTerminal``,
``setAutoSync``, ``bindTerminalColorSchemeListener`` and ``applyTerminalColorSchemeChange``. Theme pairs
(``light/dark``) resolve against the terminal appearance once, when the setting is applied. Where upstream falls back
to the system theme, MISAKA falls back to the built-in theme matching the terminal.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, NotRequired, TypedDict

from .theme import (
    SetThemeResult,
    TerminalTheme,
    Theme,
    getTerminalTheme,
    initTheme,
    resolveThemeSetting,
    setTheme,
    setThemeInstance,
)

if TYPE_CHECKING:
    from misaka.core.settings_manager import SettingsManager
    from misaka.ui.tui.tui import TUI

type ThemeResult = SetThemeResult


class InteractiveThemeControllerOptions(TypedDict):
    getSettingsManager: Callable[[], SettingsManager]
    showError: Callable[[str], None]
    onChanged: Callable[[], None]
    initialThemeSetting: NotRequired[str | None]


class InteractiveThemeController:
    """Applies the theme setting."""

    def __init__(self, ui: TUI, options: InteractiveThemeControllerOptions) -> None:
        self.ui = ui
        self.getSettingsManager = options["getSettingsManager"]
        self.showError = options["showError"]
        self.onChanged = options["onChanged"]
        self.currentThemeSetting: str | None = options.get("initialThemeSetting")
        self.activeThemeName: str | None = self.resolveThemeName()
        initTheme(self.activeThemeName, True)

    def applyFromSettings(self) -> None:
        """Apply the theme setting now."""
        themeSetting = self.getThemeSetting()
        themeName = self.resolveThemeName()
        self.applyThemeName(themeName, themeSetting is not None)

    def getThemeSelection(self) -> str | None:
        if self.currentThemeSetting is not None:
            return self.currentThemeSetting
        themeSetting = self.getSettingsManager().getThemeSetting()
        return themeSetting if themeSetting is not None else self.activeThemeName

    def setThemeName(self, themeName: str, showError: bool = False) -> ThemeResult:
        result = self.applyThemeName(themeName, showError)
        if result["success"]:
            self.currentThemeSetting = themeName
        return result

    def setThemeSetting(self, themeSetting: str) -> None:
        self.currentThemeSetting = themeSetting
        self.applyFromSettings()

    def setThemeInstance(self, themeInstance: Theme) -> ThemeResult:
        setThemeInstance(themeInstance)
        self.activeThemeName = "<in-memory>"
        self.notifyChanged()
        return {"success": True}

    def preview(self, themeSettingOrName: str) -> None:
        themeName = resolveThemeSetting(themeSettingOrName, getTerminalTheme())
        if themeName is None:
            themeName = self.activeThemeName
        if not themeName:
            return
        if setTheme(themeName, True)["success"]:
            self.ui.invalidate()
            self.ui.requestRender()

    def getTerminalTheme(self) -> TerminalTheme:
        return getTerminalTheme()

    def getThemeSetting(self) -> str | None:
        if self.currentThemeSetting is not None:
            return self.currentThemeSetting
        return self.getSettingsManager().getThemeSetting()

    def resolveThemeName(self) -> str:
        """The theme for the current setting and terminal appearance. Without a setting, MISAKA uses the built-in
        theme matching the terminal."""
        themeName = resolveThemeSetting(self.getThemeSetting(), getTerminalTheme())
        return themeName if themeName is not None else getTerminalTheme()

    def applyThemeName(self, themeName: str, showError: bool = False) -> ThemeResult:
        result = setTheme(themeName, True)
        self.activeThemeName = themeName if result["success"] else getTerminalTheme()
        self.notifyChanged()
        if not result["success"] and showError:
            self.showError(
                f'Failed to load theme "{themeName}": {result.get("error")}\n'
                f"Fell back to the {self.activeThemeName} theme."
            )
        return result

    def notifyChanged(self) -> None:
        self.ui.invalidate()
        self.onChanged()
