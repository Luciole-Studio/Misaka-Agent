"""Interactive config selector entry point for the CLI."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from misaka.ui.tui import TUI, ProcessTerminal

from misaka.core.package_manager import ResolvedPaths
from misaka.core.settings_manager import SettingsManager
from misaka.ui.tui.interactive.components.config_selector import ConfigSelectorComponent
from misaka.ui.tui.interactive.theme.theme import init_theme, stop_theme_watcher


@dataclass(slots=True)
class ConfigSelectorOptions:
    resolvedPaths: ResolvedPaths
    settingsManager: SettingsManager
    cwd: str
    agentDir: str


class _ConfigSelectorComponentLike(Protocol):
    def getResourceList(self) -> object: ...


async def select_config(
    options: ConfigSelectorOptions,
) -> None:
    await _select_config(options)


async def _select_config(
    options: ConfigSelectorOptions,
    *,
    terminalFactory: type[ProcessTerminal] = ProcessTerminal,
    uiFactory: type[TUI] = TUI,
    componentFactory: type[ConfigSelectorComponent] = ConfigSelectorComponent,
    initTheme: Any = init_theme,
    stopThemeWatcher: Any = stop_theme_watcher,
) -> None:
    initTheme(options.settingsManager.getTheme(), True)

    loop = asyncio.get_running_loop()
    done: asyncio.Future[None] = loop.create_future()
    ui = uiFactory(terminalFactory())
    closed = False

    def finish(*, exit_immediately: bool = False) -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        ui.stop()
        stopThemeWatcher()
        if exit_immediately:
            done.set_exception(SystemExit(0))
            return
        done.set_result(None)

    selector: _ConfigSelectorComponentLike = componentFactory(
        options.resolvedPaths,
        options.settingsManager,
        options.cwd,
        options.agentDir,
        lambda: finish(exit_immediately=False),
        lambda: finish(exit_immediately=True),
        lambda: ui.requestRender(),
        ui.terminal.rows,
    )
    ui.addChild(selector)
    ui.setFocus(selector.getResourceList())
    try:
        ui.start()
        await done
    finally:
        if not closed:
            closed = True
            ui.stop()
            stopThemeWatcher()


selectConfig = select_config

__all__ = ["ConfigSelectorOptions", "selectConfig"]
