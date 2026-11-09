"""``components/login_dialog.py`` against pi's ``login-dialog.ts`` (0.99.1)."""

from __future__ import annotations

import pytest

from misaka.ai.auth.types import AuthInfoLink
from misaka.core.keybindings import KeybindingsManager
from misaka.modes.interactive.components.login_dialog import LoginDialogComponent
from misaka.ui.tui import keybindings as tui_keybindings
from misaka.utils.ansi import stripAnsi


@pytest.fixture(autouse=True)
def _keybindings(monkeypatch):
    monkeypatch.setattr(tui_keybindings, "globalKeybindings", KeybindingsManager())


class _Tui:
    def requestRender(self, *_args):
        pass


def _text(dialog: LoginDialogComponent) -> str:
    return stripAnsi("\n".join(dialog.render(120)))


def test_the_title_uses_the_given_name_or_the_provider_id():
    assert "Login to Acme" in _text(LoginDialogComponent(_Tui(), "acme", lambda *_: None, "Acme"))
    assert "Login to anthropic" in _text(LoginDialogComponent(_Tui(), "anthropic", lambda *_: None))


def test_info_appends_links_and_only_offers_to_close_when_asked():
    dialog = LoginDialogComponent(_Tui(), "p", lambda *_: None)
    dialog.showDetails(["before"])
    dialog.showInfo("Profiles work too", [AuthInfoLink(url="https://docs.example", label="Docs")])

    text = _text(dialog)
    assert "before" in text and "Profiles work too" in text and "Docs: https://docs.example" in text
    assert "to close" not in text

    dialog.showInfo("Configured elsewhere", [], True)
    assert "to close" in _text(dialog)


def test_details_replace_the_content_without_a_close_hint():
    dialog = LoginDialogComponent(_Tui(), "p", lambda *_: None)
    dialog.showInfo("old")
    dialog.showDetails(["new"])

    text = _text(dialog)
    assert "new" in text and "old" not in text and "to close" not in text
