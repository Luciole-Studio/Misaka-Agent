"""Interactive session picker entry point for CLI resume flows."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from misaka.core.keybindings import KeybindingsManager
from misaka.core.session_manager import SessionInfo, SessionListProgress
from misaka.ui.tui import TUI, ProcessTerminal, setKeybindings
from misaka.ui.tui.interactive.components.session_selector import (
    SessionSelectorComponent,
)

type SessionsLoader = Callable[[SessionListProgress | None], Awaitable[list[SessionInfo]]]


class _SessionSelectorComponentLike(Protocol):
    def getSessionList(self) -> object: ...


async def select_session(
    currentSessionsLoader: SessionsLoader,
    allSessionsLoader: SessionsLoader,
) -> str | None:
    return await _select_session(currentSessionsLoader, allSessionsLoader)


async def _select_session(
    currentSessionsLoader: SessionsLoader,
    allSessionsLoader: SessionsLoader,
    *,
    terminalFactory: type[ProcessTerminal] = ProcessTerminal,
    uiFactory: type[TUI] = TUI,
    componentFactory: type[SessionSelectorComponent] = SessionSelectorComponent,
    keybindingsFactory: Callable[[], KeybindingsManager] = KeybindingsManager.create,
    setKeybindingsFn: Callable[[KeybindingsManager], None] = setKeybindings,
) -> str | None:
    loop = asyncio.get_running_loop()
    done: asyncio.Future[str | None] = loop.create_future()
    ui = uiFactory(terminalFactory())
    keybindings = keybindingsFactory()
    setKeybindingsFn(keybindings)
    closed = False

    def finish(result: str | None = None, *, exit_immediately: bool = False) -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        ui.stop()
        if exit_immediately:
            done.set_exception(SystemExit(0))
            return
        done.set_result(result)

    selector: _SessionSelectorComponentLike = componentFactory(
        currentSessionsLoader,
        allSessionsLoader,
        lambda path: finish(path, exit_immediately=False),
        lambda: finish(None, exit_immediately=False),
        lambda: finish(None, exit_immediately=True),
        lambda: ui.requestRender(),
        {"showRenameHint": False, "keybindings": keybindings},
    )
    ui.addChild(selector)
    ui.setFocus(selector.getSessionList())
    try:
        ui.start()
        return await done
    finally:
        if not closed:
            closed = True
            ui.stop()


__all__ = ["select_session"]
