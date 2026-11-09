"""Translated from pi's ``coding-agent/src/cli/startup-ui.ts`` (v1.1.0): the dialogs shown before the interactive
mode owns the terminal.

MISAKA forks:
- (D11) No system theme, so there is no terminal colour query: the theme setting resolves against the terminal's
  light/dark appearance once and falls back to the built-in theme matching the terminal.
- No first-time setup: it runs only for the official Pi distribution.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from misaka.config import get_agent_dir
from misaka.core.keybindings import KeybindingsManager
from misaka.core.package_manager import DefaultPackageManager, ResolvedResource
from misaka.core.settings_manager import SettingsManager
from misaka.modes.interactive.components.extension_input import ExtensionInputComponent
from misaka.modes.interactive.components.extension_selector import (
    ExtensionSelectorComponent,
)
from misaka.modes.interactive.theme.theme import (
    Theme,
    getTerminalTheme,
    initTheme,
    loadThemeFromPath,
    resolveThemeSetting,
    setRegisteredThemes,
)
from misaka.ui.tui import (
    TUI,
    ProcessTerminal,
    TuiMainScreen,
    setCapabilityOverrides,
    setKeybindings,
)
from misaka.utils.js import startTask


@dataclass(slots=True)
class StartupOption[T]:
    """MISAKA fork: ``{ label, value }``."""

    label: str
    value: T


def loadThemes(resources: Sequence[ResolvedResource]) -> list[Theme]:
    themes: list[Theme] = []
    seen: set[str] = set()
    for resource in resources:
        if not resource.enabled:
            continue
        try:
            loadedTheme = loadThemeFromPath(resource.path)
            if loadedTheme.name:
                if loadedTheme.name in seen:
                    continue
                seen.add(loadedTheme.name)
            themes.append(loadedTheme)
        except Exception:  # noqa: BLE001, S112
            # Startup prompts should not fail because a theme is broken. The normal
            # resource loader reports theme diagnostics later in startup.
            continue
    return themes


async def loadStartupThemes(settingsManager: SettingsManager) -> list[Theme]:
    globalSettingsManager = SettingsManager.inMemory(settingsManager.getGlobalSettings(), {"projectTrusted": False})
    packageManager = DefaultPackageManager(
        {"cwd": ".", "agentDir": get_agent_dir(), "settingsManager": globalSettingsManager}
    )
    # MISAKA fork: MISAKA has no package sources, so nothing is missing to install or skip.
    resolvedPaths = await packageManager.resolve()
    return loadThemes(resolvedPaths.themes)


async def createStartupTui(settingsManager: SettingsManager) -> TUI:
    setCapabilityOverrides(settingsManager.getTerminalCapabilityOverrides())
    setRegisteredThemes(await loadStartupThemes(settingsManager))
    # MISAKA fork (D11): the built-in theme matching the terminal, not the system theme.
    initTheme(resolveThemeSetting(settingsManager.getThemeSetting(), getTerminalTheme()) or getTerminalTheme())
    setKeybindings(KeybindingsManager.create())
    ui: TUI = TuiMainScreen(ProcessTerminal(), settingsManager.getShowHardwareCursor(), get_agent_dir())
    ui.setClearOnShrink(settingsManager.getClearOnShrink())
    return ui


def startStartupTui(ui: TUI, settingsManager: SettingsManager) -> None:
    ui.start()
    # MISAKA fork (D11): no terminal colour query.


async def clearStartupTui(ui: TUI) -> None:
    ui.clear()
    ui.requestRender()
    await asyncio.sleep(0.025)


def shouldRunFirstTimeSetup(settingsPath: str | None = None) -> bool:
    """MISAKA fork: first-time setup runs for the official Pi distribution only."""
    return False


async def showStartupSelector[T](
    settingsManager: SettingsManager,
    title: str,
    options: Sequence[StartupOption[T]],
) -> T | None:
    ui = await createStartupTui(settingsManager)
    result: asyncio.Future[T | None] = asyncio.get_running_loop().create_future()
    settled = False

    async def finish(value: T | None) -> None:
        nonlocal settled
        if settled:
            return
        settled = True
        await clearStartupTui(ui)
        ui.stop()
        result.set_result(value)

    def onSelect(option: str) -> None:
        startTask(finish(next((entry.value for entry in options if entry.label == option), None)))

    def onCancel() -> None:
        startTask(finish(None))

    selector = ExtensionSelectorComponent(
        title,
        [option.label for option in options],
        onSelect,
        onCancel,
        {"tui": ui},
    )
    ui.addChild(selector)
    ui.setFocus(selector)
    startStartupTui(ui, settingsManager)
    return await result


async def showStartupInput(
    settingsManager: SettingsManager,
    title: str,
    placeholder: str | None = None,
) -> str | None:
    ui = await createStartupTui(settingsManager)
    result: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
    settled = False
    input: Any = None

    async def finish(value: str | None) -> None:
        nonlocal settled
        if settled:
            return
        settled = True
        input.dispose()
        await clearStartupTui(ui)
        ui.stop()
        result.set_result(value)

    def onSubmit(value: str) -> None:
        startTask(finish(value))

    def onCancel() -> None:
        startTask(finish(None))

    input = ExtensionInputComponent(title, placeholder, onSubmit, onCancel, {"tui": ui})
    ui.addChild(input)
    ui.setFocus(input)
    startStartupTui(ui, settingsManager)
    return await result


__all__ = [
    "StartupOption",
    "clearStartupTui",
    "createStartupTui",
    "loadStartupThemes",
    "loadThemes",
    "shouldRunFirstTimeSetup",
    "showStartupInput",
    "showStartupSelector",
    "startStartupTui",
]
