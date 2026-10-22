"""B5 extension API: pi coding-agent's ``sdk-stream-options.test.ts`` (provider stream events),
the built-in resolution case of ``package-manager.test.ts``, the ``registerMcpServer`` and
``getSettings`` API, structured content folding, and the fallback call row."""

from __future__ import annotations

import json
import os

import pytest

from misaka.ai.types import AssistantMessage, DoneEvent, TextContent
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.config import home
from misaka.core.extensions.types import ExtensionError
from misaka.core.package_manager import DefaultPackageManager
from misaka.core.settings_manager import SettingsManager
from misaka.core.tools.render_utils import formatToolCallWithArgs
from tests.test_tool_orchestration import USAGE, harness


def _done(model) -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text="ok")], api=model.api, provider=model.provider, model=model.id,
                            usage=USAGE, stopReason="stop", timestamp=1)


# --- sdk-stream-options.test.ts: provider stream events -------------------------------------


async def test_forwards_provider_stream_events_to_extensions(tmp_path):
    provider_event = {"openrouter_metadata": {"strategy": "direct"}}
    extension_events: list = []

    def factory(pi) -> None:
        pi.on("provider_stream_event", lambda event, _ctx: extension_events.append(event))

    async with harness(tmp_path, [factory]) as session:
        await session.bindExtensions({})
        seen_options: list = []

        async def stream(model, context, options=None):
            seen_options.append(options)
            output = AssistantMessageEventStream()
            await options.onProviderStreamEvent(provider_event, model)
            output.push(DoneEvent(reason="stop", message=_done(model)))
            output.end()
            return output

        session.agent.streamFunction = stream
        await session.prompt("test")
        await session.waitForIdle()

        assert callable(seen_options[0].onProviderStreamEvent)
        model = session.model
        assert extension_events == [{
            "data": provider_event, "type": "provider_stream_event",
            "provider": model.provider, "api": model.api, "model": model.id,
        }]


# --- getSettings, registerMcpServer -------------------------------------------------------------


async def test_get_settings_returns_a_copy_of_the_effective_settings(tmp_path):
    captured: dict = {}

    def factory(pi) -> None:
        def on_start(_event, _ctx):
            captured["settings"] = pi.getSettings()
            captured["settings"]["compaction"]["enabled"] = True

        pi.on("session_start", on_start)

    async with harness(tmp_path, [factory]) as session:
        await session.bindExtensions({})
        assert captured["settings"]["compaction"] == {"enabled": True}
        # A copy: the handler's edit did not reach the session's settings.
        assert session.settingsManager.getSettings()["compaction"] == {"enabled": False}


async def test_register_mcp_server_validates_and_reports_when_nothing_connects_it(tmp_path):
    registered: dict = {}

    def factory(pi) -> None:
        pi.registerMcpServer("docs", {"url": "https://mcp.example.com/docs"})
        with pytest.raises(ValueError, match="Invalid MCP server"):
            pi.registerMcpServer("bad name", {"url": "https://x.example"})
        registered["api"] = pi

    async with harness(tmp_path, [factory]) as session:
        errors: list[ExtensionError] = []
        await session.bindExtensions({"onError": errors.append})

        servers = registered["api"].getMcpServers()
        assert [(server.name, server.config) for server in servers] == [("docs", {"url": "https://mcp.example.com/docs"})]
        # No loaded extension handles mcp_servers_change, so the registration is reported once.
        expected = (
            'MCP server "docs" is registered, but no loaded extension connects MCP servers; another '
            "extension may have replaced the built-in MCP support"
        )
        assert [(error.event, error.error) for error in errors] == [("register_mcp_server", expected)]


async def test_registered_mcp_servers_reach_the_extension_that_connects_them(tmp_path):
    changes: list = []
    apis: dict = {}

    def connector(pi) -> None:
        apis["pi"] = pi
        pi.on("mcp_servers_change", lambda event, _ctx: changes.append([server.name for server in event["servers"]]))

    async with harness(tmp_path, [connector]) as session:
        errors: list = []
        await session.bindExtensions({"onError": errors.append})
        apis["pi"].registerMcpServer("jira", {"url": "https://mcp.example.com/jira"})
        apis["pi"].unregisterMcpServer("jira")
        import asyncio

        await asyncio.sleep(0.01)
        assert changes == [["jira"], []]
        assert errors == []


# --- package-manager.test.ts: built-in extension resolution ----------------------------------


async def test_resolves_built_in_extensions_with_user_exclusions_and_project_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "home"))
    cwd = tmp_path / "project"
    agent_dir = tmp_path / "agent"
    (cwd / home.DIR_NAME).mkdir(parents=True)
    agent_dir.mkdir()
    settings = SettingsManager.create(str(cwd), str(agent_dir), {"projectTrusted": True})
    pm = DefaultPackageManager({
        "cwd": str(cwd), "agentDir": str(agent_dir), "settingsManager": settings,
        "builtinExtensions": ["mcp", "llama.cpp"],
    })

    async def builtins():
        return [
            (resource.path, resource.enabled, resource.metadata["source"], resource.metadata["scope"])
            for resource in (await pm.resolve()).extensions
        ]

    assert await builtins() == [
        ("builtin:mcp", True, "builtin", "user"),
        ("builtin:llama.cpp", True, "builtin", "user"),
    ]

    settings.setExtensionPaths(["-builtin:mcp"])
    await settings.flush()
    project_settings = cwd / home.DIR_NAME / "settings.json"
    project_settings.write_text(json.dumps({"extensions": ["+builtin:mcp", "-builtin:llama.cpp"]}), encoding="utf-8")
    await settings.reload()
    assert await builtins() == [
        ("builtin:mcp", True, "builtin", "project"),
        ("builtin:llama.cpp", False, "builtin", "project"),
    ]

    project_settings.write_text(json.dumps({"extensions": []}), encoding="utf-8")
    await settings.reload()
    assert await builtins() == [
        ("builtin:mcp", False, "builtin", "user"),
        ("builtin:llama.cpp", True, "builtin", "user"),
    ]


# #10765. MISAKA fork: pi's case uses the `skills` setting, which misaka does not have (its skills are
# core/skills); prompt templates go through the same pattern matching.
async def test_expands_tilde_in_exclusion_and_exact_patterns(tmp_path, monkeypatch):
    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = tmp_path / "project"
    agent_dir = tmp_path / "agent"
    cwd.mkdir()
    agent_dir.mkdir()
    prompts_dir = tmp_path / "extra" / "prompts"
    prompts_dir.mkdir(parents=True)
    for name in ("keep", "glob-excluded", "exact-excluded"):
        (prompts_dir / f"{name}.md").write_text(f"---\ndescription: {name}\n---\n{name}\n", encoding="utf-8")
    settings = SettingsManager.create(str(cwd), str(agent_dir), {"projectTrusted": True})
    settings.setPromptTemplatePaths([
        "~/extra/prompts",
        "!~/extra/prompts/glob-*",
        "-~/extra/prompts/exact-excluded.md",
    ])
    pm = DefaultPackageManager({"cwd": str(cwd), "agentDir": str(agent_dir), "settingsManager": settings})

    enabled = {os.path.basename(resource.path): resource.enabled for resource in (await pm.resolve()).prompts}

    assert enabled == {"keep.md": True, "glob-excluded.md": False, "exact-excluded.md": False}


# --- structured content ----------------------------------------------------------------------


async def test_a_tool_result_handler_that_replaces_content_drops_structured_content(tmp_path):
    from misaka.agent.types import AgentToolResult
    from misaka.core.extensions.types import ToolDefinition

    outcomes: list = []

    async def lookup(*_args):
        return AgentToolResult(content=[TextContent(text="secret")], details={}, structuredContent={"value": "secret"})

    async def script(_id, _params, _signal, _on_update, ctx):
        outcomes.append(await ctx.executeTool("lookup", {}))
        return AgentToolResult(content=[TextContent(text="ran")], details={})

    def factory(pi) -> None:
        pi.registerTool(ToolDefinition(
            name="lookup", label="lookup", description="Look up.", parameters={"type": "object", "properties": {}},
            outputSchema={"type": "object"}, execute=lookup,
        ))
        pi.registerTool(ToolDefinition(
            name="script", label="script", description="Runs lookup.", parameters={"type": "object", "properties": {}},
            execute=script,
        ))
        pi.on("tool_result", lambda event, _ctx: {"content": [TextContent(text="[redacted]")]}
              if event["toolName"] == "lookup" else None)

    async with harness(tmp_path, [factory]) as session:
        await session.bindExtensions({})
        session.agent.streamFunction = _replies([
            lambda model: AssistantMessage(
                content=[{"type": "toolCall", "id": "call-1", "name": "script", "arguments": {}}],
                api=model.api, provider=model.provider, model=model.id, usage=USAGE, stopReason="toolUse", timestamp=1,
            ),
            _done,
        ])
        await session.prompt("go")
        await session.waitForIdle()
        [outcome] = outcomes
        assert [block.text for block in outcome.result.content] == ["[redacted]"]
        # Redacted content must not leave the raw structured result readable by scripts.
        assert outcome.result.structuredContent is None


async def test_parts_that_replace_content_drop_structured_content_too():
    from misaka.core.moments import Moments

    class Redact:
        def tool_result(self, event, _ctx):
            return {"content": [{"type": "text", "text": "[redacted]"}]}

    class _Runner:
        def createContext(self):
            return None

    class _Session:
        extensionRunner = _Runner()

    moments = Moments(_Session(), [Redact()])
    folded = await moments.tool_result({
        "type": "tool_result", "content": [], "structuredContent": {"value": "secret"}, "details": {}, "isError": False,
    })
    assert folded["structuredContent"] is None


def _replies(replies: list):
    def stream(model, context, options=None):
        message = replies.pop(0)(model)
        output = AssistantMessageEventStream()
        output.push(DoneEvent(reason=message.stopReason, message=message))
        output.end()
        return output

    return stream


# --- tool-execution.ts: calls without a renderer show their arguments ------------------------


class _Theme:
    def fg(self, _color, text):
        return text

    def bold(self, text):
        return text


def test_a_call_without_a_renderer_shows_its_arguments():
    theme = _Theme()
    args = {"query": "hello", "limit": 3, "filter": {"a": [1, 2]}}
    assert formatToolCallWithArgs("search", args, theme, False) == (
        'search query="hello" limit=3 filter={"a":[1,2]}'
    )
    assert formatToolCallWithArgs("search", {"query": "one\ntwo"}, theme, True) == "search\n  query: one\n    two"
    long = formatToolCallWithArgs("search", {"text": "y" * 200}, theme, False)
    assert long.endswith("...") and len(long) == len("search ") + 100
    assert formatToolCallWithArgs("search", None, theme, False) == "search"
    assert formatToolCallWithArgs("search", {}, theme, False) == "search"
    assert formatToolCallWithArgs("search", [1, 2], theme, False) == "search args=[1,2]"


# --- MISAKA regressions (B5 review) ---------------------------------------------------------


async def test_nested_calls_with_non_object_arguments_keep_the_run_valid(tmp_path):
    from misaka.agent.types import AgentToolResult
    from misaka.core.extensions.types import ToolDefinition

    async def wrapper(_id, _params, _signal, _on_update, ctx):
        outcome = await ctx.executeTool("echo", "notes.txt")
        return AgentToolResult(content=[TextContent(text=f"nested error: {outcome.isError}")], details={})

    async def echo(_id, params, *_args):
        return AgentToolResult(content=[TextContent(text=str(params))], details={})

    def factory(pi) -> None:
        pi.registerTool(ToolDefinition(
            name="echo", label="echo", description="Echo.",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            execute=echo,
        ))
        pi.registerTool(ToolDefinition(
            name="wrapper", label="wrapper", description="Calls echo.", parameters={"type": "object", "properties": {}},
            execute=wrapper,
        ))

    async with harness(tmp_path, [factory]) as session:
        await session.bindExtensions({})
        session.agent.streamFunction = _replies([
            lambda model: AssistantMessage(
                content=[{"type": "toolCall", "id": "call-1", "name": "wrapper", "arguments": {}}],
                api=model.api, provider=model.provider, model=model.id, usage=USAGE, stopReason="toolUse", timestamp=1,
            ),
            _done,
        ])
        await session.prompt("go")
        await session.waitForIdle()
        result = next(message for message in session.messages if getattr(message, "role", None) == "toolResult")
        assert [block.text for block in result.content] == ["nested error: True"]
        assert result.nestedCalls.calls[0].arguments == "notes.txt"


async def test_nested_usage_reported_as_a_mapping_is_summed():
    from misaka.core.nested_tool_calls import NestedCallRecorder

    usage = {"input": 5, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 6,
             "cost": {"input": 0.1, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0.1}}
    recorder = NestedCallRecorder()
    recorder.addUsage(usage)
    recorder.addUsage(dict(usage))
    assert (recorder.totalUsage.input, recorder.totalUsage.cost.total) == (10, pytest.approx(0.2))


def test_a_lone_surrogate_argument_is_recorded_without_raising():
    from misaka.ai.types import ToolCall
    from misaka.core.nested_tool_calls import NestedCallRecorder

    record = NestedCallRecorder().start(ToolCall(id="c/1", name="t", arguments={"text": "\ud800"}))
    assert record is not None


async def test_only_the_sessions_own_tool_calls_may_nest(tmp_path):
    # MISAKA fork: a tool run by another Agent built from the session's tools must not reach the
    # session's callable tools through its hooks.
    async with harness(tmp_path, []) as session:
        await session.bindExtensions({})
        session.agent.streamFunction = _replies([_done])
        await session.prompt("hello")
        await session.waitForIdle()
        outcome = await session._executeNestedToolCall("foreign-call", "read", {"path": "x"})
        assert outcome.isError
        assert outcome.result.content[0].text == "Nested tool calls are not available in this context"


def test_mcp_names_and_types_are_checked_as_upstream_checks_them():
    from misaka.core.mcp_servers import getMcpToolExposure, validateMcpServerConfig

    assert validateMcpServerConfig("docs\n", {"url": "https://e.example"}).startswith("invalid server name")
    assert validateMcpServerConfig("docs", {"type": None, "url": "https://e.example"}) == (
        'server "docs" needs either "command" (stdio) or "url" (streamable HTTP)'
    )
    assert getMcpToolExposure({"toolExposure": {"get_*": "direct"}}, "get_a\n") == "codemode"


async def test_restoring_the_transcript_loadout_sets_the_selection_outside_a_scope(tmp_path):
    # Upstream: the transcript's loadout is the branch's selection, so a later registry refresh keeps it.
    # MISAKA fork: inside a tool scope it only sets the loadout, keeping the user's selection.
    from misaka.ai.types import SystemMessage

    async with harness(tmp_path, []) as session:
        await session.bindExtensions({})
        session.setActiveToolsByName(["read", "bash"])
        read_tool = next(tool for tool in session.agent.state.tools if tool.name == "read")
        narrowed = SystemMessage(content="", toolsAdded=[read_tool], timestamp=1)
        session.sessionManager.buildSessionContext = lambda: type("Context", (), {"messages": [narrowed]})()

        with session.toolScope(["read", "bash"]):
            session._restoreToolsFromTranscript()
            assert session.getActiveToolNames() == ["read"]
            assert session._unscopedToolNames == ["read", "bash"]

        session._restoreToolsFromTranscript()
        session.refreshTools()
        assert session.getActiveToolNames() == ["read"]
        assert session._unscopedToolNames == ["read"]
