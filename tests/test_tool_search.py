"""pi coding-agent's ``tool-search.test.ts`` (the codemode description cases come with B9) and the
``tool_search`` case of ``suite/agent-session-tool-orchestration.test.ts``, plus a run that loads a
deferred tool and declares it for the next call."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from misaka.agent.types import AgentToolResult
from misaka.ai.types import AssistantMessage, DoneEvent, TextContent, ToolCall
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.ai.utils.transcript import getCurrentTools
from misaka.core.extensions.types import ToolDefinition
from misaka.extensions.tool_search import createToolSearchExtension
from misaka.extensions.tool_search.tool import (
    TOOL_SEARCH_DESCRIPTION,
    Bm25Ranker,
    ToolSearchMatch,
    _firstLine,
    createToolSearchDocument,
    tokenize,
)


def tool(name: str, description: str, properties: dict | None = None):
    return SimpleNamespace(
        name=name, label=name, description=description,
        parameters={"type": "object", "properties": properties or {}},
    )


# -- tool-search.test.ts ---------------------------------------------------------------------


def test_tokenize_splits_camel_case_and_snake_case_drops_stop_words_and_folds_plurals():
    assert tokenize("listIssues for the GitHub_repo") == ["list", "issue", "git", "hub", "repo"]
    assert tokenize("searches queries HTTPServer") == ["search", "query", "http", "server"]


TOOLS = [
    tool("mcp__github__list_issues", "List issues in a repository.", {
        "state": {"type": "string", "description": "open or closed"},
    }),
    tool("mcp__github__create_pull_request", "Open a pull request."),
    tool("mcp__linear__search_issues", "Search Linear issues by text."),
    tool("mcp__docs__search", "Search the documentation."),
]
DOCUMENTS = [createToolSearchDocument(entry) for entry in TOOLS]


def test_bm25_ranks_by_term_relevance_and_respects_the_limit():
    ranker = Bm25Ranker()
    assert [match.name for match in ranker.rank("issue", DOCUMENTS, 8)] == [
        "mcp__linear__search_issues",
        "mcp__github__list_issues",
    ]
    assert ranker.rank("pull requests", DOCUMENTS, 8)[0].name == "mcp__github__create_pull_request"
    assert len(ranker.rank("search", DOCUMENTS, 1)) == 1
    # Property names and descriptions are searchable.
    assert [match.name for match in ranker.rank("closed", DOCUMENTS, 8)] == ["mcp__github__list_issues"]


def test_bm25_returns_nothing_for_unknown_or_empty_queries():
    ranker = Bm25Ranker()
    assert ranker.rank("kubernetes", DOCUMENTS, 8) == []
    assert ranker.rank("the", DOCUMENTS, 8) == []
    # Known v1 limit: no synonyms, so "tickets" does not find "issues".
    assert ranker.rank("tickets", DOCUMENTS, 8) == []


def test_bm25_includes_the_namespace_in_the_search_text():
    document = createToolSearchDocument(tool("mcp__x__run", "Run it."), {
        "name": "mcp__x",
        "description": "Kubernetes cluster tools",
        "instructions": "Prefer helm releases over raw manifests.",
    })
    [match] = Bm25Ranker().rank("kubernetes", [document], 8)
    assert isinstance(match, ToolSearchMatch) and match.name == "mcp__x__run" and isinstance(match.score, float)
    # The namespace instructions are searchable too (pi 0.99.2).
    assert [match.name for match in Bm25Ranker().rank("helm", [document], 8)] == ["mcp__x__run"]


# -- sessions --------------------------------------------------------------------------------


def _deferred_tools(pi) -> None:
    async def weather(*_args):
        return AgentToolResult(content=[TextContent(text="sunny")], details={})

    pi.registerTool(ToolDefinition(
        name="weather_lookup", label="weather_lookup", description="Look up the weather forecast for a city.",
        parameters={"type": "object", "properties": {"city": {"type": "string"}}},
        exposure="deferred", namespace={"name": "mcp__weather", "description": "Weather service\nsecond line"},
        execute=weather,
    ))
    pi.registerTool(ToolDefinition(
        name="stock_quote", label="stock_quote", description="Get a stock price.",
        parameters={"type": "object", "properties": {}}, exposure="codemode", execute=weather,
    ))


@asynccontextmanager
async def session_with(tmp_path, extension_factories, **options):
    from misaka.ai.compat import getModels
    from misaka.core.auth_storage import AuthStorage
    from misaka.core.model_runtime import ModelRuntime
    from misaka.core.resource_loader import DefaultResourceLoader
    from misaka.core.sdk import createAgentSession
    from misaka.core.session_manager import SessionManager
    from misaka.core.settings_manager import SettingsManager

    workspace, agent_dir = tmp_path / "workspace", tmp_path / "agent"
    workspace.mkdir(exist_ok=True)
    agent_dir.mkdir(exist_ok=True)
    settings = SettingsManager.inMemory({
        "compaction": {"enabled": False}, "retry": {"enabled": False}, **options.pop("settings", {}),
    })
    base = {"cwd": str(workspace), "agentDir": str(agent_dir)}
    loader = DefaultResourceLoader({**base, "settingsManager": settings, "extensionFactories": extension_factories})
    await loader.reload()
    model = next(m for m in getModels("anthropic") if m.id == "claude-opus-5")
    runtime = await ModelRuntime.create({"authStorage": AuthStorage.inMemory(), "modelsPath": None})
    await runtime.setRuntimeApiKey(model.provider, "fixture")
    result = await createAgentSession({
        **base, "resourceLoader": loader, "model": model, "modelRuntime": runtime, "settingsManager": settings,
        "sessionManager": SessionManager.inMemory(str(workspace)), **options,
    })
    session = result["session"]
    try:
        yield session
    finally:
        session.dispose()


async def test_registers_tool_search_inactive_until_it_is_named(tmp_path):
    from misaka.core.settings_manager import DEFAULT_TOOL_NAMES

    async with session_with(tmp_path, [createToolSearchExtension()]) as plain:
        assert "tool_search" in [info.name for info in plain.getAllTools()]
        assert plain.getActiveToolNames() == list(DEFAULT_TOOL_NAMES)

    # --tools and the defaultTools setting name it explicitly.
    async with session_with(tmp_path, [createToolSearchExtension()], tools=["read", "tool_search"]) as named:
        assert named.getActiveToolNames() == ["read", "tool_search"]
    async with session_with(
        tmp_path, [createToolSearchExtension()], settings={"defaultTools": ["+tool_search"]}
    ) as configured:
        assert "tool_search" in configured.getActiveToolNames()


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


USAGE = {"input": 1, "output": 1, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 2,
         "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}}


def _assistant(content, stop_reason="stop") -> AssistantMessage:
    return AssistantMessage(content=content, api="x", provider="x", model="x", usage=USAGE,
                            stopReason=stop_reason, timestamp=1)


async def test_tool_search_loads_a_deferred_tool_for_the_next_call(tmp_path):
    declared: list[list[str]] = []
    descriptions: list[str] = []

    def record(context):
        tools = getCurrentTools(context.messages)
        declared.append(sorted(tool.name for tool in tools))
        descriptions.extend(tool.description for tool in tools if tool.name == "tool_search")

    def first(context):
        record(context)
        return _assistant([ToolCall(id="call-1", name="tool_search", arguments={"query": "weather forecast"})], "toolUse")

    def second(context):
        record(context)
        return _assistant([ToolCall(id="call-2", name="weather_lookup", arguments={"city": "Tokyo"})], "toolUse")

    def third(context):
        record(context)
        return _assistant([TextContent(text="done")])

    # `defaultTools` picks the initial loadout; `--tools` would also keep the deferred tools out of the registry.
    async with session_with(
        tmp_path, [createToolSearchExtension(), _deferred_tools], settings={"defaultTools": ["read", "tool_search"]}
    ) as session:
        session.agent.streamFunction = _scripted([first, second, third])
        await session.prompt("what is the weather")
        await session.waitForIdle()

        # The description is a constant: it does not list the searchable sources (pi 0.99.2).
        assert descriptions[0] == TOOL_SEARCH_DESCRIPTION
        assert declared[0] == ["read", "tool_search"]
        # The match is declared from the next call on, and the call to it runs.
        assert declared[1] == ["read", "tool_search", "weather_lookup"]
        results = [message for message in session.messages if getattr(message, "role", None) == "toolResult"]
        assert results[0].details == {"loaded": ["weather_lookup"]}
        assert "Loaded 1 tool. They are available from your next call:\n- weather_lookup: Look up" in (
            results[0].content[0].text
        )
        assert results[1].content[0].text == "sunny"
        assert "weather_lookup" in session.getActiveToolNames()


async def test_tool_search_rejects_empty_queries_and_bad_limits(tmp_path):
    from misaka.extensions.tool_search.tool import (
        ToolSearchToolOptions,
        createToolSearchToolDefinition,
    )

    definition = createToolSearchToolDefinition(ToolSearchToolOptions())
    with pytest.raises(RuntimeError, match="query must not be empty"):
        await definition.execute("call", {"query": "  "})
    for limit in (0, -1, 1.5, True):
        with pytest.raises(RuntimeError, match="positive integer"):
            await definition.execute("call", {"query": "x", "limit": limit})
    # Without the session's tools it finds nothing.
    result = await definition.execute("call", {"query": "x", "limit": 2.0})
    assert result.content[0].text == "No matching tools found."
    assert result.details == {"loaded": []}


# -- B7 review regressions -------------------------------------------------------------------


async def test_trimming_follows_javascript_whitespace():
    from misaka.extensions.tool_search.tool import (
        ToolSearchToolOptions,
        createToolSearchToolDefinition,
    )

    definition = createToolSearchToolDefinition(ToolSearchToolOptions())
    with pytest.raises(RuntimeError, match="query must not be empty"):
        await definition.execute("call", {"query": "\ufeff"})
    for query in ("\x1c", "\x85"):
        result = await definition.execute("call", {"query": query})
        assert result.content[0].text == "No matching tools found."
    # The execute listing's first line is cut after a JS trim: U+0085 stays, the BOM goes.
    assert _firstLine("\x85x\nmore") == "\x85x"
    assert _firstLine("\ufeffx\ufeff") == "x"


def test_a_pydantic_parameter_model_is_indexed():
    from pydantic import BaseModel, Field

    class QuoteArgs(BaseModel):
        ticker: str = Field(description="Exchange ticker symbol")

    document = createToolSearchDocument(SimpleNamespace(name="quote", description="Get a quote.", parameters=QuoteArgs))
    assert [match.name for match in Bm25Ranker().rank("ticker exchange", [document], 8)] == ["quote"]


def _search_call(query: str):
    def respond(_context):
        return _assistant([ToolCall(id="call-search", name="tool_search", arguments={"query": query})], "toolUse")

    return respond


async def test_inside_a_tool_scope_tool_search_only_finds_what_the_run_can_reach(tmp_path):
    async with session_with(
        tmp_path, [createToolSearchExtension(), _deferred_tools],
        settings={"defaultTools": ["read", "bash", "tool_search"]},
    ) as session:
        session.agent.streamFunction = _scripted([_search_call("weather forecast"), _assistant([TextContent(text="ok")])])
        with session.toolScope(["read", "tool_search"]):
            await session.prompt("find the weather tool")
            await session.waitForIdle()
        result = next(message for message in session.messages if getattr(message, "role", None) == "toolResult")
        assert result.content[0].text == "No matching tools found."
        # The selection the scope hid survives the run inside it.
        assert sorted(session.getActiveToolNames()) == ["bash", "read", "tool_search"]


async def test_a_caller_inside_a_scope_keeps_the_tools_the_scope_hides(tmp_path):
    async with session_with(tmp_path, [createToolSearchExtension(), _deferred_tools],
                            settings={"defaultTools": ["read", "bash", "tool_search"]}) as session:
        with session.toolScope(["read", "tool_search", "weather_lookup"]):
            session.setActiveToolsByName([*session.getActiveToolNames(), "weather_lookup"])
            assert session.getActiveToolNames() == ["read", "tool_search", "weather_lookup"]
        # The scope granted weather_lookup; it ends with the scope.
        assert sorted(session.getActiveToolNames()) == ["bash", "read", "tool_search"]


async def test_a_tool_a_caller_adds_inside_a_scope_outlives_it(tmp_path):
    async with session_with(tmp_path, [createToolSearchExtension(), _deferred_tools],
                            settings={"defaultTools": ["read", "bash", "tool_search"]}) as session:
        with session.toolScope(["read", "tool_search"], callable=["weather_lookup"]):
            session.setActiveToolsByName([*session.getActiveToolNames(), "weather_lookup"])
        assert sorted(session.getActiveToolNames()) == ["bash", "read", "tool_search", "weather_lookup"]


async def test_tools_a_scope_grants_do_not_outlive_it(tmp_path):
    async with session_with(tmp_path, [createToolSearchExtension(), _deferred_tools],
                            settings={"defaultTools": ["read", "bash"]}) as session:
        with session.toolScope(["read", "tool_search"], callable=["weather_lookup"]):
            # tool_search loading a tool sets the loadout to what is visible plus the tool.
            session.setActiveToolsByName([*session.getActiveToolNames(), "weather_lookup"])
            assert session.getActiveToolNames() == ["read", "tool_search", "weather_lookup"]
        assert sorted(session.getActiveToolNames()) == ["bash", "read", "weather_lookup"]


async def test_a_loaded_tool_stays_on_its_branch_across_a_refresh(tmp_path):
    async with session_with(
        tmp_path, [createToolSearchExtension(), _deferred_tools], settings={"defaultTools": ["read", "tool_search"]}
    ) as session:
        session.agent.streamFunction = _scripted([
            _assistant([TextContent(text="first")]),
            _search_call("weather forecast"),
            _assistant([TextContent(text="loaded")]),
        ])
        await session.prompt("hello")
        first_answer = session.sessionManager.getLeafId()
        await session.prompt("load the weather tool")
        await session.waitForIdle()
        assert "weather_lookup" in session.getActiveToolNames()

        await session.navigateTree(first_answer)
        session.refreshTools()
        assert "weather_lookup" not in session.getActiveToolNames()
