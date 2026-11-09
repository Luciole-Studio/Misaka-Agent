"""A model section left unfinished: a failed sign-in, a skipped key, a missing SDK.

Each used to pass as done. The summary ticked "model" because a provider had been chosen,
and said "the key is stored but does not work" when no key had been stored; "Install it
now?" took a Yes and installed nothing under uv and pipx; and bare ``misaka`` then ran the
whole wizard again, Sisters and documents included, for the one section that was missing.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace as NS

import pytest

from misaka.cli import setup_wizard as setup


def _no_question(*_args, **_kwargs):
    raise AssertionError("asked a question it should not have")


def _model_row(capsys, state) -> str:
    wizard = setup.Wizard()
    wizard.state.update(state)
    wizard._model_check()
    return capsys.readouterr().out


def test_a_failed_sign_in_is_not_ticked(capsys):
    row = _model_row(capsys, {"provider": "openai", "model": "gpt-6.1-sol", "credential": False})
    assert "✗" in row and "no credential stored" in row and "misaka setup model" in row


def test_a_failed_request_no_longer_claims_a_stored_key(capsys):
    row = _model_row(capsys, {"provider": "openai", "model": "m", "credential": True, "verified": False})
    assert "✗" in row and "key is stored" not in row and "stored credential does not work" in row


def test_a_working_untested_credential_is_still_ticked(capsys):
    row = _model_row(capsys, {"provider": "openai", "model": "m", "credential": True})
    assert "✓" in row and "(not tested)" in row


def test_nothing_is_sent_without_a_credential(monkeypatch, capsys):
    monkeypatch.setattr(setup, "prompt_yes_no", _no_question)
    wizard = setup.Wizard()
    wizard.state["credential"] = False
    wizard._verify(NS(), "openai", "m")
    assert "nothing to test" in capsys.readouterr().out


def test_a_uv_tool_install_prints_the_command_without_asking(monkeypatch, capsys):
    from misaka.cli import update

    command = ["uv", "tool", "install", "--force", "misaka[openai] @ git+https://example/repo@branch"]
    monkeypatch.setattr(update, "describe", lambda: NS(kind="git", installer="uv tool"))
    monkeypatch.setattr(update, "adding_extras", lambda install, extras: command)
    monkeypatch.setattr(setup, "prompt_yes_no", _no_question)
    monkeypatch.setattr(setup.subprocess, "run", _no_question)

    wizard = setup.Wizard()
    wizard._add_extras(["openai"])
    wizard.state.update(provider="openai", model="m", credential=True)
    wizard._verify(NS(), "openai", "m")      # skipped: the request would fail without the SDK
    wizard._model_check()

    out = capsys.readouterr().out
    assert "cannot be installed while setup is running" in out
    assert "test request is skipped" in out
    assert "✗" in out.splitlines()[-1] and "its SDK is not installed" in out.splitlines()[-1]


@pytest.mark.parametrize(("saved", "section"), [(True, "model"), (False, None)])
def test_bare_misaka_reopens_only_the_unfinished_section(monkeypatch, saved, section):
    from misaka.cli import app
    from misaka.config import home

    if saved:
        (home.path("settings")).write_text(json.dumps({"defaultProvider": "openai", "defaultModel": "m"}))
    asked = []
    monkeypatch.setattr(setup, "configured_anywhere", lambda: False)
    monkeypatch.setattr(setup, "run", lambda section=None: asked.append(section) or 1)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    assert app.main([]) == 1
    assert asked == [section]


def test_a_key_stored_during_setup_counts_at_once(monkeypatch):
    """The wizard's own runtime keeps the auth status it read at start; asked there, a key
    entered a moment ago would read as missing and a working sign-in would get a ✗."""
    import asyncio

    from misaka.ai.auth.types import ApiKeyCredential
    from misaka.cli.auth_overview import create_auth_runtime

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    auth = asyncio.run(create_auth_runtime(read_only=False, refresh=True))
    assert not setup.has_credential("openai")

    async def store(_current):
        return ApiKeyCredential(key="sk-test")

    asyncio.run(auth.credentials.modify("openai", store))
    assert not auth.runtime.getProviderAuthStatus("openai").configured   # the stale view
    assert setup.has_credential("openai")
