"""Each agent's own thinking level: Last Order and every Sister keep a default of their own
(``defaultThinkingLevel`` in her role's settings.json), ahead of the shared per-model table and
the home's default. All models are loopback fixtures."""
import json
from types import SimpleNamespace as NS

import pytest
from test_agent_model_runtime import (
    local_models as local_models,  # noqa: PLC0414
)
from test_agent_model_runtime import opened
from test_subagent_native_startup import isolated as isolated  # noqa: PLC0414

from misaka.config import home
from misaka.core.network import roster
from misaka.core.settings_manager import SettingsManager
from misaka.modes.interactive import interactive_mode


def _reasoning_models():
    path = home.path("models")
    models = json.loads(path.read_text(encoding="utf-8"))
    for provider in models["providers"].values():
        provider["models"][0].update(reasoning=True, compat={"forceAdaptiveThinking": True})
    path.write_text(json.dumps(models), encoding="utf-8")


def _settings(path, **values):
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(values)
    path.write_text(json.dumps(data), encoding="utf-8")


async def test_each_agent_starts_at_her_own_level(local_models):
    root, _requests = local_models
    _reasoning_models()
    _settings(home.path("settings"), defaultThinkingLevel="medium",
              modelThinkingLevels={"fixture-lo/shared": "high", "fixture-a/shared": "high"})
    _settings(home.path("roles_root") / "last_order" / "settings.json", defaultThinkingLevel="low")

    async with opened(root, "last_order", "foreground") as (_, session):
        assert session.thinkingLevel == "low"          # her own level beats the per-model table
    async with opened(root, "sisters/10032", "card") as (_, session):
        assert session.thinkingLevel == "high"         # no level of her own: the per-model table
    async with opened(root, "sisters/10036", "card") as (_, session):
        assert session.thinkingLevel == "medium"       # neither: the home's default


async def test_a_role_window_saves_the_default_as_hers(local_models):
    root, _requests = local_models
    _reasoning_models()
    _settings(home.path("settings"), defaultThinkingLevel="medium")
    lo_file = home.path("roles_root") / "last_order" / "settings.json"

    async with opened(root, "last_order", "foreground") as (_, session):
        session.settingsManager.setDefaultThinkingLevel("high")
    assert json.loads(lo_file.read_text(encoding="utf-8"))["defaultThinkingLevel"] == "high"
    assert json.loads(lo_file.read_text(encoding="utf-8"))["custom"] == "preserve"
    assert json.loads(home.path("settings").read_text(encoding="utf-8"))["defaultThinkingLevel"] == "medium"
    assert "defaultThinkingLevel" not in json.loads(
        (home.path("roles_root") / "sisters" / "10032" / "settings.json").read_text(encoding="utf-8"))

    async with opened(root, "last_order", "foreground") as (_, session):
        assert session.thinkingLevel == "high"
    async with opened(root, "sisters/10032", "card") as (_, session):
        assert session.thinkingLevel == "medium"

    unbound = SettingsManager.forRole(None)
    unbound.setDefaultThinkingLevel("low")                 # outside a role: the home's default
    await unbound.flush()
    assert json.loads(home.path("settings").read_text(encoding="utf-8"))["defaultThinkingLevel"] == "low"
    assert json.loads(lo_file.read_text(encoding="utf-8"))["defaultThinkingLevel"] == "high"


async def test_a_model_switch_keeps_the_agents_own_level(local_models):
    root, _requests = local_models
    _reasoning_models()
    _settings(home.path("settings"), modelThinkingLevels={"fixture-global/global": "high"})
    _settings(home.path("roles_root") / "last_order" / "settings.json", defaultThinkingLevel="low")

    async with opened(root, "last_order", "foreground") as (_, session):
        await session.setModel(session.modelRegistry.find("fixture-global", "global"))
        assert session.model.provider == "fixture-global"
        assert session.thinkingLevel == "low"


def test_a_failed_save_changes_neither_the_file_nor_the_session():
    calls, errors = [], []

    def refuse(_level):
        raise ValueError("settings.json does not parse")

    mode = NS(settingsManager=NS(setDefaultThinkingLevel=refuse, getModelProfile=lambda: "/p"),
              session=NS(setThinkingLevel=lambda *args: calls.append(args)),
              footer=NS(invalidate=lambda: None), updateEditorBorderColor=lambda: None,
              showStatus=lambda text: calls.append(("status", text)), showError=errors.append)
    interactive_mode.InteractiveMode.selectThinkingLevel(mode, "high", True)
    assert calls == [] and errors == ["Thinking level not saved: settings.json does not parse"]

    saved = []
    mode.settingsManager = NS(setDefaultThinkingLevel=saved.append, getModelProfile=lambda: "/p")
    interactive_mode.InteractiveMode.selectThinkingLevel(mode, "high", True)
    assert saved == ["high"]
    assert calls == [("high",), ("status", "Default thinking level for this agent: high")]


def test_a_new_sister_can_be_given_her_own_level(tmp_path):
    ok, message = roster.create_sister("10077", root=str(tmp_path), thinking="high")
    assert ok and "Default thinking level: high" in message
    assert json.loads((tmp_path / "10077" / "settings.json").read_text(encoding="utf-8")) == {"defaultThinkingLevel": "high"}

    ok, message = roster.create_sister("10078", root=str(tmp_path), thinking="loud")
    assert not ok and "unknown thinking level 'loud'" in message
    assert not (tmp_path / "10078").exists()

    ok, _message = roster.create_sister("10079", root=str(tmp_path))
    assert ok and not (tmp_path / "10079" / "settings.json").exists()


@pytest.mark.parametrize("level", ["", None, 3])
def test_a_role_without_a_usable_level_keeps_none(tmp_path, level):
    role = tmp_path / "10032"
    role.mkdir()
    (role / "settings.json").write_text(json.dumps({"defaultThinkingLevel": level}), encoding="utf-8")
    manager = SettingsManager.forRole(str(role))
    assert manager.getRoleThinkingLevel() is None
