"""Offline checks for role-scoped model UI/setup; no real profiles or authentication."""
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from misaka.cli import setup


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    from misaka import config
    from misaka.cli import auth

    roles = tmp_path / "profiles"
    (roles / "last_order").mkdir(parents=True)
    (roles / "sisters/10032").mkdir(parents=True)
    cfg = {"provider": "openai-codex", "default_model": "shared",
           "roles_root": str(roles), "profiles_root": str(roles / "sisters")}
    models = [NS(provider="openai-codex", id="shared", name="Shared"),
              NS(provider="custom", id="vendor/model", name="Custom"),
              NS(provider="unconfigured", id="hidden", name="Hidden")]
    registry = NS(getAll=lambda: models, getOAuthProviders=list,
                  hasConfiguredAuth=lambda model: model.provider != "unconfigured")
    monkeypatch.setattr(config, "current_config", lambda: cfg.copy())
    monkeypatch.setattr(auth, "_create_runtime", lambda **kwargs: NS(registry=registry, storage=None))
    monkeypatch.setattr(auth, "_missing_sdk_extras", lambda *args: [])
    monkeypatch.setattr(setup.Wizard, "_provider_labels", staticmethod(lambda registry, providers, oauth: providers))
    monkeypatch.setattr(setup.Wizard, "_credential", lambda *args: None)
    return cfg, models


@pytest.mark.parametrize("scope", [0, 1, 2])
def test_setup_saves_only_selected_scope(catalog, monkeypatch, scope):
    from misaka.config import profiles
    from misaka.core.settings_manager import SettingsManager

    cfg, _ = catalog
    role_writes, global_writes, verified = [], [], []
    prompts = []

    def pick(title, choices, default=0, description=None):
        prompts.append((title, choices))
        if title == "Whose default model should change?":
            return scope
        if title.startswith("Default provider"):
            return choices.index("Another provider...")
        return choices.index("custom")

    monkeypatch.setattr(setup, "prompt_choice", pick)
    monkeypatch.setattr(setup.Wizard, "_pick_model", lambda *args: "vendor/model")
    monkeypatch.setattr(setup.Wizard, "_verify", lambda self, *args: verified.append(args))
    monkeypatch.setattr(profiles, "persist_role_default_model",
                        lambda profile, model, **kwargs: role_writes.append((profile, model, kwargs)))
    monkeypatch.setattr(SettingsManager, "create", lambda *args: NS(
        setDefaultModelAndProvider=lambda *values: global_writes.append(values)))
    setup.Wizard().model()
    assert len(verified) == 1
    assert prompts[0][1] == ["Global default (roles without their own default)", "Last Order", "Sister 10032"]
    if scope:
        suffix = "/last_order" if scope == 1 else "/sisters/10032"
        assert role_writes == [(cfg["roles_root"] + suffix, "custom/vendor/model", {"strict": True})]
        assert global_writes == []
    else:
        assert role_writes == []
        assert global_writes == [("custom", "vendor/model")]


def test_setup_role_preselects_its_own_provider_and_model(catalog, monkeypatch):
    from pathlib import Path

    from misaka.config import profiles

    cfg, _ = catalog
    (Path(cfg["roles_root"]) / "last_order/settings.json").write_text(
        json.dumps({"defaultProvider": "custom", "defaultModel": "vendor/model"}))
    seen = []

    def pick(title, choices, default=0, description=None):
        if title == "Whose default model should change?":
            return 1
        seen.append(choices[default])
        return default

    def pick_model(self, registry, provider, selected_cfg):
        assert provider == selected_cfg["provider"] == "custom"
        assert selected_cfg["default_model"] == "vendor/model"
        return selected_cfg["default_model"]

    monkeypatch.setattr(setup, "prompt_choice", pick)
    monkeypatch.setattr(setup.Wizard, "_pick_model", pick_model)
    monkeypatch.setattr(setup.Wizard, "_verify", lambda *args: None)
    monkeypatch.setattr(profiles, "persist_role_default_model", lambda *args, **kwargs: False)
    setup.Wizard().model()
    assert seen == ["custom"]


def test_setup_failed_role_write_does_not_report_saved_or_verify(catalog, monkeypatch, capsys):
    from misaka.config import profiles

    monkeypatch.setattr(setup, "prompt_choice",
                        lambda title, choices, *args: 1 if title.startswith("Whose") else 0)
    monkeypatch.setattr(setup.Wizard, "_pick_model", lambda *args: "shared")
    monkeypatch.setattr(setup.Wizard, "_verify", lambda *args: pytest.fail("must not send a request after failed save"))

    def fail(*args, **kwargs):
        raise OSError("fixture disk full")

    monkeypatch.setattr(profiles, "persist_role_default_model", fail)
    setup.Wizard().model()
    output = capsys.readouterr().out
    assert "Default model was not saved" in output
    assert "Default model saved for" not in output


def test_new_sister_can_choose_other_configured_provider(catalog, monkeypatch):
    choices_seen = []

    def pick(title, choices, *args):
        choices_seen.append(choices)
        return 1

    monkeypatch.setattr(setup, "prompt_choice", pick)
    assert setup.Wizard()._sister_model() == "custom/vendor/model"
    assert choices_seen[0][0].startswith("Follow the global default")
    assert len(choices_seen[1]) == 2
    assert all("unconfigured" not in label for label in choices_seen[1])


def test_model_alias_folding_does_not_cross_providers():
    models = [NS(provider="a", id="same"), NS(provider="a", id="same-20260917"),
              NS(provider="b", id="same-20260917")]
    assert setup.Wizard._model_choices(models, None) == [models[0], models[2]]


@pytest.mark.asyncio
@pytest.mark.parametrize("persist", [False, True])
@pytest.mark.parametrize("failure", [False, True])
async def test_model_selector_has_one_save_path(persist, failure):
    from misaka.ui.tui.interactive.interactive_mode import InteractiveMode

    calls, statuses, errors = [], [], []
    model = NS(provider="custom", id="vendor/model")
    mode = NS(session=NS(setModel=AsyncMock(side_effect=OSError("fixture write failed") if failure else None)),
              updateAvailableProviderCount=lambda: None, footer=NS(invalidate=lambda: None),
              updateEditorBorderColor=lambda: None, showStatus=statuses.append, showError=errors.append,
              _schedule_task=lambda task: None, maybeWarnAboutAnthropicSubscriptionAuth=lambda model: None)
    await InteractiveMode._handle_model_select(mode, model, lambda: calls.append("done"), persist=persist)
    mode.session.setModel.assert_awaited_once_with(model, persist=persist)
    assert calls == ["done"]
    if failure:
        assert errors == ["fixture write failed"]
        assert statuses == []
    else:
        assert not errors
        assert statuses == (["Default model: custom/vendor/model"] if persist else ["Model: vendor/model"])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_login_adoption_has_one_save_path(failure):
    from misaka.ui.tui.interactive.interactive_mode import InteractiveMode

    model = NS(provider="custom", id="vendor/model")
    statuses, errors = [], []
    registry = NS(refresh=AsyncMock(), getAvailable=lambda: [model])
    mode = NS(session=NS(modelRegistry=registry,
                        setModel=AsyncMock(side_effect=OSError("fixture write failed") if failure else None)),
              updateAvailableProviderCount=lambda: None, footer=NS(invalidate=lambda: None),
              updateEditorBorderColor=lambda: None, showStatus=statuses.append,
              showError=errors.append, _schedule_task=lambda task: None,
              maybeWarnAboutAnthropicSubscriptionAuth=lambda model: None)
    await InteractiveMode.completeProviderAuthentication(mode, "custom", "Custom", "api-key")
    mode.session.setModel.assert_awaited_once_with(model, persist=True)
    if failure:
        assert "fixture write failed" in errors[-1]
        assert all("Selected" not in message for message in statuses)
    else:
        assert not errors
        assert "Selected vendor/model" in statuses[-1]
