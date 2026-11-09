"""pi coding-agent's ``suite/agent-session-tool-orchestration.test.ts`` (all three cases), plus the MISAKA fork: a
nested call answers to the session's tool scope and to the parts' tool boundaries."""

from __future__ import annotations

from contextlib import asynccontextmanager

from misaka.agent.types import AgentToolResult
from misaka.ai.types import (
    AssistantMessage,
    DoneEvent,
    NestedToolCalls,
    TextContent,
    ToolCall,
)
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.transcript import getCurrentTools
from misaka.core.extensions.types import ToolDefinition
from misaka.utils.values import read_field

EMPTY = {"type": "object", "properties": {}}
USAGE = {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 2,
         "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}


def _text(text: str) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text=text)], details={})


def orchestrator_extension(pi) -> None:
    """A tool that calls other tools, built only on the extension API: its own name, exposure,
    loadout hook, and ctx.executeTool(). Codemode and tool search use the same mechanisms."""

    async def echo(_id, params, _signal, _on_update, _ctx):
        return _text(f"echo: {params['text']}")

    async def helper(*_args):
        return _text("helped")

    async def run_tools(_id, _params, _signal, _on_update, ctx):
        helper_outcome = await ctx.executeTool("helper", {})
        echo_outcome = await ctx.executeTool("echo", {"text": "hi"})
        self_outcome = await ctx.executeTool("run_tools", {})
        text = " | ".join(outcome.result.content[0].text for outcome in (helper_outcome, echo_outcome, self_outcome))
        return _text(text)

    def prepare_loadout(loadout):
        return {
            "descriptions": {
                "run_tools": f"Runs tools: {', '.join(tool.name for tool in loadout.callable)}",
                "echo": "Echo text (also callable from run_tools).",
            },
            "hiddenDeclarations": ["echo"],
        }

    pi.registerTool(ToolDefinition(
        name="echo", label="echo", description="Echo text.",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        execute=echo,
    ))
    pi.registerTool(ToolDefinition(
        name="helper", label="helper", description="Only reachable from other tools.", parameters=EMPTY,
        exposure="codemode", execute=helper,
    ))
    pi.registerTool(ToolDefinition(
        name="run_tools", label="run_tools", description="Runs tools.", parameters=EMPTY,
        exposure="model-only", prepareLoadout=prepare_loadout, execute=run_tools,
    ))


def _scripted(responses: list):
    def stream(model, context, options=None):
        response = responses.pop(0)
        message = response(context) if callable(response) else response
        output = AssistantMessageEventStream()
        output.push(DoneEvent(reason=message.stopReason, message=message.model_copy(
            update={"api": model.api, "provider": model.provider, "model": model.id})))
        output.end()
        return output

    return stream


def _assistant(content, stop_reason="stop") -> AssistantMessage:
    return AssistantMessage(content=content, api="x", provider="x", model="x", usage=USAGE,
                            stopReason=stop_reason, timestamp=1)


@asynccontextmanager
async def harness(tmp_path, extension_factories):
    from misaka.ai.compat import getModels
    from misaka.core.auth_storage import AuthStorage
    from misaka.core.model_runtime import ModelRuntime
    from misaka.core.resource_loader import DefaultResourceLoader
    from misaka.core.sdk import createAgentSession
    from misaka.core.session_manager import SessionManager
    from misaka.core.settings_manager import SettingsManager

    workspace = tmp_path / "workspace"
    agent_dir = tmp_path / "agent"
    workspace.mkdir()
    agent_dir.mkdir()
    settings = SettingsManager.inMemory({"compaction": {"enabled": False}, "retry": {"enabled": False}})
    options = {"cwd": str(workspace), "agentDir": str(agent_dir)}
    loader = DefaultResourceLoader({**options, "settingsManager": settings, "extensionFactories": extension_factories})
    await loader.reload()
    model = next(m for m in getModels("anthropic") if m.id == "claude-opus-5")
    runtime = await ModelRuntime.create({"authStorage": AuthStorage.inMemory(), "modelsPath": None})
    await runtime.setRuntimeApiKey(model.provider, "fixture")
    result = await createAgentSession({
        **options, "resourceLoader": loader, "model": model, "modelRuntime": runtime, "settingsManager": settings,
        "noTools": "builtin", "sessionManager": SessionManager.inMemory(str(workspace)),
    })
    session = result["session"]
    try:
        yield session
    finally:
        session.dispose()


async def test_supports_tools_that_call_other_tools_under_any_name_through_the_extension_api(tmp_path):
    tool_calls: list[str] = []

    def spy(pi) -> None:
        pi.on("tool_call", lambda event, _ctx: tool_calls.append(f"{event['toolName']}:{event.get('parentToolCallId', 'top')}"))

    async with harness(tmp_path, [orchestrator_extension, spy]) as session:
        await session.bindExtensions({})

        assert session.getActiveToolNames() == ["echo", "run_tools"]
        assert session.getCallableToolNames() == ["echo", "helper"]

        def find(name):
            return next(tool for tool in session.agent.state.tools if tool.name == name)

        assert find("run_tools").description == "Runs tools: echo, helper"
        assert find("echo").description == "Echo text (also callable from run_tools)."

        request_tools: list[list[str]] = []

        def first(context):
            request_tools.append([tool.name for tool in getCurrentTools(context.messages)])
            return _assistant([ToolCall(id="call-1", name="run_tools", arguments={})], "toolUse")

        session.agent.streamFunction = _scripted([first, _assistant([TextContent(text="done")])])
        await session.prompt("go")
        await session.waitForIdle()

        # echo stays active, but its declaration is left out of requests.
        assert request_tools[0] == ["run_tools"]
        result = next(message for message in session.messages if getattr(message, "role", None) == "toolResult")
        assert [block.text for block in result.content] == ["helped | echo: hi | Tool run_tools not found"]
        parent = result.toolCallId
        assert tool_calls == ["run_tools:top", f"helper:{parent}", f"echo:{parent}"]
        assert [(call.id, call.name, call.status) for call in result.nestedCalls.calls] == [
            (f"{parent}/1", "helper", "ok"),
            (f"{parent}/2", "echo", "ok"),
            (f"{parent}/3", "run_tools", "error"),
        ]
        # The record is persisted with the session.
        persisted = next(
            entry for entry in session.sessionManager.getBranch()
            if read_field(entry, "type") == "message" and read_field(read_field(entry, "message"), "role") == "toolResult"
        )
        stored = read_field(read_field(entry := persisted, "message"), "nestedCalls")
        assert NestedToolCalls.model_validate(stored) == result.nestedCalls, entry


async def test_registers_codemode_and_tool_search_inactive_until_they_are_named():
    from misaka.extensions.codemode import createCodemodeExtension
    from misaka.extensions.tool_search import createToolSearchExtension
    from tests.suite_harness import createHarness

    extensionFactories = [createCodemodeExtension(), createToolSearchExtension()]
    async with createHarness(extensionFactories=extensionFactories) as plain:
        assert {"codemode", "tool_search"} <= {tool.name for tool in plain.session.getAllTools()}
        # MISAKA fork: MISAKA's eight default tools.
        assert plain.session.getActiveToolNames() == ["read", "bash", "edit", "write", "office", "grep", "find", "ls"]

    # --tools and the defaultTools setting name them explicitly.
    async with createHarness(allowedToolNames=["read", "codemode"], extensionFactories=extensionFactories) as allowed:
        assert allowed.session.getActiveToolNames() == ["read", "codemode"]
    async with createHarness(initialActiveToolNames=["tool_search"], extensionFactories=extensionFactories) as initial:
        assert initial.session.getActiveToolNames() == ["tool_search"]


async def test_leaves_results_without_nested_calls_unchanged(tmp_path):
    async with harness(tmp_path, [orchestrator_extension]) as session:
        await session.bindExtensions({})
        session.agent.streamFunction = _scripted([
            _assistant([ToolCall(id="call-1", name="echo", arguments={"text": "x"})], "toolUse"),
            _assistant([TextContent(text="done")]),
        ])
        await session.prompt("go")
        await session.waitForIdle()
        result = next(message for message in session.messages if getattr(message, "role", None) == "toolResult")
        assert result.nestedCalls is None


def _probe_extension(target: str, outcomes: list):
    """A tool that calls `target` through ctx.executeTool during a real run and keeps the outcome."""

    def factory(pi) -> None:
        async def probe(_id, _params, _signal, _on_update, ctx):
            outcomes.append(await ctx.executeTool(target, {}))
            return _text("probed")

        pi.registerTool(ToolDefinition(
            name="probe", label="probe", description="Probes.", parameters=EMPTY, execute=probe,
        ))

    return factory


def _probe_run() -> list:
    return [_assistant([ToolCall(id="call-1", name="probe", arguments={})], "toolUse"), _assistant([TextContent(text="done")])]


async def test_a_nested_call_answers_to_the_tool_scope(tmp_path):
    # MISAKA fork: codemode/deferred exposure must not reach past a research window's tool scope.
    outcomes: list = []
    async with harness(tmp_path, [orchestrator_extension, _probe_extension("helper", outcomes)]) as session:
        await session.bindExtensions({})
        session.agent.streamFunction = _scripted(_probe_run())
        with session.toolScope(["echo", "run_tools", "probe"]):
            assert session.getCallableToolNames() == ["echo", "probe"]
            await session.prompt("go")
            await session.waitForIdle()
        # A scope never widens the callable set, and a call outside it is blocked, not run.
        [outcome] = outcomes
        assert outcome.isError
        assert outcome.result.content[0].text == "Tool helper not found"
        assert session.getCallableToolNames() == ["echo", "helper", "probe"]


async def test_a_nested_call_answers_to_the_parts_tool_boundaries(tmp_path):
    # MISAKA fork: the parts' projections (for example driver-owned tools in a research card)
    # narrow what ctx.executeTool can reach.
    from misaka.core.moments import Moments

    class HideHelper:
        def project_tools(self, tools):
            return [tool for tool in tools if tool.name != "helper"]

    outcomes: list = []
    async with harness(tmp_path, [orchestrator_extension, _probe_extension("helper", outcomes)]) as session:
        await session.bindExtensions({})
        session.moments = Moments(session, [HideHelper()])
        session.setActiveToolsByName(session.getActiveToolNames())
        assert session.getCallableToolNames() == ["echo", "probe"]
        session.agent.streamFunction = _scripted(_probe_run())
        await session.prompt("go")
        await session.waitForIdle()
        [outcome] = outcomes
        assert outcome.isError
        assert outcome.result.content[0].text == "Tool helper not found"
