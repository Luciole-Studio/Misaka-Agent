"""Translated from pi's ``coding-agent/src/cli/config-selector.ts`` (v1.5.0).

TUI config selector for `misaka config` command

MISAKA fork: ``selectConfig`` is a coroutine that waits on a future, as ``session_picker.selectSession`` does, and
``SystemExit`` takes the place of ``process.exit(0)``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from misaka.core.settings_manager import SettingsManager
from misaka.modes.interactive.components.config_selector import (
    ConfigSelectorComponent,
    ScopedResolvedPaths,
)
from misaka.modes.interactive.theme.theme import initTheme, stopThemeWatcher
from misaka.ui.tui import TUI, ProcessTerminal, TuiMainScreen


@dataclass(slots=True)
class ConfigSelectorOptions:
    resolvedPaths: ScopedResolvedPaths
    settingsManager: SettingsManager
    cwd: str
    agentDir: str
    writeScope: Literal["global", "project"]
    projectModeAvailable: bool


async def selectConfig(options: ConfigSelectorOptions) -> None:
    """Show TUI config selector and return when closed"""
    # Initialize theme before showing TUI
    initTheme(options.settingsManager.getTheme(), True)

    result: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    ui: TUI = TuiMainScreen(
        ProcessTerminal(),
        options.settingsManager.getShowHardwareCursor(),
        options.agentDir,
    )
    ui.setClearOnShrink(options.settingsManager.getClearOnShrink())
    resolved = False

    def onClose() -> None:
        nonlocal resolved
        if not resolved:
            resolved = True
            ui.stop()
            stopThemeWatcher()
            result.set_result(None)

    def onExit() -> None:
        ui.stop()
        stopThemeWatcher()
        # MISAKA fork: `SystemExit` in place of `process.exit(0)`.
        if not result.done():
            result.set_exception(SystemExit(0))

    selector = ConfigSelectorComponent(
        options.resolvedPaths,
        options.settingsManager,
        options.cwd,
        options.agentDir,
        onClose,
        onExit,
        lambda: ui.requestRender(),
        ui.terminal.rows,
        options.writeScope,
        options.projectModeAvailable,
    )

    ui.addChild(selector)
    ui.setFocus(selector.getResourceList())
    ui.start()
    await result


__all__ = ["ConfigSelectorOptions", "selectConfig"]
