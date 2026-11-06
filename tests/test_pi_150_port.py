"""pi 1.3.0 -> 1.5.0, the tool loadout only: what a resumed branch activates (bd3c9b82, #11062, #11075) and that
reordering the same tools changes nothing (b9e3eef6, #11495).

Upstream cases, translated: default-tools-setting and system-prompt-updates. MISAKA's own: where pi has no counterpart
-- a tool scope, a part's projection, a role session, a fork child -- and `/tree` keeping `--tools -name` off.
"""

from __future__ import annotations

import time

from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.ai.types import SystemMessage, TextContent, TranscriptContext
from misaka.core.extensions.types import ToolDefinition
from misaka.core.platform.toolkit import tool_definition
from misaka.core.session_manager import SessionManager
from misaka.core.subagent import fork
from misaka.utils.values import read_field
from tests.suite_harness import createHarness
from tests.test_pi_110_port import _inactive_and_active_tools
from tests.test_pi_130_port import _changed_session, _recorded_session
from tests.test_prompt_assembly_integration import (  # noqa: F401 - a fixture
    assembled,
    prompt_home,
)
from tests.test_tool_search import _assistant, _scripted, session_with


def _record(manager: SessionManager, added: list[str], removed: list[str] | None = None) -> str:
    """Append a system message that adds and removes tools, as a run records loadout changes."""
    message: dict = {"role": "system", "content": "", "timestamp": int(time.time() * 1000)}
    if added:
        message["toolsAdded"] = [
            {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}} for name in added
        ]
    if removed:
        message["toolsRemoved"] = [{"name": name} for name in removed]
    return manager.appendMessage(message)


def _user(manager: SessionManager, text: str = "hi") -> str:
    return manager.appendMessage({"role": "user", "content": text, "timestamp": int(time.time() * 1000)})


def _system_messages(session) -> list:
    return [message for message in session.messages if read_field(message, "role") == "system"]


def _replies(texts: str):
    return _scripted([_assistant([TextContent(text=text)]) for text in texts])


def _tool_with_snippet(name: str, **options) -> ToolDefinition:
    """A tool the prompt lists, so its place in the loadout shows in the prompt."""
    return ToolDefinition(name=name, label=name, description=name, parameters={"type": "object", "properties": {}},
                          execute=None, promptSnippet=f"{name} snippet", **options)


async def _last_order(home, name: str, manager: SessionManager):
    """A Last Order session as a chat opens it, `session_start` included."""
    from misaka.ai.compat import getModels
    from misaka.core.auth_storage import AuthStorage
    from misaka.core.model_runtime import ModelRuntime
    from misaka.core.resource_loader import DefaultResourceLoader
    from misaka.core.sdk import createAgentSession
    from misaka.core.wiring import role_session_setup

    workspace = home / "workspace"
    workspace.mkdir(exist_ok=True)
    _flags, assembly, _env = role_session_setup(str(home / "profiles" / "last_order"), str(workspace), receive_messages=True)
    agent_dir = home / "agent" / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    options = {"cwd": str(workspace), "agentDir": str(agent_dir)}
    loader = DefaultResourceLoader({**options, "extensionFactories": assembly.extension_factories})
    await loader.reload()
    model = next(model for model in getModels("anthropic") if model.id == "claude-opus-5")
    runtime = await ModelRuntime.create({"authStorage": AuthStorage.inMemory(), "modelsPath": None})
    await runtime.setRuntimeApiKey(model.provider, "fixture")
    created = await createAgentSession({**options, "resourceLoader": loader, "customTools": assembly.custom_tools, "model": model,
                                        "modelRuntime": runtime, "parts": assembly.parts, "sessionManager": manager})
    await created["session"].bindExtensions({})
    return created["session"]


# --- default-tools-setting: resume and /tree (#11062, #11075) -----------------------------------------------------


async def test_activates_extension_tools_the_transcript_never_added_or_removed(tmp_path):
    # Built-in tools and tools registered inactive stay inactive.
    manager = _recorded_session(tmp_path, ["read", "grep"])
    async with session_with(tmp_path, [_inactive_and_active_tools], sessionManager=manager) as session:
        assert sorted(session.getActiveToolNames()) == ["active_tool", "grep", "read"]


async def test_built_in_tools_a_transcript_never_mentioned_stay_off(tmp_path):
    manager = _recorded_session(tmp_path, ["read"])
    async with session_with(tmp_path, [_inactive_and_active_tools], sessionManager=manager) as session:
        assert sorted(session.getActiveToolNames()) == ["active_tool", "read"]


async def test_keeps_tools_removed_before_a_compaction_inactive(tmp_path):
    manager = _changed_session(tmp_path)
    manager.appendCompaction("summary", _user(manager), 1000)
    async with session_with(tmp_path, [_inactive_and_active_tools], sessionManager=manager) as session:
        assert sorted(session.getActiveToolNames()) == ["grep", "inactive_tool", "read"]


async def test_restores_the_target_branch_s_loadout_on_tree_navigation(tmp_path):
    # The target branch never mentions active_tool, so it is active there. It removed grep.
    manager = _recorded_session(tmp_path, ["read", "grep"])
    _record(manager, [], ["grep"])
    target = _user(manager, "first")
    _record(manager, ["grep"], ["active_tool"])
    async with session_with(tmp_path, [_inactive_and_active_tools], sessionManager=manager) as session:
        assert sorted(session.getActiveToolNames()) == ["grep", "read"]
        await session.navigateTree(target, {"summarize": False})
        assert sorted(session.getActiveToolNames()) == ["active_tool", "read"]


async def test_keeps_registered_tools_outside_the_tools_allowlist_inactive_on_tree_navigation(tmp_path):
    # An allowlist without mcp__ entries keeps MCP tools registered but inactive.
    manager = _recorded_session(tmp_path, ["read"])
    target = _user(manager, "first")
    lookup = tool_definition(name="mcp__server__lookup", label="Lookup", description="MCP tool", parameters={}, execute=None)
    async with session_with(tmp_path, [], sessionManager=manager, tools=["read"], customTools=[lookup]) as session:
        assert session.getActiveToolNames() == ["read"]
        await session.navigateTree(target, {"summarize": False})
        assert session.getActiveToolNames() == ["read"]


# --- system-prompt-updates: order (#11495) ------------------------------------------------------------------------


async def test_does_not_patch_the_prompt_when_the_active_tools_are_only_reordered():
    async with createHarness() as harness:
        harness.setResponses([fauxAssistantMessage("first"), fauxAssistantMessage("second")])
        await harness.session.prompt("one")
        before = harness.session.getActiveToolNames()
        harness.session.setActiveToolsByName(list(reversed(before)))
        assert harness.session.getActiveToolNames() == before
        await harness.session.prompt("two")
        assert len(_system_messages(harness.session)) == 1


def _handler_swapping_its_tools(pi) -> None:
    pi.registerTool(_tool_with_snippet("x_tool", defaultActive=False))
    pi.registerTool(_tool_with_snippet("y_tool", defaultActive=False))
    runs = []

    async def before(event, _ctx):
        runs.append(1)
        options = event["systemPromptOptions"]
        others = [name for name in options["selectedTools"] if name not in ("x_tool", "y_tool")]
        options["selectedTools"] = [*others, *(("x_tool", "y_tool") if len(runs) == 1 else ("y_tool", "x_tool"))]
        return {"systemPromptOptions": options}

    pi.on("before_agent_start", before)


async def test_a_handler_reordering_tools_it_added_does_not_patch_the_prompt(tmp_path):
    # The commit says this path is covered: the tools are outside the selection, so the order is the active tools'.
    async with session_with(tmp_path, [_handler_swapping_its_tools]) as session:
        await session.bindExtensions({})
        session.agent.streamFunction = _replies("ab")
        await session.prompt("one")
        before = session.getActiveToolNames()
        await session.prompt("two")
        assert session.getActiveToolNames() == before
        assert len(_system_messages(session)) == 1


def _handler_giving_a_tool_back(pi) -> None:
    runs = []

    async def before(event, _ctx):
        runs.append(1)
        options = event["systemPromptOptions"]
        others = [name for name in options["selectedTools"] if name != "read"]
        options["selectedTools"] = others if len(runs) == 1 else [*others, "read"]
        return {"systemPromptOptions": options}

    pi.on("before_agent_start", before)


async def test_a_tool_a_handler_gave_back_is_appended(tmp_path):
    async with session_with(tmp_path, [_handler_giving_a_tool_back]) as session:
        await session.bindExtensions({})
        session.agent.streamFunction = _replies("ab")
        await session.prompt("one")
        await session.prompt("two")
        assert session.getActiveToolNames()[-1] == "read"


async def test_newly_active_tools_are_appended(tmp_path):
    async with session_with(tmp_path, [_inactive_and_active_tools]) as session:
        before = session.getActiveToolNames()
        session.setActiveToolsByName(["inactive_tool", *reversed(before)])
        assert session.getActiveToolNames() == [*before, "inactive_tool"]


# --- MISAKA: the rule where pi has no counterpart -----------------------------------------------------------------


async def test_a_scope_gives_the_selection_back_in_its_order_with_its_prompt(tmp_path):
    # pi orders by the active tools: what a scope hid would come back last, and the prompt would differ.
    async with session_with(tmp_path, [_inactive_and_active_tools]) as session:
        before, prompt = session.getActiveToolNames(), session.systemPrompt
        with session.toolScope([name for name in before if name not in ("read", "edit")]):
            assert "read" not in session.getActiveToolNames()
        assert session.getActiveToolNames() == before
        assert session.systemPrompt == prompt


async def test_a_reordered_selection_does_not_come_back_from_a_scope(tmp_path):
    async with session_with(tmp_path, [_inactive_and_active_tools]) as session:
        before, prompt = session.getActiveToolNames(), session.systemPrompt
        session.setActiveToolsByName(list(reversed(before)))
        with session.toolScope(before[:2]):
            pass
        assert session.getActiveToolNames() == before
        assert session.systemPrompt == prompt


async def test_reordering_inside_a_scope_changes_nothing(tmp_path):
    async with session_with(tmp_path, [_inactive_and_active_tools]) as session:
        with session.toolScope(["read", "write", "bash"]):
            session.setActiveToolsByName(["bash", "write", "read"])
            assert session.getActiveToolNames() == ["read", "write", "bash"]
            session.refreshTools()      # the scope lays its own loadout out again
            assert session.getActiveToolNames() == ["read", "write", "bash"]


async def test_a_projection_ending_inside_a_scope_gives_its_tool_back_in_place(tmp_path):
    async with session_with(tmp_path, [_inactive_and_active_tools]) as session:
        project, hidden = session.moments.project_tools, {"write"}
        session.moments.project_tools = lambda tools: [tool for tool in project(tools) if tool.name not in hidden]
        with session.toolScope(["read", "write", "bash"]):
            assert session.getActiveToolNames() == ["read", "bash"]
            hidden.clear()
            session.refreshTools()
            assert session.getActiveToolNames() == ["read", "write", "bash"]


async def test_a_nested_scope_gives_the_outer_scope_back_in_its_order(tmp_path):
    async with session_with(tmp_path, [_inactive_and_active_tools]) as session:
        before = session.getActiveToolNames()
        with session.toolScope(["read", "write", "bash"]):
            with session.toolScope(["bash"]):
                assert session.getActiveToolNames() == ["bash"]
            assert session.getActiveToolNames() == ["read", "write", "bash"]
        assert session.getActiveToolNames() == before


async def test_a_restore_inside_a_scope_keeps_the_selection(tmp_path):
    manager = _recorded_session(tmp_path, ["read", "grep", "write"])
    target = _user(manager, "first")
    _record(manager, ["edit"], ["write"])
    async with session_with(tmp_path, [_inactive_and_active_tools], sessionManager=manager) as session:
        selection = session.getActiveToolNames()
        assert "edit" in selection and "write" not in selection
        with session.toolScope(["read"]):
            await session.navigateTree(target, {"summarize": False})
            assert session.getActiveToolNames() == ["read"]
        assert session.getActiveToolNames() == selection


async def test_a_restore_inside_a_scope_does_not_declare_a_callable_tool(tmp_path):
    # `callable` lets scripts reach a tool without declaring it; a tool new to the branch is not an exception.
    manager = _recorded_session(tmp_path, ["read", "grep"])
    target = _user(manager, "first")
    async with session_with(tmp_path, [], sessionManager=manager, customTools=[_tool_with_snippet("mcp__never__seen")]) as session:
        session.setActiveToolsByName(["read", "grep"])
        with session.toolScope(["read"], callable=["mcp__*"]):
            await session.navigateTree(target, {"summarize": False})
            assert session.getActiveToolNames() == ["read"]


async def test_a_restore_inside_a_scope_applies_a_minus_tool(tmp_path):
    manager = _recorded_session(tmp_path, ["read", "grep"])
    target = _user(manager, "first")
    async with session_with(tmp_path, [], sessionManager=manager, tools=["-grep"]) as session:
        with session.toolScope(["read", "grep"]):
            await session.navigateTree(target, {"summarize": False})
            assert session.getActiveToolNames() == ["read"]


async def test_tree_navigation_keeps_a_minus_tool_off(tmp_path):
    # MISAKA fork: pi applies the modifiers on resume only, so its `/tree` turns this tool on.
    manager = _recorded_session(tmp_path, ["read", "grep"])
    target = _user(manager, "first")
    async with session_with(tmp_path, [_inactive_and_active_tools], sessionManager=manager, tools=["-active_tool"]) as session:
        assert "active_tool" not in session.getActiveToolNames()
        await session.navigateTree(target, {"summarize": False})
        assert "active_tool" not in session.getActiveToolNames()


async def test_tree_navigation_does_not_turn_a_plus_tool_back_on(tmp_path):
    # A `+name` is what the session started with, not a restriction: a branch that removed the tool wins.
    manager = _recorded_session(tmp_path, ["read", "inactive_tool"])
    _user(manager, "first")
    _record(manager, [], ["inactive_tool"])
    target = _user(manager, "second")
    async with session_with(tmp_path, [_inactive_and_active_tools], sessionManager=manager, tools=["+inactive_tool"]) as session:
        assert "inactive_tool" in session.getActiveToolNames()
        await session.navigateTree(target, {"summarize": False})
        assert "inactive_tool" not in session.getActiveToolNames()


async def test_a_refresh_keeps_the_order_a_handler_and_an_extension_left(tmp_path):
    # The handler moves `read` out for one run, then it comes back last. The selection does not see that.
    async with session_with(tmp_path, [_handler_giving_a_tool_back]) as session:
        await session.bindExtensions({})
        session.agent.streamFunction = _replies("abc")
        await session.prompt("one")
        await session.prompt("two")
        before = session.getActiveToolNames()
        assert before[-1] == "read"
        session.refreshTools()
        assert session.getActiveToolNames() == before


async def test_a_refresh_after_an_extension_gave_a_tool_back_does_not_patch_the_prompt(tmp_path):
    runs = []

    def extension(pi) -> None:
        async def before(event, _ctx):
            runs.append(1)
            if len(runs) == 1:
                options = event["systemPromptOptions"]
                options["selectedTools"] = [name for name in options["selectedTools"] if name != "read"]
                return {"systemPromptOptions": options}

        pi.on("before_agent_start", before)

    async with session_with(tmp_path, [extension]) as session:
        await session.bindExtensions({})
        session.agent.streamFunction = _replies("abc")
        await session.prompt("one")
        session.setActiveToolsByName([*session.getActiveToolNames(), "read"])
        await session.prompt("two")
        before, patches = session.getActiveToolNames(), len(_system_messages(session))
        assert before[-1] == "read"
        session.refreshTools()
        assert session.getActiveToolNames() == before
        await session.prompt("three")
        assert len(_system_messages(session)) == patches


async def test_a_projection_gives_its_tools_back_in_place_with_the_prompt(prompt_home):  # noqa: F811
    from misaka.core.research.tool_policy import DRIVER_OWNED_TOOLS
    from misaka.core.research.window import WindowLO

    async with assembled(prompt_home, "last_order", "foreground") as session:
        before, prompt = session.getActiveToolNames(), session.systemPrompt
        owner = WindowLO(session, lambda: None, describe=dict)
        try:
            assert not DRIVER_OWNED_TOOLS.intersection(session.getActiveToolNames())
        finally:
            await owner.close()
        assert session.getActiveToolNames() == before
        assert session.systemPrompt == prompt


async def test_a_resume_after_a_scope_keeps_the_order_and_writes_no_prompt_update(tmp_path):
    # A scope removes tools and gives them back, so replaying the transcript puts them last, where they
    # never were. The resumed session must keep its order, or its first run would record a prompt update.
    (tmp_path / "workspace").mkdir(exist_ok=True)
    manager = SessionManager.create(str(tmp_path / "workspace"), str(tmp_path / "sessions"))

    def written(entries) -> list:
        return [entry for entry in entries if read_field(entry, "type") == "message"
                and read_field(read_field(entry, "message"), "role") == "system"]

    async with session_with(tmp_path, [_inactive_and_active_tools], sessionManager=manager) as session:
        session.agent.streamFunction = _scripted([_assistant([TextContent(text=text)]) for text in "abc"])
        await session.prompt("one")
        before = session.getActiveToolNames()
        assert before[0] == "read"
        with session.toolScope([name for name in before if name != "read"]):
            await session.prompt("two")
        await session.prompt("three")
        assert session.getActiveToolNames() == before
        path = manager.getSessionFile()

    resumed = SessionManager.open(path)
    recorded = len(written(resumed.getEntries()))
    assert recorded > 1, "the scope wrote its loadout changes"
    async with session_with(tmp_path, [_inactive_and_active_tools], sessionManager=resumed) as session:
        session.agent.streamFunction = _scripted([_assistant([TextContent(text="d")])])
        assert session.getActiveToolNames() == before
        await session.prompt("four")
    assert len(written(resumed.getEntries())) == recorded


async def test_last_order_resuming_an_old_chat_gets_the_tools_added_since(prompt_home):  # noqa: F811
    workspace = prompt_home / "workspace"
    workspace.mkdir(exist_ok=True)
    session = await _last_order(prompt_home, "new", SessionManager.inMemory(str(workspace)))
    full = session.getActiveToolNames()
    session.dispose()
    added_since = {"misaka_board", "misaka_dispatch"}
    assert added_since <= set(full)
    old = SessionManager.inMemory(str(workspace))
    _record(old, [name for name in full if name not in added_since])
    session = await _last_order(prompt_home, "old", old)
    active = session.getActiveToolNames()
    session.dispose()
    assert active == full


async def test_reading_tools_are_active_only_while_leading_a_reading(prompt_home, monkeypatch):  # noqa: F811
    # They are `defaultActive=False`, so a chat activates them only by leading a reading.
    from misaka.extensions.reading import cli, engine

    workspace = prompt_home / "workspace"
    workspace.mkdir(exist_ok=True)
    note = workspace / "nora.md"
    note.write_text("# Nora\n\nWhat happens after Nora leaves home?\n", encoding="utf-8")
    reading = await engine.open_reading([str(note)], "What happens?", workspace=str(workspace))

    def reading_tools(session) -> list[str]:
        return [name for name in session.getActiveToolNames() if name.startswith("reading_")]

    plain = await _last_order(prompt_home, "plain", SessionManager.create(str(workspace), str(prompt_home / "sessions")))
    assert reading_tools(plain) == []
    plain.dispose()

    monkeypatch.setenv(cli.ENV_RUN, reading.id)
    leader = await _last_order(prompt_home, "leader", SessionManager.create(str(workspace), str(prompt_home / "sessions")))
    assert len(reading_tools(leader)) == 4
    leader.dispose()


async def test_last_order_keeps_a_restored_tool_waiting_for_its_server_on_resume(prompt_home):  # noqa: F811
    # A deferred tool tool_search loaded: only the pending list brings it back once its server registers it.
    workspace = prompt_home / "workspace"
    workspace.mkdir(exist_ok=True)
    session = await _last_order(prompt_home, "new", SessionManager.inMemory(str(workspace)))
    full = session.getActiveToolNames()
    session.dispose()
    old = SessionManager.inMemory(str(workspace))
    _record(old, [*full, "mcp__server__lookup"])
    session = await _last_order(prompt_home, "old", old)
    session.registerCustomTools([ToolDefinition(
        name="mcp__server__lookup", label="Lookup", description="Lookup", parameters={"type": "object", "properties": {}},
        execute=None, exposure="deferred",
    )])
    active = session.getActiveToolNames()
    session.dispose()
    assert "mcp__server__lookup" in active


async def test_last_order_keeps_a_restored_tool_waiting_for_its_server_on_tree_navigation(prompt_home):  # noqa: F811
    workspace = prompt_home / "workspace"
    workspace.mkdir(exist_ok=True)
    session = await _last_order(prompt_home, "new", SessionManager.inMemory(str(workspace)))
    full = session.getActiveToolNames()
    session.dispose()
    old = SessionManager.inMemory(str(workspace))
    _record(old, [*full, "mcp__server__lookup"])
    target = _user(old, "first")
    _user(old, "second")
    session = await _last_order(prompt_home, "old", old)
    await session.navigateTree(target, {"summarize": False})
    pending = list(session._pendingToolNames)
    session.dispose()
    assert pending == ["mcp__server__lookup"]


async def test_a_fork_request_carries_the_parent_s_tools_in_the_parent_s_order(tmp_path):
    # A fork's cache identity rests on `fork.install` rebuilding the head from the parent's snapshot,
    # whatever the child itself declared.
    async with session_with(tmp_path, [_inactive_and_active_tools]) as session:
        tools = {tool.name: tool for tool in session.agent.state.tools}
        parent = list(reversed(session.getActiveToolNames()))
        snapshot = {"systemPrompt": "PARENT PROMPT", "tools": [
            {"name": name, "description": tools[name].description, "parameters": tools[name].parameters_json_schema()}
            for name in parent
        ]}
        seen = {}

        def capture(_model, context, _options):
            seen["context"] = context

        session.agent.streamFunction = capture
        fork.install(session, snapshot)
        own = SystemMessage(content="CHILD PROMPT", toolsAdded=[tools["read"]], timestamp=1)
        session.agent.streamFunction(None, TranscriptContext(messages=[own]), None)
        head = seen["context"].messages[0]
        assert [tool.name for tool in head.toolsAdded] == parent
        assert head.content == "PARENT PROMPT"


# --- MISAKA: a projection hides tools, it does not remove them ----------------------------------------------------


def _driver_owned_recorded_as_removed(tools: list[str], manager: SessionManager) -> None:
    """The transcript of a chat that ran a research window: the board tools go away and nothing brings them back."""
    from misaka.core.research.tool_policy import DRIVER_OWNED_TOOLS

    _record(manager, tools)
    _user(manager, "before the window")
    _record(manager, ["misaka_research_assign"], sorted(DRIVER_OWNED_TOOLS))
    _user(manager, "in the window")
    _record(manager, [], ["misaka_research_assign"])


async def test_last_order_gets_her_board_tools_back_after_a_research_window(prompt_home):  # noqa: F811
    from misaka.core.research.tool_policy import DRIVER_OWNED_TOOLS

    workspace = prompt_home / "workspace"
    workspace.mkdir(exist_ok=True)
    session = await _last_order(prompt_home, "new", SessionManager.inMemory(str(workspace)))
    full = session.getActiveToolNames()
    session.dispose()
    assert DRIVER_OWNED_TOOLS <= set(full)
    manager = SessionManager.inMemory(str(workspace))
    _driver_owned_recorded_as_removed(full, manager)
    session = await _last_order(prompt_home, "old", manager)
    active = session.getActiveToolNames()
    session.dispose()
    assert active == full


async def test_a_minus_tool_stays_off_after_a_research_window(prompt_home):  # noqa: F811
    from misaka.core.resource_loader import DefaultResourceLoader
    from misaka.core.sdk import createAgentSession
    from misaka.core.wiring import role_session_setup

    workspace = prompt_home / "workspace"
    workspace.mkdir(exist_ok=True)
    session = await _last_order(prompt_home, "new", SessionManager.inMemory(str(workspace)))
    full = session.getActiveToolNames()
    session.dispose()
    manager = SessionManager.inMemory(str(workspace))
    _driver_owned_recorded_as_removed(full, manager)
    _flags, assembly, _env = role_session_setup(str(prompt_home / "profiles" / "last_order"), str(workspace), receive_messages=True)
    agent_dir = prompt_home / "agent" / "minus"
    agent_dir.mkdir(parents=True, exist_ok=True)
    options = {"cwd": str(workspace), "agentDir": str(agent_dir)}
    loader = DefaultResourceLoader({**options, "extensionFactories": assembly.extension_factories})
    await loader.reload()
    created = await createAgentSession({**options, "resourceLoader": loader, "customTools": assembly.custom_tools, "parts": assembly.parts,
                                        "sessionManager": manager, "tools": ["-misaka_dispatch"]})
    active = created["session"].getActiveToolNames()
    created["session"].dispose()
    assert "misaka_dispatch" not in active and "misaka_card" in active


async def test_a_minus_tool_recorded_as_active_stays_off(prompt_home):  # noqa: F811
    # Nearly every Last Order chat: the transcript declares the tool. It must not be counted twice.
    from misaka.core.resource_loader import DefaultResourceLoader
    from misaka.core.sdk import createAgentSession
    from misaka.core.wiring import role_session_setup

    workspace = prompt_home / "workspace"
    workspace.mkdir(exist_ok=True)
    session = await _last_order(prompt_home, "new", SessionManager.inMemory(str(workspace)))
    full = session.getActiveToolNames()
    session.dispose()
    manager = SessionManager.inMemory(str(workspace))
    _record(manager, full)
    target = _user(manager, "first")
    _flags, assembly, _env = role_session_setup(str(prompt_home / "profiles" / "last_order"), str(workspace), receive_messages=True)
    agent_dir = prompt_home / "agent" / "recorded"
    agent_dir.mkdir(parents=True, exist_ok=True)
    options = {"cwd": str(workspace), "agentDir": str(agent_dir)}
    loader = DefaultResourceLoader({**options, "extensionFactories": assembly.extension_factories})
    await loader.reload()
    created = await createAgentSession({**options, "resourceLoader": loader, "customTools": assembly.custom_tools, "parts": assembly.parts,
                                        "sessionManager": manager, "tools": ["-misaka_dispatch"]})
    session = created["session"]
    assert "misaka_dispatch" not in session.getActiveToolNames()
    await session.navigateTree(target, {"summarize": False})
    active = session.getActiveToolNames()
    session.dispose()
    assert "misaka_dispatch" not in active and "misaka_card" in active


async def test_a_tool_joining_an_open_window_leaves_the_hidden_tools_selected(prompt_home):  # noqa: F811
    # What tool_search and MCP do: they can only name the tools they see.
    from misaka.core.research.window import WindowLO

    async with assembled(prompt_home, "last_order", "foreground") as session:
        before = session.getActiveToolNames()
        owner = WindowLO(session, lambda: None, describe=dict)
        try:
            session.setActiveToolsByName([*session.getActiveToolNames(), "tool_search"])
            assert "tool_search" in session.getActiveToolNames()
        finally:
            await owner.close()
        assert session.getActiveToolNames() == [*before, "tool_search"]


async def test_tree_navigation_in_an_open_window_keeps_the_hidden_tools(prompt_home):  # noqa: F811
    from misaka.core.research.window import WindowLO

    workspace = prompt_home / "workspace"
    workspace.mkdir(exist_ok=True)
    manager = SessionManager.create(str(workspace), str(prompt_home / "sessions"))
    session = await _last_order(prompt_home, "tree", manager)
    session.agent.streamFunction = _replies("abc")
    await session.prompt("one")
    first = manager.getLeafId()
    await session.prompt("two")
    before = session.getActiveToolNames()
    owner = WindowLO(session, lambda: None, describe=dict)
    await session.navigateTree(first, {"summarize": False})
    await session.prompt("a turn in the window")
    await owner.close()
    active = session.getActiveToolNames()
    session.dispose()
    assert active == before


async def test_without_a_projection_a_caller_can_still_remove_a_board_tool(prompt_home):  # noqa: F811
    async with assembled(prompt_home, "last_order", "foreground") as session:
        kept = [name for name in session.getActiveToolNames() if name != "misaka_card"]
        session.setActiveToolsByName(kept)
        session.refreshTools()
        assert "misaka_card" not in session.getActiveToolNames()


async def test_a_board_tool_nothing_is_hiding_is_really_removed(prompt_home):  # noqa: F811
    # What keeps a tool selected is a projection hiding it now, not that one could.
    async with assembled(prompt_home, "last_order", "foreground") as session:
        session.agent.state.tools = [tool for tool in session.agent.state.tools if tool.name != "misaka_card"]
        session.setActiveToolsByName(session.getActiveToolNames())
        session.refreshTools()
        assert "misaka_card" not in session.getActiveToolNames()


async def test_a_tool_a_handler_took_out_is_really_removed(tmp_path):
    # Gone once a caller sets the loadout without it, as in pi.
    runs = []

    def extension(pi) -> None:
        async def before(event, _ctx):
            runs.append(1)
            if len(runs) == 1:
                options = event["systemPromptOptions"]
                options["selectedTools"] = [name for name in options["selectedTools"] if name != "read"]
                return {"systemPromptOptions": options}

        pi.on("before_agent_start", before)

    async with session_with(tmp_path, [extension]) as session:
        await session.bindExtensions({})
        session.agent.streamFunction = _replies("a")
        await session.prompt("one")
        assert "read" not in session.getActiveToolNames()
        session.setActiveToolsByName(session.getActiveToolNames())
        session.refreshTools()
        assert "read" not in session.getActiveToolNames()


async def test_a_sister_withholds_nothing(prompt_home):  # noqa: F811
    async with assembled(prompt_home, "sisters/10032", "foreground") as session:
        assert session.moments.withheld_tools() == set()
