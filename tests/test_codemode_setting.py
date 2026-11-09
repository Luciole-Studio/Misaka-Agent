"""MISAKA's codemode switches, one for each kind of agent (`roles.<kind>.codemode`): the settings that read and
write them, the new sessions of Last Order and the Sisters that follow them, the sub-agents' loadout, and the three
"Codemode" rows of /settings."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.core.keybindings import KeybindingsManager
from misaka.core.settings_manager import DEFAULT_TOOL_NAMES, SettingsManager
from misaka.core.subagent.child import inherited_tool_names
from misaka.extensions.codemode import createCodemodeExtension
from misaka.modes.interactive.components.settings_selector import (
    SettingsSelectorComponent,
)
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import setKeybindings
from misaka.utils.values import read_field
from tests.suite_harness import createHarness
from tests.test_settings_selector import makeCallbacks, makeConfig


@pytest.fixture
def roles(tmp_path):
    lastOrder, sister = tmp_path / "profiles" / "last_order", tmp_path / "profiles" / "sisters" / "10032"
    lastOrder.mkdir(parents=True)
    sister.mkdir(parents=True)
    return SimpleNamespace(lastOrder=str(lastOrder), sister=str(sister))


def bound(settings: dict, profile: str | None) -> SettingsManager:
    manager = SettingsManager.inMemory(settings)
    manager.bindRole(profile)
    return manager


# -- settings ----------------------------------------------------------------------------------


def test_unset_switches_are_what_each_kind_does_today():
    manager = SettingsManager.inMemory()
    assert [manager.getRoleCodemode(kind) for kind in ("lastOrder", "sisters", "subagents")] == [False, False, True]
    # Last Order and the Sisters follow a hand-written `defaultTools` while their switch is unset.
    manager = SettingsManager.inMemory({"defaultTools": ["+codemode"]})
    assert [manager.getRoleCodemode(kind) for kind in ("lastOrder", "sisters", "subagents")] == [True, True, True]


def test_a_switch_is_written_to_the_home_for_its_kind_alone():
    manager = SettingsManager.inMemory({"roles": {"sisters": {"codemode": True}}})
    manager.setRoleCodemode("lastOrder", True)
    manager.setRoleCodemode("subagents", False)
    assert manager.getGlobalSettings()["roles"] == {
        "sisters": {"codemode": True}, "lastOrder": {"codemode": True}, "subagents": {"codemode": False},
    }
    assert [manager.getRoleCodemode(kind) for kind in ("lastOrder", "sisters", "subagents")] == [True, True, False]
    with pytest.raises(ValueError):
        manager.setRoleCodemode("last-order", True)


def test_the_bound_role_takes_its_kinds_switch_over_default_tools(roles):
    switches = {"roles": {"lastOrder": {"codemode": True}, "sisters": {"codemode": False}}}
    assert bound(switches, roles.lastOrder).getRoleType() == "lastOrder"
    assert bound(switches, roles.lastOrder).getDefaultTools() == [*DEFAULT_TOOL_NAMES, "codemode"]
    sister = bound({**switches, "defaultTools": ["-ls", "+codemode"]}, roles.sister)
    assert sister.getRoleType() == "sisters"
    assert sister.getDefaultTools() == [name for name in DEFAULT_TOOL_NAMES if name != "ls"]


def test_without_its_switch_or_a_role_a_session_follows_default_tools_alone(roles):
    assert bound({"roles": {"sisters": {"codemode": True}}}, roles.lastOrder).getDefaultTools() is None
    unbound = bound({"roles": {"lastOrder": {"codemode": True}, "sisters": {"codemode": True}}}, None)
    assert unbound.getRoleType() is None
    assert unbound.getDefaultTools() is None


# -- new sessions ------------------------------------------------------------------------------


@asynccontextmanager
async def newSession(tmp_path, settings: dict, profile: str):
    from misaka.ai.compat import getModels
    from misaka.core.auth_storage import AuthStorage
    from misaka.core.model_runtime import ModelRuntime
    from misaka.core.resource_loader import DefaultResourceLoader
    from misaka.core.sdk import createAgentSession
    from misaka.core.session_manager import SessionManager

    workspace, agentDir = tmp_path / "workspace", tmp_path / "agent"
    workspace.mkdir(exist_ok=True)
    agentDir.mkdir(exist_ok=True)
    manager = SettingsManager.inMemory({"compaction": {"enabled": False}, "retry": {"enabled": False}, **settings})
    base = {"cwd": str(workspace), "agentDir": str(agentDir)}
    loader = DefaultResourceLoader({**base, "settingsManager": manager,
                                    "extensionFactories": [createCodemodeExtension()]})
    await loader.reload()
    model = next(m for m in getModels("anthropic") if m.id == "claude-opus-5")
    runtime = await ModelRuntime.create({"authStorage": AuthStorage.inMemory(), "modelsPath": None})
    await runtime.setRuntimeApiKey(model.provider, "fixture")
    result = await createAgentSession({
        **base, "resourceLoader": loader, "model": model, "modelRuntime": runtime, "settingsManager": manager,
        "sessionManager": SessionManager.inMemory(str(workspace)), "modelProfile": profile,
    })
    session = result["session"]
    try:
        yield session
    finally:
        session.dispose()


async def test_new_sessions_of_last_order_and_a_sister_start_as_their_switches_say(tmp_path, roles):
    switches = {"roles": {"lastOrder": {"codemode": True}}}
    async with newSession(tmp_path, switches, roles.lastOrder) as lastOrder:
        assert "codemode" in lastOrder.getActiveToolNames()
    async with newSession(tmp_path, switches, roles.sister) as sister:
        assert "codemode" not in sister.getActiveToolNames()
        assert set(DEFAULT_TOOL_NAMES) <= set(sister.getActiveToolNames())


@pytest.mark.parametrize(("declared", "switch"), [(True, False), (False, True)])
async def test_a_resumed_session_keeps_the_tools_its_transcript_declared(tmp_path, roles, monkeypatch, declared, switch):
    from misaka.core.session_manager import SessionManager

    async with createHarness(extensionFactories=[createCodemodeExtension()]) as harness:
        if declared:
            harness.session.setActiveToolsByName([*harness.session.getActiveToolNames(), "codemode"])
        harness.setResponses([fauxAssistantMessage("one")])
        await harness.session.prompt("first")
        transcript = harness.session.sessionManager
    monkeypatch.setattr(SessionManager, "inMemory", staticmethod(lambda *_args, **_kwargs: transcript))
    async with newSession(tmp_path, {"roles": {"lastOrder": {"codemode": switch}}}, roles.lastOrder) as resumed:
        assert ("codemode" in resumed.getActiveToolNames()) is declared


# -- sub-agents and cards that start with every tool --------------------------------------------


def child(settings: dict, *, active: list[str], tools=("read", "bash", "codemode", "web_search")):
    return SimpleNamespace(
        getAllTools=lambda: [SimpleNamespace(name=name) for name in tools],
        getActiveToolNames=lambda: list(active),
        settingsManager=SettingsManager.inMemory(settings),
        sessionManager=SimpleNamespace(getBranch=list),
    )


def test_a_sub_agent_keeps_codemode_unless_its_switch_is_off():
    assert "codemode" in inherited_tool_names(child({}, active=["read"]), card=False, fork=False)
    off = {"roles": {"subagents": {"codemode": False}}}
    assert inherited_tool_names(child(off, active=["read"]), card=False, fork=False) == ["read", "bash", "web_search"]
    # The switch is more specific than `defaultTools`, which put codemode in the starting tools here.
    assert "codemode" not in inherited_tool_names(
        child({**off, "defaultTools": ["+codemode"]}, active=["read", "codemode"]), card=False, fork=False)


def test_an_mcp_server_that_turned_codemode_on_keeps_it_on():
    off = {"roles": {"subagents": {"codemode": False}}}
    assert "codemode" in inherited_tool_names(child(off, active=["read", "codemode"]), card=False, fork=False)


def test_a_card_keeps_codemode_as_its_sisters_settings_started_it():
    assert "codemode" not in inherited_tool_names(child({}, active=["read"]), card=True, fork=False)
    assert "codemode" in inherited_tool_names(child({}, active=["read", "codemode"]), card=True, fork=False)


async def test_a_resumed_sub_agent_keeps_the_codemode_its_transcript_declared():
    # Taking a declared tool back is a `tool_removal` on the wire: the switch is for new sessions only. Here
    # `defaultTools` put codemode in the starting tools, so it is not an MCP server's doing either.
    off = {"defaultTools": ["+codemode"], "roles": {"subagents": {"codemode": False}}}
    async with createHarness(settings=off, extensionFactories=[createCodemodeExtension()]) as harness:
        session = harness.session
        session.setActiveToolsByName([*session.getActiveToolNames(), "codemode"])
        harness.setResponses([fauxAssistantMessage("one"), fauxAssistantMessage("two")])
        await session.prompt("first")
        names = inherited_tool_names(session, card=False, fork=False)
        assert "codemode" in names
        session.setActiveToolsByName(names)
        await session.prompt("second")
        assert "codemode" not in [
            read_field(tool, "name") for message in session.messages if read_field(message, "role") == "system"
            for tool in (read_field(message, "toolsRemoved") or [])
        ]


def test_a_fork_keeps_its_parents_tools():
    off = {"roles": {"subagents": {"codemode": False}}}
    names = inherited_tool_names(child(off, active=[]), card=False, fork=True)
    assert names == ["read", "bash", "codemode", "web_search"]


# -- /settings ---------------------------------------------------------------------------------


@pytest.fixture
def keybindings():
    initTheme("dark")
    setKeybindings(KeybindingsManager())


def test_three_codemode_rows_follow_skill_commands_and_set_their_kind(keybindings):
    onCodemodeChange = MagicMock()
    list = SettingsSelectorComponent(
        makeConfig(codemode={"lastOrder": False, "sisters": True, "subagents": True}),
        makeCallbacks(onCodemodeChange=onCodemodeChange),
    ).getSettingsList()
    ids = [item.id for item in list.items]
    start = ids.index("skill-commands") + 1
    assert ids[start:start + 4] == [
        "codemode-lastOrder", "codemode-sisters", "codemode-subagents", "show-hardware-cursor",
    ]
    assert [item.currentValue for item in list.items[start:start + 3]] == ["false", "true", "true"]

    list.selectItem("codemode-lastOrder")
    list.handleInput("\r")
    list.selectItem("codemode-subagents")
    list.handleInput("\r")
    assert [call.args for call in onCodemodeChange.call_args_list] == [("lastOrder", True), ("subagents", False)]
