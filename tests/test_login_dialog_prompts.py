"""Translated from pi's ``coding-agent/test/suite/regressions/5433-extension-oauth-prompt-input.test.ts`` (v1.3.0)."""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from misaka.ai.auth.types import AuthInfoLink
from misaka.core.keybindings import KeybindingsManager
from misaka.modes.interactive.components import login_dialog
from misaka.modes.interactive.components.login_dialog import LoginDialogComponent
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import setKeybindings
from misaka.utils.ansi import stripAnsi


@pytest.fixture(autouse=True, scope="module")
def darkTheme():
    initTheme("dark")


@pytest.fixture(autouse=True)
def keybindings(monkeypatch: pytest.MonkeyPatch):
    setKeybindings(KeybindingsManager())
    monkeypatch.setattr(login_dialog, "openBrowser", MagicMock())


def createDialog() -> LoginDialogComponent:
    return LoginDialogComponent(SimpleNamespace(requestRender=MagicMock()), "prompt-repro", lambda *_: None, "Prompt Repro")


def renderDialog(dialog: LoginDialogComponent) -> list[str]:
    # stripAnsi keeps APC sequences, so drop the TUI's zero-width cursor markers (e.g. \x1b_pi:fc\x07) first
    return [
        line.rstrip() for line in stripAnsi(re.sub(r"\x1b_[^\x07]*\x07", "", "\n".join(dialog.render(120)))).split("\n")
    ]


def countRenderedValue(lines: list[str], value: str) -> int:
    return len([line for line in lines if line.strip() == f"> {value}"])


async def test_keeps_previous_prompt_input_stable_when_a_later_prompt_is_active():
    dialog = createDialog()

    firstPrompt = dialog.showPrompt("First prompt:", "first-value")
    dialog.handleInput("first-value")
    dialog.handleInput("\n")
    assert await firstPrompt == "first-value"

    secondPrompt = dialog.showPrompt("Second prompt:")
    dialog.handleInput("second-secret-demo")

    lines = renderDialog(dialog)
    assert "First prompt:" in "\n".join(lines)
    assert "Second prompt:" in "\n".join(lines)
    assert countRenderedValue(lines, "first-value") == 1
    assert countRenderedValue(lines, "second-secret-demo") == 1

    dialog.handleInput("\n")
    assert await secondPrompt == "second-secret-demo"


async def test_preserves_auth_instructions_when_showing_a_prompt():
    dialog = createDialog()

    dialog.showAuth("https://example.invalid/login", "Authorize the extension")
    dialog.showPrompt("First prompt:")

    output = "\n".join(renderDialog(dialog))
    assert "https://example.invalid/login" in output
    assert "Authorize the extension" in output
    assert "First prompt:" in output


async def test_preserves_neutral_information_and_links_when_showing_a_prompt():
    dialog = createDialog()

    dialog.showInfo(
        "Configure credentials outside pi.",
        [AuthInfoLink(label="Provider documentation", url="https://example.invalid/docs")],
    )
    dialog.showPrompt("Press Enter to continue:")

    output = "\n".join(renderDialog(dialog))
    assert "Configure credentials outside pi." in output
    assert "Provider documentation: https://example.invalid/docs" in output
    assert "Press Enter to continue:" in output


async def test_preserves_setup_details_when_showing_a_prompt():
    dialog = createDialog()

    dialog.showDetails(["AWS credential setup:", "providers.md"])
    dialog.showPrompt("Enter API key:")

    output = "\n".join(renderDialog(dialog))
    assert "AWS credential setup:" in output
    assert "providers.md" in output
    assert "Enter API key:" in output


async def test_keeps_previous_manual_input_stable_when_a_later_prompt_is_active():
    dialog = createDialog()

    manualInput = dialog.showManualInput("Paste callback URL:")
    dialog.handleInput("callback-value")
    dialog.handleInput("\n")
    assert await manualInput == "callback-value"

    prompt = dialog.showPrompt("Second prompt:")
    dialog.handleInput("second-secret-demo")

    lines = renderDialog(dialog)
    assert "Paste callback URL:" in "\n".join(lines)
    assert "Second prompt:" in "\n".join(lines)
    assert countRenderedValue(lines, "callback-value") == 1
    assert countRenderedValue(lines, "second-secret-demo") == 1

    dialog.handleInput("\n")
    assert await prompt == "second-secret-demo"
