"""Translated from pi's ``coding-agent/test/auth-url-copy.test.ts`` (v1.1.0).

``vi.mock`` of the clipboard and open-browser modules becomes ``monkeypatch``; ``vi.waitFor`` becomes a short poll on
the running loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from misaka.ai.compat.extension_oauth_types import OAuthDeviceCodeInfo
from misaka.core.keybindings import KeybindingsManager
from misaka.extensions.mcp.ui import McpManagerView
from misaka.modes.interactive.components import login_dialog
from misaka.modes.interactive.components.login_dialog import LoginDialogComponent
from misaka.modes.interactive.theme.theme import initTheme, theme
from misaka.ui.tui import setKeybindings
from misaka.utils import clipboard
from misaka.utils.ansi import stripAnsi
from misaka.utils.js import AbortController

URL = f"https://auth.example.invalid/authorize?{'x' * 300}"
CTRL_X = "\x18"

tui = SimpleNamespace(requestRender=MagicMock())


def rendered(component: Any) -> str:
    return stripAnsi("\n".join(component.render(80)))


async def waitFor(assertion: Callable[[], None], timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            assertion()
            return
        except AssertionError:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(0.01)


@pytest.fixture(autouse=True, scope="module")
def darkTheme():
    initTheme("dark")


@pytest.fixture
def copyToClipboard(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    setKeybindings(KeybindingsManager())
    mock = AsyncMock()
    monkeypatch.setattr(clipboard, "copyToClipboard", mock)
    monkeypatch.setattr(login_dialog, "openBrowser", MagicMock())
    return mock


async def test_login_dialog_copies_the_auth_url_instead_of_typing_into_the_code_input(copyToClipboard):
    dialog = LoginDialogComponent(tui, "test", lambda *_: None)
    dialog.showAuth(URL)
    dialog.showManualInput("Paste the code:")
    assert "ctrl+x to copy" in rendered(dialog)

    dialog.handleInput(CTRL_X)

    def copied() -> None:
        assert "Copied URL to clipboard" in rendered(dialog)

    await waitFor(copied)
    copyToClipboard.assert_called_with(URL)


def test_login_dialog_ignores_the_copy_key_without_an_auth_url(copyToClipboard):
    dialog = LoginDialogComponent(tui, "test", lambda *_: None)
    dialog.showDeviceCode(OAuthDeviceCodeInfo(userCode="ABCD", verificationUri="https://example.invalid/device"))
    dialog.handleInput(CTRL_X)
    copyToClipboard.assert_not_called()


async def test_mcp_sign_in_screen_copies_the_authorization_url(copyToClipboard):
    view = McpManagerView(tui, theme, KeybindingsManager())
    view.redirectUrl("Sign in to issues", URL, AbortController().signal)
    assert "ctrl+x to copy" in rendered(view)

    view.handleInput(CTRL_X)

    def copied() -> None:
        assert "Copied URL to clipboard" in rendered(view)

    await waitFor(copied)
    copyToClipboard.assert_called_with(URL)
