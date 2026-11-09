"""The setup wizard's Research section stores coverage_scan's optional OpenAlex key in the
home's .env -- a credential, beside the web vendors' keys -- and Enter keeps what is there."""
from misaka.cli import setup_wizard as setup
from misaka.config import env as env_file
from misaka.extensions import coverage


def _answer(monkeypatch, value):
    monkeypatch.setattr(setup.ui, "prompt", lambda question, default=None, *, password=False: value)


def test_a_key_is_written_to_the_home_env_and_enter_keeps_it(monkeypatch):
    monkeypatch.delenv(coverage.KEY_ENV, raising=False)
    wizard = setup.Wizard()
    _answer(monkeypatch, "")
    wizard._openalex_key()
    assert coverage.KEY_ENV not in env_file.read() and wizard.state["openalex"] is False
    _answer(monkeypatch, "ok-123")
    wizard._openalex_key()
    assert env_file.read()[coverage.KEY_ENV] == "ok-123" and wizard.state["openalex"] is True
    _answer(monkeypatch, "")
    wizard._openalex_key()
    assert env_file.read()[coverage.KEY_ENV] == "ok-123" and wizard.state["openalex"] is True


def test_the_key_never_lands_in_settings_json(monkeypatch):
    from misaka.config import home
    monkeypatch.delenv(coverage.KEY_ENV, raising=False)
    _answer(monkeypatch, "ok-456")
    setup.Wizard()._openalex_key()
    settings = home.path("settings")
    assert not settings.exists() or "ok-456" not in settings.read_text(encoding="utf-8")
