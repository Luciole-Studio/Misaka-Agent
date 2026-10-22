"""``misaka/core/settings_defaults.py`` (pi 1.2 ``settings-defaults.ts``) and the accessors that read it: an unset
setting reads its default, the role layer and MISAKA's own defaults stay as they were, and pi 1.3's
``sessionShareDestination`` is a global setting."""

from __future__ import annotations

import json

import pytest

from misaka.ai.utils.retry import DEFAULT_MAX_AGENT_RETRY_DELAY_MS
from misaka.core.project_trust import resolveProjectTrusted
from misaka.core.settings_defaults import SETTINGS_DEFAULTS
from misaka.core.settings_manager import ROLE_KEYS, SettingsManager


def test_defaults_are_read_only_all_the_way_down():
    with pytest.raises(TypeError):
        SETTINGS_DEFAULTS["transport"] = "sse"  # type: ignore[index]
    with pytest.raises(TypeError):
        SETTINGS_DEFAULTS["retry"]["provider"]["maxRetryDelayMs"] = 1  # type: ignore[index]


def test_an_empty_settings_file_reads_every_default():
    manager = SettingsManager.inMemory()

    assert manager.getTransport() == SETTINGS_DEFAULTS["transport"] == "auto"
    assert manager.getSessionShareDestination() == "auto"
    assert manager.getSteeringMode() == manager.getFollowUpMode() == "one-at-a-time"
    assert manager.getCompactionSettings() == {"enabled": True, "reserveTokens": 16384, "keepRecentTokens": 20000}
    assert manager.getBranchSummarySettings() == {"reserveTokens": 16384, "skipPrompt": False}
    assert manager.getBranchSummarySkipPrompt() is False
    assert manager.getRetrySettings() == {
        "enabled": True, "maxRetries": 3, "baseDelayMs": 2000, "maxAgentDelayMs": DEFAULT_MAX_AGENT_RETRY_DELAY_MS,
    }
    assert manager.getProviderRetrySettings() == {"timeoutMs": None, "maxRetries": None, "maxRetryDelayMs": 60000}
    assert manager.getCacheWarmingMode() == "streaming"
    assert manager.getHideThinkingBlock() is False
    assert manager.getShowCacheMissNotices() is False
    assert manager.getQuietStartup() is False
    assert manager.getDefaultProjectTrust() == "ask"
    assert manager.getCollapseChangelog() is False
    assert manager.getEnableSkillCommands() is True
    assert (manager.getShowImages(), manager.getImageWidthCells(), manager.getShowTerminalProgress()) == (True, 60, False)
    assert (manager.getImageAutoResize(), manager.getBlockImages()) == (True, False)
    assert (manager.getDoubleEscapeAction(), manager.getTreeFilterMode()) == ("tree", "default")
    assert (manager.getEditorPaddingX(), manager.getOutputPad(), manager.getAutocompleteMaxVisible()) == (0, 1, 5)
    assert (manager.getCodeBlockIndent(), manager.getMermaidRenderingMode()) == ("  ", "streaming")
    assert SETTINGS_DEFAULTS["codemode"] == {"mode": "on", "inlineBudget": 3000}
    assert SETTINGS_DEFAULTS["warnings"]["anthropicExtraUsage"] is True


def test_install_telemetry_keeps_misakas_default_off():
    # MISAKA fork: the key names the client "misaka" to gateways; upstream's install ping defaults on.
    assert SETTINGS_DEFAULTS["enableInstallTelemetry"] is False
    assert SettingsManager.inMemory().getEnableInstallTelemetry() is False
    assert SettingsManager.inMemory({"enableInstallTelemetry": True}).getEnableInstallTelemetry() is True
    # No pi feature misaka lacks has a default.
    assert {"enableAnalytics", "tuiMode", "fullscreenExitOutput"}.isdisjoint(SETTINGS_DEFAULTS)


def test_clear_on_shrink_reads_the_setting_then_misakas_variable_then_the_default(monkeypatch):
    monkeypatch.delenv("MISAKA_CLEAR_ON_SHRINK", raising=False)
    assert SettingsManager.inMemory().getClearOnShrink() is False
    monkeypatch.setenv("MISAKA_CLEAR_ON_SHRINK", "1")
    assert SettingsManager.inMemory().getClearOnShrink() is True
    assert SettingsManager.inMemory({"terminal": {"clearOnShrink": False}}).getClearOnShrink() is False
    monkeypatch.setenv("PI_CLEAR_ON_SHRINK", "1")
    monkeypatch.delenv("MISAKA_CLEAR_ON_SHRINK")
    assert SettingsManager.inMemory().getClearOnShrink() is False


@pytest.mark.parametrize(("stored", "expected"), [
    (None, "auto"), ("auto", "auto"), ("gist", "gist"), ("radius", "radius"), ("Gist", "auto"), (3, "auto"),
])
def test_session_share_destination_takes_only_its_values(stored, expected):
    settings = {} if stored is None else {"sessionShareDestination": stored}
    assert SettingsManager.inMemory(settings).getSessionShareDestination() == expected


def test_session_share_destination_is_saved_globally_even_in_a_role_session(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    role = tmp_path / "role"
    role.mkdir()
    manager = SettingsManager.create(str(tmp_path), str(home), {"projectTrusted": False})
    manager.bindRole(str(role))

    manager.setSessionShareDestination("gist")

    assert "sessionShareDestination" not in ROLE_KEYS
    assert manager.getSessionShareDestination() == "gist"
    assert json.loads((home / "settings.json").read_text(encoding="utf-8")) == {"sessionShareDestination": "gist"}
    assert not (role / "settings.json").exists()


def test_a_roles_own_thinking_level_still_wins_over_the_default(tmp_path):
    role = tmp_path / "role"
    role.mkdir()
    (role / "settings.json").write_text(json.dumps({"defaultThinkingLevel": "xhigh"}), encoding="utf-8")
    manager = SettingsManager.inMemory({"defaultThinkingLevel": "low"})
    manager.bindRole(str(role))

    assert manager.getRoleThinkingLevel() == "xhigh"
    assert manager.getDefaultThinkingLevel() == "xhigh"


async def test_project_trust_without_a_setting_asks(tmp_path):
    from misaka.config import home

    project = tmp_path / "project"
    (project / home.DIR_NAME).mkdir(parents=True)
    (project / home.DIR_NAME / "settings.json").write_text("{}", encoding="utf-8")

    class _Store:
        def get(self, _cwd):
            return None

    asked = []

    class _Ui:
        async def select(self, title, options):
            asked.append(title)

    # With no setting, "ask": the user is asked; a dismissed prompt leaves the project untrusted.
    trusted = await resolveProjectTrusted({
        "cwd": str(project), "trustStore": _Store(), "projectTrustContext": {"hasUI": True, "ui": _Ui()},
    })
    assert trusted is False and len(asked) == 1
    assert SETTINGS_DEFAULTS["defaultProjectTrust"] == "ask"


def test_cache_warming_modes_satisfy_the_schema_type():
    # `CACHE_WARMING_MODES ... as const satisfies readonly CacheWarmingMode[]`.
    from typing import get_args

    from misaka.core.settings_manager import CACHE_WARMING_MODES, CacheWarmingMode

    assert CACHE_WARMING_MODES == get_args(CacheWarmingMode.__value__)


@pytest.mark.parametrize("stored", [["always"], {"mode": "never"}, 3])
def test_a_list_or_object_where_a_mode_belongs_reads_the_default(stored):
    # Upstream compares with `===` / `includes`: an unhashable value is just not a mode.
    manager = SettingsManager.inMemory({"defaultProjectTrust": stored, "treeFilterMode": stored})

    assert manager.getDefaultProjectTrust() == SETTINGS_DEFAULTS["defaultProjectTrust"]
    assert manager.getTreeFilterMode() == SETTINGS_DEFAULTS["treeFilterMode"]
