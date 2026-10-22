"""Translated from pi's ``coding-agent/test/session-selector-rename.test.ts`` (v1.1.0)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from misaka.core.keybindings import KeybindingsManager
from misaka.core.session_manager import SessionInfo
from misaka.modes.interactive.components.session_selector import (
    SessionSelectorComponent,
)
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import setKeybindings
from tests.fake_timers import flush


async def flushPromises() -> None:
    await flush()


def makeSession(id: str, **overrides: Any) -> SessionInfo:
    return SessionInfo(
        path=overrides.get("path", f"/tmp/{id}.jsonl"),
        id=id,
        cwd=overrides.get("cwd", ""),
        name=overrides.get("name"),
        created=overrides.get("created", datetime.fromtimestamp(0, UTC)),
        modified=overrides.get("modified", datetime.fromtimestamp(0, UTC)),
        messageCount=overrides.get("messageCount", 1),
        firstMessage=overrides.get("firstMessage", "hello"),
        allMessagesText=overrides.get("allMessagesText", "hello"),
    )


# Kitty keyboard protocol encoding for Ctrl+R
CTRL_R = "\x1b[114;5u"


@pytest.fixture(autouse=True, scope="module")
def darkTheme():
    initTheme("dark")


@pytest.fixture(autouse=True)
def isolatedKeybindings():
    # Ensure test isolation: keybindings are a global singleton
    setKeybindings(KeybindingsManager())


def noop(*_args: Any) -> None:
    return None


def loader(sessions: list[SessionInfo]) -> Any:
    async def load(*_args: Any) -> list[SessionInfo]:
        return sessions

    return load


async def test_shows_rename_hint_in_interactive_resume_picker_configuration():
    sessions = [makeSession("a")]
    keybindings = KeybindingsManager()
    selector = SessionSelectorComponent(
        loader(sessions), loader([]), noop, noop, noop, noop, {"showRenameHint": True, "keybindings": keybindings}
    )
    await flushPromises()

    output = "\n".join(selector.render(120))
    assert "ctrl+r" in output
    assert "rename" in output


async def test_does_not_show_rename_hint_in_resume_picker_configuration():
    sessions = [makeSession("a")]
    keybindings = KeybindingsManager()
    selector = SessionSelectorComponent(
        loader(sessions), loader([]), noop, noop, noop, noop, {"showRenameHint": False, "keybindings": keybindings}
    )
    await flushPromises()

    output = "\n".join(selector.render(120))
    assert "ctrl+r" not in output
    assert "rename" not in output


async def test_enters_rename_mode_on_ctrl_r_and_submits_with_enter():
    sessions = [makeSession("a", name="Old")]
    renameSession = AsyncMock()

    keybindings = KeybindingsManager()
    selector = SessionSelectorComponent(
        loader(sessions),
        loader([]),
        noop,
        noop,
        noop,
        noop,
        {"renameSession": renameSession, "showRenameHint": True, "keybindings": keybindings},
    )
    await flushPromises()

    selector.getSessionList().handleInput(CTRL_R)
    await flushPromises()

    # Rename mode layout
    output = "\n".join(selector.render(120))
    assert "Rename Session" in output
    assert "Resume Session" not in output

    # Type and submit
    selector.handleInput("X")
    selector.handleInput("\r")
    await flushPromises()

    assert renameSession.call_count == 1
    renameSession.assert_called_with(sessions[0].path, "XOld")
