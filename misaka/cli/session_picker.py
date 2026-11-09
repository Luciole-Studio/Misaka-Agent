"""Translated from pi's ``coding-agent/src/cli/session-picker.ts`` (v1.1.0).

TUI session selector for --resume flag
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from misaka.core.keybindings import KeybindingsManager
from misaka.core.session_manager import SessionInfo, SessionListProgress
from misaka.core.settings_manager import SettingsManager
from misaka.modes.interactive.components.session_selector import (
    SessionSelectorComponent,
)
from misaka.ui.tui import setKeybindings

from .startup_ui import createStartupTui, startStartupTui

type SessionsLoader = Callable[[SessionListProgress | None, Any], Awaitable[list[SessionInfo]]]


async def selectSession(
    currentSessionsLoader: SessionsLoader,
    allSessionsLoader: SessionsLoader,
    settingsManager: SettingsManager,
) -> str | None:
    """Show TUI session selector and return selected session path or null if cancelled"""
    ui = await createStartupTui(settingsManager)
    result: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
    keybindings = KeybindingsManager.create()
    setKeybindings(keybindings)
    resolved = False

    def onSelect(path: str) -> None:
        nonlocal resolved
        if not resolved:
            resolved = True
            ui.stop()
            result.set_result(path)

    def onCancel() -> None:
        nonlocal resolved
        if not resolved:
            resolved = True
            ui.stop()
            result.set_result(None)

    def onExit() -> None:
        ui.stop()
        # MISAKA fork: `SystemExit` in place of `process.exit(0)`.
        if not result.done():
            result.set_exception(SystemExit(0))

    selector = SessionSelectorComponent(
        currentSessionsLoader,
        allSessionsLoader,
        onSelect,
        onCancel,
        onExit,
        lambda: ui.requestRender(),
        {"showRenameHint": False, "keybindings": keybindings},
    )

    ui.addChild(selector)
    ui.setFocus(selector.getSessionList())
    startStartupTui(ui, settingsManager)
    return await result


__all__ = ["selectSession"]
