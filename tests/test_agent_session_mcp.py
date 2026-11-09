"""pi coding-agent's ``test/suite/agent-session-mcp.test.ts``."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import pytest

from misaka.agent.types import AgentToolResult
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.core.extensions.types import ToolDefinition
from misaka.extensions.codemode import createCodemodeExtension
from misaka.extensions.mcp import MCP_SERVERS_SECTION, createMcpExtension
from misaka.extensions.mcp.tools import createMcpToolName
from misaka.extensions.tool_search import createToolSearchExtension
from misaka.extensions.tool_search.tool import TOOL_SEARCH_DESCRIPTION
from misaka.mcp.protocol.types import LATEST_PROTOCOL_VERSION
from misaka.mcp.testing import createInMemoryTransportPair
from misaka.utils.js import jsonStringify, spawn
from misaka.utils.values import read_field
from tests.suite_harness import (
    Harness,
    createHarness,
    createTestExtensionsResult,
    createTestResourceLoader,
    createTestUiContext,
    getAssistantTexts,
    getMessageText,
)
from tests.suite_harness import getToolResult as toolResult

TINY_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="

SERVER_TOOLS = [
    {
        "name": "search",
        "description": "Search the docs.",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        "outputSchema": {
            "type": "object",
            "properties": {"hits": {"type": "array", "items": {"type": "string"}}},
            "required": ["hits"],
        },
    },
    {
        "name": "fail",
        "description": "Always fails.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"title": "Fail", "destructiveHint": True, "readOnlyHint": False, "idempotentHint": "yes"},
    },
    {"name": "shot", "description": "Returns an image.", "inputSchema": {"type": "object", "properties": {}}},
]

# MISAKA fork: asyncio holds tasks weakly; the fake servers' sends live here.
_tasks: set[asyncio.Task[Any]] = set()


def createFakeServer(
    calls: list[str],
    listTools=None,
    resources: bool = False,
    instructions: str | None = None,
    initializeDelayMs: float = 0,
):
    """Minimal MCP server over an in-memory transport. Records the tool calls it receives.
    ``initializeDelayMs`` delays the answer to ``initialize``; ``math.inf`` never answers."""
    listTools = listTools if listTools is not None else (lambda: SERVER_TOOLS)
    pair = createInMemoryTransportPair()

    def respond(request: dict[str, Any]) -> Any:
        method = request["method"]
        if method == "initialize":
            return {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {"tools": {}, **({"resources": {}} if resources else {})},
                "serverInfo": {"name": "docs", "version": "1.0.0"},
                **({"instructions": instructions} if instructions else {}),
            }
        if method == "tools/list":
            return {"tools": listTools()}
        if method == "resources/list":
            return {
                "resources": [
                    {"uri": "docs://intro", "name": "intro", "mimeType": "text/markdown", "_meta": {"x": 1}},
                    # MCP App user interfaces are left out.
                    {"uri": "ui://docs/viewer", "name": "viewer", "mimeType": "text/html;profile=mcp-app"},
                ],
            }
        if method == "resources/templates/list":
            return {
                "resourceTemplates": [
                    {"uriTemplate": "docs://pages/{slug}", "name": "page", "icons": [{"src": "data:image/png;base64,AAAA"}]},
                ],
            }
        if method == "resources/read":
            uri = request["params"]["uri"]
            calls.append(f"read:{uri}")
            return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": f"# {uri}"}]}
        if method == "tools/call":
            params = request["params"]
            arguments = params.get("arguments") or {}
            calls.append(f"{params['name']}:{jsonStringify(arguments)}")
            if params["name"] == "search":
                hits = [f"{arguments.get('query')} guide", f"{arguments.get('query')} faq"]
                return {"content": [{"type": "text", "text": "\n".join(hits)}], "structuredContent": {"hits": hits}}
            if params["name"] == "shot":
                return {"content": [{"type": "image", "data": TINY_PNG_BASE64, "mimeType": "image/png"}]}
            return {"content": [{"type": "text", "text": "server exploded"}], "isError": True}
        return {}

    def onMessage(message: dict[str, Any]) -> None:
        if "id" not in message or "method" not in message:
            return

        def send() -> None:
            spawn(pair["server"].send({"jsonrpc": "2.0", "id": message["id"], "result": respond(message)}), _tasks)

        loop = asyncio.get_running_loop()
        if message["method"] != "initialize" or initializeDelayMs == 0:
            loop.call_soon(send)
        elif math.isfinite(initializeDelayMs):
            loop.call_later(initializeDelayMs / 1000, send)

    pair["server"].onMessage(onMessage)
    return pair


async def waitFor(predicate, timeout: float = 1.0) -> None:
    """``vi.waitFor``: poll until ``predicate`` holds."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def prompt(harness: Harness, text: str) -> None:
    await harness.session.prompt(text)
    await harness.session.waitForIdle()


def toolNames(harness: Harness) -> list[str]:
    return [tool.name for tool in harness.session.getAllTools()]


def declaredToolNames(harness: Harness) -> list[str]:
    return [
        read_field(tool, "name")
        for message in harness.session.messages
        if read_field(message, "role") == "system"
        for tool in (read_field(message, "toolsAdded") or [])
    ]


def serversSection(harness: Harness) -> Any:
    """The ``mcp_servers`` prompt section as the model currently has it."""
    section: Any = "<undefined>"
    for message in harness.session.messages:
        sections = read_field(message, "sections")
        if read_field(message, "role") == "system" and sections and MCP_SERVERS_SECTION in sections:
            section = sections[MCP_SERVERS_SECTION]
    return None if section == "<undefined>" else section


def nestedToolNames(harness: Harness) -> list[str]:
    return harness.session.getCallableToolNames()


def toolDescription(harness: Harness, name: str) -> str:
    return next((tool.description for tool in harness.session.agent.state.tools if tool.name == name), "")


def lastLine(message: Any) -> str:
    return getMessageText(message).split("\n")[-1]


def firstEntry(harness: Harness, role: str) -> Any:
    entry = next(
        (
            entry
            for entry in harness.sessionManager.getBranch()
            if read_field(entry, "type") == "message" and read_field(read_field(entry, "message"), "role") == role
        ),
        None,
    )
    if entry is None:
        raise RuntimeError(f"No {role} entry")
    return entry


@pytest.fixture
async def stack():
    async with AsyncExitStack() as cleanups:
        yield cleanups


# -- AgentSession MCP integration -----------------------------------------------------------------


async def setup(
    stack: AsyncExitStack,
    exposure: str,
    listTools=None,
    *,
    builtInTools: list[str] | None = None,
    allowedToolNames: list[str] | None = None,
    excludedToolNames: list[str] | None = None,
    sessionManager: Any = None,
    waitForTools: bool = True,
    extensionFactories: list[Any] | None = None,
    toolExposure: dict[str, str] | None = None,
    resources: bool = False,
    withoutToolSearch: bool = False,
    description: str | None = None,
    instructions: str | None = None,
    **configOptions: Any,
):
    calls: list[str] = []
    notifications: list[str] = []
    servers: list[Any] = []
    entry = {
        "name": "docs",
        "config": {
            "url": "http://unused.invalid",
            "exposure": exposure,
            **({"toolExposure": toolExposure} if toolExposure else {}),
            **({"description": description} if description else {}),
        },
        "source": "test",
    }

    def createTransport(*_args: Any) -> Any:
        pair = createFakeServer(calls, listTools, resources, instructions)
        servers.append(pair["server"])
        spawn(pair["server"].start(), _tasks)
        return pair["client"]

    # `builtInTools` are the built-in tools active at the start. The MCP extension activates codemode
    # or tool_search.
    harness = await stack.enter_async_context(createHarness(
        initialActiveToolNames=allowedToolNames if allowedToolNames is not None else (builtInTools or []),
        allowedToolNames=allowedToolNames,
        excludedToolNames=excludedToolNames,
        sessionManager=sessionManager,
        extensionFactories=[
            *(extensionFactories or []),
            createCodemodeExtension(),
            *([] if withoutToolSearch else [createToolSearchExtension()]),
            createMcpExtension({
                "loadConfig": lambda _ctx: {"servers": [entry], "errors": [], **configOptions},
                "createTransport": createTransport,
            }),
        ],
    ))
    await harness.session.bindExtensions({
        "uiContext": createTestUiContext(notify=lambda message, *_args: notifications.append(message)),
    })
    # The first prompt waits only for servers with direct tools; wait for the others here.
    if waitForTools:
        await waitFor(lambda: "mcp__docs__search" in toolNames(harness))
    return harness, calls, servers, notifications


async def test_keeps_mcp_tools_when_tools_names_no_mcp_tool(stack):
    for exposure in ("codemode", "deferred"):
        harness, *_rest = await setup(stack, exposure, allowedToolNames=["read", "codemode"], resources=True)
        assert harness.session.getActiveToolNames() == ["read", "codemode"]
        assert {"mcp__docs__search", "mcp__docs__fail", "list_mcp_resources", "read_mcp_resource"} <= set(
            nestedToolNames(harness)
        )
        assert "bash" not in toolNames(harness)

    # A direct MCP tool stays registered but is declared only when --tools names it.
    harness, *_rest = await setup(stack, "codemode", allowedToolNames=["codemode"], toolExposure={"fail": "direct"})
    await waitFor(lambda: "mcp__docs__fail" in toolNames(harness))
    assert harness.session.getActiveToolNames() == ["codemode"]


async def test_removes_mcp_tools_with_no_tools(stack):
    # The first prompt waits for servers with direct tools, so their tools would be registered by then.
    harness, _calls, servers, _notifications = await setup(stack, "direct", allowedToolNames=[], waitForTools=False)
    harness.setResponses([fauxAssistantMessage("done")])
    await prompt(harness, "go")

    assert len(servers) == 1
    assert harness.session.getAllTools() == []
    assert harness.session.getActiveToolNames() == []


async def test_does_not_declare_unnamed_mcp_tools_restored_from_the_transcript(stack):
    first, *_rest = await setup(stack, "direct")
    first.setResponses([fauxAssistantMessage("one"), fauxAssistantMessage("two")])
    await prompt(first, "first")
    await prompt(first, "second")
    assert "mcp__docs__search" in declaredToolNames(first)

    # Like `misaka --tools read,codemode -c` followed by /tree.
    second, *_rest = await setup(
        stack, "direct", allowedToolNames=["read", "codemode"], sessionManager=first.sessionManager
    )
    await second.session.navigateTree(read_field(firstEntry(second, "assistant"), "id"))

    # The transcript's loadout is restored without the MCP tools it declared.
    assert second.session.getActiveToolNames() == []
    assert "mcp__docs__search" in toolNames(second)


async def test_lets_tool_search_declare_unnamed_mcp_tools_when_tools_names_it(stack):
    harness, *_rest = await setup(stack, "deferred", allowedToolNames=["tool_search"])
    harness.setResponses([
        fauxAssistantMessage([fauxToolCall("tool_search", {"query": "search the docs", "limit": 1})], {"stopReason": "toolUse"}),
        fauxAssistantMessage("loaded"),
    ])
    await prompt(harness, "load")

    assert harness.session.getActiveToolNames() == ["tool_search", "mcp__docs__search"]


async def test_filters_mcp_tools_by_the_mcp_entries_of_tools(stack):
    harness, *_rest = await setup(
        stack,
        "codemode",
        allowedToolNames=["codemode", "mcp__docs__s*"],
        resources=True,
        toolExposure={"shot": "direct"},
    )
    await waitFor(lambda: harness.session.getActiveToolNames() == ["codemode", "mcp__docs__shot"])
    registered = toolNames(harness)
    assert {"mcp__docs__search", "mcp__docs__shot"} <= set(registered)
    assert "mcp__docs__fail" not in registered
    assert "list_mcp_resources" not in registered


async def test_removes_mcp_tools_matching_exclude_tools_patterns(stack):
    harness, *_rest = await setup(stack, "codemode", allowedToolNames=["codemode"], excludedToolNames=["mcp__docs__f*"])
    registered = toolNames(harness)
    assert "mcp__docs__search" in registered
    assert "mcp__docs__fail" not in registered


async def test_exposes_codemode_only_mcp_tools_through_codemode_and_hides_them_from_the_model(stack):
    harness, calls, _servers, _notifications = await setup(stack, "codemode")
    searchName = createMcpToolName("docs", "search")
    # MCP tools resolve to their CallToolResult, errors included.
    harness.setResponses([
        fauxAssistantMessage(
            [
                fauxToolCall("codemode", {
                    "code": f"""
                        const [a, b] = await Promise.allSettled([
                            tools.{searchName}({{ query: "mcp" }}),
                            tools.{searchName}({{ query: "pi" }}),
                        ]);
                        const failure = await tools.mcp__docs__fail({{}});
                        const shot = await tools.mcp__docs__shot({{}});
                        image(shot.content[0]);
                        text(JSON.stringify({{
                            hits: [...a.value.structuredContent.hits, ...b.value.structuredContent.hits],
                            failed: failure.isError,
                            failure: failure.content[0].text,
                            found: ALL_TOOLS.filter((tool) => tool.name.includes("search")).map((tool) => tool.name),
                        }}));
                    """,
                }),
            ],
            {"stopReason": "toolUse"},
        ),
        fauxAssistantMessage("done"),
    ])

    await prompt(harness, "search the docs")

    # Exec was activated for the codemode-exposed server; MCP tools are never declared.
    assert harness.session.getActiveToolNames() == ["codemode"]
    assert declaredToolNames(harness) == ["codemode"]
    assert nestedToolNames(harness) == [searchName, "mcp__docs__fail", "mcp__docs__shot"]
    # The description lists neither the server nor its tools; scripts search for them.
    assert "mcp__docs" not in toolDescription(harness, "codemode")
    assert "Shared MCP Types:" not in toolDescription(harness, "codemode")

    result = toolResult(harness, "codemode")
    assert result.isError is False
    # Output items keep the order the script produced them in; each image follows the path it was saved to.
    saved = re.match(r"^\[Image saved to (\S+\.png) \(image/png, \d+B\)\]\Z", result.content[1].text)
    assert saved is not None
    savedPath = saved.group(1)
    try:
        assert base64.b64encode(Path(savedPath).read_bytes()).decode() == TINY_PNG_BASE64
    finally:
        Path(savedPath).unlink(missing_ok=True)
    assert result.content[2].model_dump(exclude_none=True) == {"type": "image", "data": TINY_PNG_BASE64, "mimeType": "image/png"}
    assert json.loads(result.content[3].text) == {
        "hits": ["mcp guide", "mcp faq", "pi guide", "pi faq"],
        "failed": True,
        "failure": "server exploded",
        "found": [searchName],
    }
    assert len(result.content) == 4
    assert calls == ['search:{"query":"mcp"}', 'search:{"query":"pi"}', "fail:{}", "shot:{}"]


async def test_keeps_codemode_only_mcp_tools_callable_across_tree_navigation(stack):
    harness, *_rest = await setup(stack, "codemode")
    searchName = createMcpToolName("docs", "search")
    harness.setResponses([fauxAssistantMessage("one"), fauxAssistantMessage("two")])
    await prompt(harness, "first")
    await prompt(harness, "second")
    assert harness.session.getActiveToolNames() == ["codemode"]
    assert searchName in nestedToolNames(harness)

    await harness.session.navigateTree(read_field(firstEntry(harness, "assistant"), "id"))

    assert harness.session.getActiveToolNames() == ["codemode"]
    assert searchName in nestedToolNames(harness)


# Regression: #10239.
@pytest.mark.parametrize("reverse", [False, True])
async def test_routes_tools_whose_names_differ_only_in_dash_and_underscore(stack, reverse):
    tools = [
        {"name": "read-file", "description": "dashed", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "read_file", "description": "underscored", "inputSchema": {"type": "object", "properties": {}}},
    ]
    if reverse:
        tools.reverse()
    harness, calls, _servers, _notifications = await setup(stack, "codemode", lambda: [*tools, *SERVER_TOOLS])
    code = 'for (const query of ["dashed", "underscored"]) await tools[(await searchTools(query))[0].name]({});'
    harness.setResponses([
        fauxAssistantMessage([fauxToolCall("codemode", {"code": code})], {"stopReason": "toolUse"}),
        fauxAssistantMessage("done"),
    ])

    await prompt(harness, "go")

    assert calls == ["read-file:{}", "read_file:{}"]


async def test_rejects_direct_model_calls_to_codemode_only_mcp_tools(stack):
    harness, calls, _servers, _notifications = await setup(stack, "codemode")
    harness.setResponses([
        fauxAssistantMessage([fauxToolCall(createMcpToolName("docs", "search"), {"query": "x"})], {"stopReason": "toolUse"}),
        fauxAssistantMessage("done"),
    ])

    await prompt(harness, "go")

    result = toolResult(harness, "mcp__docs__search")
    assert result.isError is True
    assert getMessageText(result) == "Tool mcp__docs__search not found"
    assert calls == []


async def test_declares_directly_exposed_mcp_tools_to_the_model(stack):
    harness, *_rest = await setup(stack, "direct")
    harness.setResponses([
        fauxAssistantMessage([fauxToolCall("mcp__docs__search", {"query": "direct"})], {"stopReason": "toolUse"}),
        fauxAssistantMessage("done"),
    ])

    await prompt(harness, "go")

    assert declaredToolNames(harness) == ["mcp__docs__search", "mcp__docs__fail", "mcp__docs__shot"]
    assert "codemode" not in harness.session.getActiveToolNames()
    # Boolean annotation hints are passed on for permission extensions.
    annotations = {tool.name: tool.annotations for tool in harness.session.getAllTools()}
    assert annotations["mcp__docs__fail"] == {"destructiveHint": True, "readOnlyHint": False}
    assert annotations["mcp__docs__search"] is None
    assert getMessageText(toolResult(harness, "mcp__docs__search")) == "direct guide\ndirect faq"


@pytest.mark.parametrize("exposure", ["direct", "codemode"])
async def test_withdraws_and_restores_mcp_tools_the_server_changes(stack, exposure):
    tools = SERVER_TOOLS
    harness, _calls, servers, _notifications = await setup(stack, exposure, lambda: tools)
    harness.setResponses([fauxAssistantMessage("ready")])
    await prompt(harness, "start")

    # Direct tools are declared to the model, codemode tools are only callable from codemode.
    def reachable() -> list[str]:
        return harness.session.getActiveToolNames() if exposure == "direct" else nestedToolNames(harness)

    async def listChanged() -> None:
        await servers[0].send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        await asyncio.sleep(0.01)

    assert "mcp__docs__fail" in reachable()

    tools = [tool for tool in SERVER_TOOLS if tool["name"] != "fail"]
    await listChanged()
    assert "mcp__docs__fail" not in reachable()
    assert "mcp__docs__search" in reachable()
    assert "mcp__docs__fail" not in toolDescription(harness, "codemode")

    tools = SERVER_TOOLS
    await listChanged()
    assert "mcp__docs__fail" in reachable()


async def test_lists_and_reads_resources_with_codexs_resource_tools(stack):
    harness, calls, _servers, _notifications = await setup(stack, "direct", resources=True)
    harness.setResponses([
        fauxAssistantMessage(
            [
                fauxToolCall("list_mcp_resources", {}),
                fauxToolCall("list_mcp_resource_templates", {"server": "docs"}),
                fauxToolCall("read_mcp_resource", {"server": "docs", "uri": "docs://pages/setup"}),
            ],
            {"stopReason": "toolUse"},
        ),
        fauxAssistantMessage([fauxToolCall("read_mcp_resource", {"server": "nope", "uri": "docs://x"})], {"stopReason": "toolUse"}),
        fauxAssistantMessage("done"),
    ])

    await prompt(harness, "read")

    # The resource tools take the exposure of the servers they reach.
    assert {"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"} <= set(
        harness.session.getActiveToolNames()
    )
    assert json.loads(getMessageText(toolResult(harness, "list_mcp_resources"))) == {
        "resources": [{"server": "docs", "uri": "docs://intro", "name": "intro", "mimeType": "text/markdown"}],
    }
    assert json.loads(getMessageText(toolResult(harness, "list_mcp_resource_templates"))) == {
        "server": "docs",
        "resourceTemplates": [{"server": "docs", "uriTemplate": "docs://pages/{slug}", "name": "page"}],
    }
    results = [
        message
        for message in harness.session.messages
        if read_field(message, "role") == "toolResult" and message.toolName == "read_mcp_resource"
    ]
    assert getMessageText(results[0]) == "# docs://pages/setup"
    assert results[1].isError is True
    assert getMessageText(results[1]) == 'MCP server "nope" has no resources. Servers with resources: docs'
    assert calls == ["read:docs://pages/setup"]
    readResource = next(tool for tool in harness.session.getAllTools() if tool.name == "read_mcp_resource")
    assert readResource.annotations == {"readOnlyHint": True}


async def test_makes_the_resource_tools_callable_from_codemode_for_codemode_servers(stack):
    harness, calls, _servers, _notifications = await setup(stack, "codemode", resources=True)
    harness.setResponses([
        fauxAssistantMessage(
            [
                fauxToolCall("codemode", {
                    "code": """const listed = await tools.list_mcp_resources({ server: "docs" });
const read = await tools.read_mcp_resource({ server: "docs", uri: listed.resources[0].uri });
return { uris: listed.resources.map((r) => r.uri), text: read.contents[0].text };""",
                }),
            ],
            {"stopReason": "toolUse"},
        ),
        fauxAssistantMessage("done"),
    ])

    await prompt(harness, "read")

    assert harness.session.getActiveToolNames() == ["codemode"]
    assert json.loads(lastLine(toolResult(harness, "codemode"))) == {"uris": ["docs://intro"], "text": "# docs://intro"}
    assert calls == ["read:docs://intro"]


async def test_applies_per_tool_exposure_overrides(stack):
    harness, *_rest = await setup(stack, "hidden", toolExposure={"search": "direct", "s*": "codemode"})
    harness.setResponses([fauxAssistantMessage("ready")])
    await prompt(harness, "start")

    # `search` is declared, `shot` is only callable from codemode, `fail` keeps the server's `hidden`.
    assert declaredToolNames(harness) == ["codemode", "mcp__docs__search"]
    assert nestedToolNames(harness) == ["mcp__docs__search", "mcp__docs__shot"]
    assert "mcp__docs__shot" not in toolDescription(harness, "codemode")
    assert "mcp__docs__fail" not in toolDescription(harness, "codemode")


async def test_describes_the_server_with_its_configured_description_and_returns_its_instructions_to_scripts(stack):
    harness, *_rest = await setup(
        stack,
        "codemode",
        description="Search the product docs",
        instructions="Always search before reading.",
        builtInTools=["tool_search"],
    )
    harness.setResponses([
        fauxAssistantMessage(
            [
                fauxToolCall("codemode", {
                    "code": """const docs = await describeNamespace("mcp__docs");
const aliases = await Promise.all(["docs", "mcp__docs"].map((name) => describeNamespace(name)));
return { docs, sameForAliases: aliases.every((alias) => JSON.stringify(alias) === JSON.stringify(docs)), none: await describeNamespace("mcp__nope") };""",
                }),
            ],
            {"stopReason": "toolUse"},
        ),
        fauxAssistantMessage("done"),
    ])

    await prompt(harness, "go")

    # The instructions stay out of every tool description.
    assert "mcp__docs" not in toolDescription(harness, "codemode")
    assert toolDescription(harness, "tool_search") == TOOL_SEARCH_DESCRIPTION
    for name in ("codemode", "tool_search"):
        assert "Always search" not in toolDescription(harness, name)
    assert json.loads(lastLine(toolResult(harness, "codemode"))) == {
        "docs": {
            "name": "mcp__docs",
            "description": "Search the product docs",
            "instructions": "Always search before reading.",
            "tools": ["mcp__docs__search", "mcp__docs__fail", "mcp__docs__shot"],
        },
        "sameForAliases": True,
    }
    # The system prompt lists the server with its configured description rather than its instructions.
    assert "- mcp__docs (codemode): Search the product docs" in serversSection(harness)


async def test_lists_servers_by_the_first_line_of_their_instructions_without_a_configured_description(stack):
    harness, *_rest = await setup(stack, "deferred", instructions="Docs search.\nLong guidance.")
    harness.setResponses([fauxAssistantMessage("done")])

    await prompt(harness, "go")

    section = serversSection(harness)
    assert "- mcp__docs (tool_search): Docs search.\n" in section
    assert "Long guidance" not in section


async def test_does_not_activate_codemode_when_auto_enable_codemode_is_false(stack):
    harness, _calls, _servers, notifications = await setup(stack, "codemode", autoEnableCodemode=False)
    harness.setResponses([fauxAssistantMessage("ready")])
    await prompt(harness, "start")

    assert harness.session.getActiveToolNames() == []
    assert "mcp__docs__search" in nestedToolNames(harness)
    assert notifications == [
        "MCP tools are only reachable from the codemode or tool_search tool, but neither is active (autoEnableCodemode is false); they cannot be called.",
    ]


async def test_treats_codemode_mcp_tools_as_reachable_through_an_active_tool_search(stack):
    harness, _calls, _servers, notifications = await setup(
        stack, "codemode", autoEnableCodemode=False, builtInTools=["tool_search"]
    )
    harness.setResponses([
        fauxAssistantMessage([fauxToolCall("tool_search", {"query": "search the docs", "limit": 1})], {"stopReason": "toolUse"}),
        fauxAssistantMessage([fauxToolCall("mcp__docs__search", {"query": "loaded"})], {"stopReason": "toolUse"}),
        fauxAssistantMessage("done"),
    ])
    await prompt(harness, "go")

    assert harness.session.getActiveToolNames() == ["tool_search", "mcp__docs__search"]
    assert getMessageText(toolResult(harness, "mcp__docs__search")) == "loaded guide\nloaded faq"
    assert notifications == []


async def test_reaches_deferred_mcp_tools_through_codemode_when_tool_search_is_not_available(stack):
    harness, _calls, _servers, notifications = await setup(
        stack, "deferred", builtInTools=["codemode"], withoutToolSearch=True
    )
    harness.setResponses([fauxAssistantMessage("ready")])
    await prompt(harness, "start")

    assert harness.session.getActiveToolNames() == ["codemode"]
    assert "mcp__docs__search" in nestedToolNames(harness)
    assert notifications == []


async def test_warns_when_deferred_mcp_tools_have_neither_tool_search_nor_codemode(stack):
    harness, _calls, _servers, notifications = await setup(stack, "deferred", withoutToolSearch=True)
    harness.setResponses([fauxAssistantMessage("ready")])
    await prompt(harness, "start")

    assert harness.session.getActiveToolNames() == []
    assert notifications == [
        "MCP tools are only reachable from the codemode or tool_search tool, but neither is active; they cannot be called.",
    ]


async def test_tells_scripts_what_directly_exposed_mcp_tools_resolve_to(stack):
    harness, *_rest = await setup(stack, "direct")
    harness.session.setActiveToolsByName([*harness.session.getActiveToolNames(), "codemode"])
    assert (
        "Codemode: `tools.mcp__docs__search(args)` resolves to `{ content, structuredContent?: { hits }, isError? }`."
        in toolDescription(harness, "mcp__docs__search")
    )
    assert (
        "Codemode: `tools.mcp__docs__fail(args)` resolves to `{ content, structuredContent?, isError? }`."
        in toolDescription(harness, "mcp__docs__fail")
    )


async def test_activates_another_extensions_codemode_tool_when_it_reaches_other_tools(stack):
    def otherExec(pi) -> None:
        async def execute(*_args: Any) -> AgentToolResult:
            return AgentToolResult(content=[], details=None)

        pi.registerTool(ToolDefinition(
            name="codemode",
            label="codemode",
            description="Another extension's codemode tool.",
            parameters={"type": "object", "properties": {}},
            defaultActive=False,
            reachesTools=lambda _input: "all",
            execute=execute,
        ))

    harness, *_rest = await setup(stack, "codemode", extensionFactories=[otherExec])
    harness.setResponses([fauxAssistantMessage("ready")])
    await prompt(harness, "start")

    assert harness.session.getActiveToolNames() == ["codemode"]


async def test_does_not_activate_another_extensions_tool_named_codemode(stack):
    # Registered first, so it wins over the codemode extension's codemode.
    def otherExec(pi) -> None:
        async def execute(*_args: Any) -> AgentToolResult:
            return AgentToolResult(content=[], details=None)

        pi.registerTool(ToolDefinition(
            name="codemode",
            label="codemode",
            description="Another extension's codemode tool.",
            parameters={"type": "object", "properties": {}},
            defaultActive=False,
            execute=execute,
        ))

    harness, *_rest = await setup(stack, "codemode", extensionFactories=[otherExec])
    harness.setResponses([fauxAssistantMessage("ready")])
    await prompt(harness, "start")

    assert harness.session.getActiveToolNames() == []


async def setupSlow(
    stack: AsyncExitStack,
    config: dict[str, Any],
    initializeDelayMs: float,
    startupWaitMs: float | None = None,
):
    """A server named ``slow`` whose answer to ``initialize`` takes ``initializeDelayMs``, without waiting for it."""
    calls: list[str] = []
    notifications: list[str] = []
    config = dict(config)
    name = config.pop("name", "slow")
    instructions = config.pop("instructions", None)
    entry = {"name": name, "config": {"url": "http://unused.invalid", **config}, "source": "test"}

    def createTransport(*_args: Any) -> Any:
        pair = createFakeServer(calls, initializeDelayMs=initializeDelayMs, instructions=instructions)
        spawn(pair["server"].start(), _tasks)
        return pair["client"]

    harness = await stack.enter_async_context(createHarness(
        initialActiveToolNames=[],
        extensionFactories=[
            createCodemodeExtension(),
            createToolSearchExtension(),
            createMcpExtension({
                "loadConfig": lambda _ctx: {"servers": [entry], "errors": []},
                "createTransport": createTransport,
                **({} if startupWaitMs is None else {"startupWaitMs": startupWaitMs}),
            }),
        ],
    ))
    await harness.session.bindExtensions({
        "uiContext": createTestUiContext(notify=lambda message, *_args: notifications.append(message)),
    })
    return harness, calls, notifications


async def test_does_not_hold_the_first_prompt_for_codemode_servers_that_are_still_connecting(stack):
    # The server never answers `initialize`.
    harness, *_rest = await setupSlow(stack, {}, math.inf)
    harness.setResponses([fauxAssistantMessage("ready")])

    await prompt(harness, "start")

    assert getAssistantTexts(harness) == ["ready"]
    # Codemode is activated from the config, before the server connects.
    assert declaredToolNames(harness) == ["codemode"]


async def test_keeps_the_codemode_description_unchanged_when_the_server_connects(stack):
    harness, *_rest = await setupSlow(stack, {}, 20)
    before = toolDescription(harness, "codemode")
    assert before
    await waitFor(lambda: "mcp__slow__search" in harness.session.getCallableToolNames())
    assert toolDescription(harness, "codemode") == before


async def test_lists_a_server_before_it_connects_and_appends_its_summary_with_the_next_prompt(stack):
    harness, *_rest = await setupSlow(stack, {"instructions": "Slow docs.\nMore."}, 30)
    harness.setResponses([fauxAssistantMessage("one"), fauxAssistantMessage("two")])

    await prompt(harness, "first")
    await waitFor(lambda: "mcp__slow__search" in harness.session.getCallableToolNames())
    await prompt(harness, "second")

    systemMessages = [message for message in harness.session.messages if read_field(message, "role") == "system"]
    # The first request lists the server by name; its summary follows as an appended patch.
    assert len(systemMessages) == 2
    assert "- mcp__slow (codemode)\n" in systemMessages[0].sections[MCP_SERVERS_SECTION]
    assert list(systemMessages[1].sections) == [MCP_SERVERS_SECTION]
    assert "- mcp__slow (codemode): Slow docs." in systemMessages[1].sections[MCP_SERVERS_SECTION]
    messages = harness.session.messages
    firstAssistant = next(index for index, message in enumerate(messages) if read_field(message, "role") == "assistant")
    assert messages.index(systemMessages[1]) > firstAssistant


async def test_waits_for_servers_with_direct_tools_before_listing_the_servers(stack):
    harness, *_rest = await setupSlow(
        stack, {"exposure": "direct", "toolExposure": {"shot": "codemode"}, "instructions": "Slow docs."}, 30
    )
    harness.setResponses([fauxAssistantMessage("ready")])

    await prompt(harness, "start")

    assert "- mcp__slow (codemode): Slow docs." in serversSection(harness)


async def test_leaves_servers_with_only_direct_tools_out_of_the_servers_section(stack):
    harness, *_rest = await setupSlow(stack, {"exposure": "direct"}, 0)
    harness.setResponses([fauxAssistantMessage("ready")])

    await prompt(harness, "start")

    assert serversSection(harness) is None


async def test_waits_for_the_servers_a_codemode_script_names(stack):
    harness, calls, _notifications = await setupSlow(stack, {}, 30)
    harness.setResponses([
        fauxAssistantMessage(
            [fauxToolCall("codemode", {"code": 'return (await tools.mcp__slow__search({ query: "q" })).structuredContent;'})],
            {"stopReason": "toolUse"},
        ),
        fauxAssistantMessage("done"),
    ])

    await prompt(harness, "go")

    result = toolResult(harness, "codemode")
    assert result.isError is False
    assert json.loads(lastLine(result)) == {"hits": ["q guide", "q faq"]}
    assert calls == ['search:{"query":"q"}']


async def test_waits_for_servers_whose_script_identifiers_differ_from_their_names(stack):
    harness, calls, _notifications = await setupSlow(stack, {"name": "slow-docs"}, 30)
    harness.setResponses([
        fauxAssistantMessage(
            [
                fauxToolCall("codemode", {
                    "code": (
                        'await tools.mcp__slow_docs__search({ query: "q" });\n'
                        'const names = ["slow-docs", "slow_docs", "mcp__slow-docs", "mcp__slow_docs"];\n'
                        "return [...new Set(await Promise.all(names.map(async (name) => (await describeNamespace(name))?.name)))];"
                    ),
                }),
            ],
            {"stopReason": "toolUse"},
        ),
        fauxAssistantMessage("done"),
    ])

    await prompt(harness, "go")

    assert toolResult(harness, "codemode").isError is False
    assert lastLine(toolResult(harness, "codemode")) == '["mcp__slow_docs"]'
    assert calls == ['search:{"query":"q"}']


async def test_does_not_wait_for_servers_a_codemode_script_does_not_name(stack):
    harness, *_rest = await setupSlow(stack, {}, math.inf)
    harness.setResponses([
        fauxAssistantMessage([fauxToolCall("codemode", {"code": "return 1 + 1;"})], {"stopReason": "toolUse"}),
        fauxAssistantMessage("done"),
    ])

    await prompt(harness, "go")

    assert lastLine(toolResult(harness, "codemode")) == "2"


async def test_waits_for_servers_before_tool_search_searches(stack):
    harness, *_rest = await setupSlow(stack, {"exposure": "deferred"}, 30)
    harness.setResponses([
        fauxAssistantMessage([fauxToolCall("tool_search", {"query": "search the docs", "limit": 1})], {"stopReason": "toolUse"}),
        fauxAssistantMessage([fauxToolCall("mcp__slow__search", {"query": "late"})], {"stopReason": "toolUse"}),
        fauxAssistantMessage("done"),
    ])

    await prompt(harness, "go")

    assert getMessageText(toolResult(harness, "mcp__slow__search")) == "late guide\nlate faq"


async def test_holds_the_first_prompt_for_servers_with_direct_tools(stack):
    harness, *_rest = await setupSlow(stack, {"exposure": "direct"}, 30)
    harness.setResponses([fauxAssistantMessage("ready")])

    await prompt(harness, "start")

    assert "mcp__slow__search" in declaredToolNames(harness)


async def test_holds_the_first_prompt_for_servers_with_direct_tools_only_up_to_startup_wait_ms(stack):
    harness, _calls, notifications = await setupSlow(stack, {"exposure": "direct"}, math.inf, 20)
    harness.setResponses([fauxAssistantMessage("ready")])

    await prompt(harness, "start")

    assert getAssistantTexts(harness) == ["ready"]
    assert harness.session.getActiveToolNames() == []
    assert notifications == ["MCP servers are still connecting; their tools become available once connected."]


async def test_does_not_let_codemode_call_itself_or_inactive_direct_tools(stack):
    harness, *_rest = await setup(stack, "direct")
    harness.setResponses([fauxAssistantMessage("ready")])
    await prompt(harness, "start")
    harness.session.setActiveToolsByName(["codemode", "mcp__docs__search"])

    assert nestedToolNames(harness) == ["mcp__docs__search"]


async def test_finds_tools_from_scripts_with_search_tools_and_describe_tool(stack):
    harness, calls, _servers, _notifications = await setup(stack, "codemode")
    harness.setResponses([
        fauxAssistantMessage(
            [
                fauxToolCall("codemode", {
                    "code": """
                        const [match] = await searchTools("search the docs", { limit: 1 });
                        const none = await searchTools("docs", { namespace: "mcp__other" });
                        const declaration = await describeTool(match.name);
                        const result = await tools[match.name]({ query: "found" });
                        text(JSON.stringify({
                            name: match.name,
                            sameAsAllTools: ALL_TOOLS.find((tool) => tool.name === match.name).description === match.description,
                            none: none.length,
                            declared: declaration.includes("codemode tool declaration:"),
                            missing: (await describeTool("nope")) === undefined,
                            hits: result.structuredContent.hits,
                        }));
                    """,
                }),
            ],
            {"stopReason": "toolUse"},
        ),
        fauxAssistantMessage("done"),
    ])

    await prompt(harness, "go")

    result = toolResult(harness, "codemode")
    assert result.isError is False
    assert json.loads(result.content[1].text) == {
        "name": "mcp__docs__search",
        "sameAsAllTools": True,
        "none": 0,
        "declared": True,
        "missing": True,
        "hits": ["found guide", "found faq"],
    }
    assert calls == ['search:{"query":"found"}']


async def test_activates_tool_search_for_deferred_mcp_tools_and_keeps_loaded_tools_declared_on_the_branch(stack):
    # No built-in tools are active; the MCP extension activates tool_search, not codemode.
    harness, calls, _servers, _notifications = await setup(stack, "deferred")
    searchName = createMcpToolName("docs", "search")
    harness.setResponses([
        fauxAssistantMessage([fauxToolCall("tool_search", {"query": "search the docs", "limit": 1})], {"stopReason": "toolUse"}),
        fauxAssistantMessage([fauxToolCall(searchName, {"query": "loaded"})], {"stopReason": "toolUse"}),
        fauxAssistantMessage("done"),
    ])
    await prompt(harness, "find a docs tool")

    assert harness.session.getActiveToolNames() == ["tool_search", searchName]
    # The description does not depend on the connected servers.
    assert toolDescription(harness, "tool_search") == TOOL_SEARCH_DESCRIPTION

    search = toolResult(harness, "tool_search")
    assert getMessageText(search) == f"Loaded 1 tool. They are available from your next call:\n- {searchName}: Search the docs."
    # Only the loaded tool is added; earlier declarations are not repeated.
    loadMessages = [
        message
        for message in harness.session.messages
        if read_field(message, "role") == "system"
        and any(read_field(tool, "name") == searchName for tool in (read_field(message, "toolsAdded") or []))
    ]
    assert len(loadMessages) == 1
    assert [read_field(tool, "name") for tool in loadMessages[0].toolsAdded] == [searchName]
    assert getMessageText(toolResult(harness, searchName)) == "loaded guide\nloaded faq"
    assert calls == ['search:{"query":"loaded"}']

    # Loads are recorded in the transcript: navigating back before the load drops the tool,
    # navigating to a later entry restores it.
    branch = harness.sessionManager.getBranch()
    firstUser = firstEntry(harness, "user")
    last = branch[-1]
    await harness.session.navigateTree(read_field(firstUser, "id"))
    assert searchName not in harness.session.getActiveToolNames()
    await harness.session.navigateTree(read_field(last, "id"))
    assert searchName in harness.session.getActiveToolNames()


async def test_finds_nothing_to_load_when_every_matching_tool_is_already_declared(stack):
    harness, *_rest = await setup(stack, "direct", builtInTools=["tool_search"])
    harness.setResponses([
        fauxAssistantMessage([fauxToolCall("tool_search", {"query": "docs"})], {"stopReason": "toolUse"}),
        fauxAssistantMessage("done"),
    ])
    await prompt(harness, "go")
    assert getMessageText(toolResult(harness, "tool_search")) == "No matching tools found."


# -- AgentSession MCP servers registered by extensions -------------------------------------------


async def setupRegistered(stack: AsyncExitStack, plugins: Any, configured: list[dict[str, Any]] | None = None):
    """``configured`` are the mcp.json servers; ``plugins`` register servers through the extension API."""
    connected: list[dict[str, Any]] = []

    def createTransport(entry: Any, *_args: Any) -> Any:
        connected.append(entry)
        pair = createFakeServer([])
        spawn(pair["server"].start(), _tasks)
        return pair["client"]

    harness = await stack.enter_async_context(createHarness(
        initialActiveToolNames=[],
        extensionFactories=[
            *(plugins if isinstance(plugins, list) else [plugins]),
            createCodemodeExtension(),
            createMcpExtension({
                "loadConfig": lambda _ctx: {"servers": configured or [], "errors": []},
                "createTransport": createTransport,
            }),
        ],
    ))
    # `/reload` emits session_start only to bound extensions.
    await harness.session.bindExtensions({"uiContext": createTestUiContext()})
    return harness, connected


async def test_connects_servers_registered_while_extensions_load(stack):
    def plugin(pi) -> None:
        pi.registerMcpServer("plugin", {"url": "http://plugin.invalid", "exposure": "direct"})

    harness, connected = await setupRegistered(stack, plugin)
    harness.setResponses([fauxAssistantMessage("ready")])
    await prompt(harness, "start")

    assert [[entry["name"], entry.get("scope")] for entry in connected] == [["plugin", "extension"]]
    assert "mcp__plugin__search" in harness.session.getActiveToolNames()


async def test_connects_and_disconnects_servers_registered_during_the_session(stack):
    api: Any = None

    def capture(pi) -> None:
        nonlocal api
        api = pi

    harness, connected = await setupRegistered(stack, capture)
    assert api is not None
    pi = api

    pi.registerMcpServer("late", {"url": "http://late.invalid"})
    await waitFor(lambda: "mcp__late__search" in harness.session.getCallableToolNames())
    assert [entry["name"] for entry in connected] == ["late"]
    # Codemode-exposed tools need the codemode tool, which is activated for them.
    assert "codemode" in harness.session.getActiveToolNames()

    pi.unregisterMcpServer("late")
    await waitFor(lambda: "mcp__late__search" not in harness.session.getCallableToolNames())


# "my_docs" shares the namespace of "my-docs" (#10239).
@pytest.mark.parametrize("name", ["my-docs", "my_docs"])
async def test_prefers_the_mcp_json_server_over_a_registered_one(stack, name):
    configured = {"name": "my-docs", "config": {"url": "http://config.invalid"}, "source": "mcp.json"}

    def plugin(pi) -> None:
        pi.registerMcpServer(name, {"url": "http://plugin.invalid"})

    _harness, connected = await setupRegistered(stack, plugin, [configured])
    await waitFor(lambda: connected == [configured])
    await asyncio.sleep(0.01)
    assert connected == [configured]


async def test_rejects_names_another_extension_registered(stack):
    errors: list[str] = []

    def first(pi) -> None:
        pi.registerMcpServer("taken", {"url": "http://x.invalid"})
        # Registering again replaces the extension's own registration.
        pi.registerMcpServer("taken", {"url": "http://y.invalid"})
        pi.registerMcpServer("my-server", {"url": "http://x.invalid"})

    def second(pi) -> None:
        for name in ("taken", "my_server"):
            try:
                pi.registerMcpServer(name, {"url": "http://z.invalid"})
            except Exception as caught:  # noqa: BLE001 - recorded for the assertion
                errors.append(str(caught))

    await setupRegistered(stack, [first, second])
    assert len(errors) == 2
    assert re.search(r'MCP server "taken" is already registered by extension', errors[0])
    # Names that differ only in - and _ share a namespace (#10239).
    assert errors[1] == 'MCP server "my_server" conflicts with registered server "my-server"'


async def test_reports_registered_servers_when_no_extension_connects_them(stack):
    def orphan(pi) -> None:
        pi.registerMcpServer("orphan", {"url": "http://orphan.invalid"})

    harness = await stack.enter_async_context(createHarness(extensionFactories=[orphan]))
    errors: list[str] = []
    await harness.session.bindExtensions({"onError": lambda error: errors.append(read_field(error, "error"))})

    assert len(errors) == 1
    assert 'MCP server "orphan" is registered, but no loaded extension' in errors[0]


# -- AgentSession MCP tools after resume and reload -----------------------------------------------


class _ReloadingResourceLoader:
    """``createTestResourceLoader()`` whose ``reload`` loads the extensions again."""

    def __init__(self, base: Any, factories: list[Any], extensions: Any) -> None:
        self._base = base
        self._factories = factories
        self._extensions = extensions

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def getExtensions(self) -> Any:
        return self._extensions

    async def reload(self, *_args: Any) -> None:
        self._extensions = await createTestExtensionsResult(self._factories, os.getcwd())


async def setupResumable(
    stack: AsyncExitStack,
    sessionManager: Any = None,
    extensionFactories: list[Any] | None = None,
    initializeDelayMs: float = 0,
):
    """A deferred ``docs`` server that answers ``initialize`` after ``initializeDelayMs``; ``connected`` counts
    its connections. ``/reload`` loads the extensions again."""
    connected: list[str] = []
    servers = [{"name": "docs", "config": {"url": "http://unused.invalid", "exposure": "deferred"}, "source": "test"}]

    def createTransport(entry: Any, *_args: Any) -> Any:
        connected.append(entry["name"])
        pair = createFakeServer([], initializeDelayMs=initializeDelayMs)
        spawn(pair["server"].start(), _tasks)
        return pair["client"]

    factories = [
        *(extensionFactories or []),
        createToolSearchExtension(),
        createMcpExtension({"loadConfig": lambda _ctx: {"servers": servers, "errors": []}, "createTransport": createTransport}),
    ]
    extensions = await createTestExtensionsResult(factories, os.getcwd())
    resourceLoader = _ReloadingResourceLoader(createTestResourceLoader(), factories, extensions)
    harness = await stack.enter_async_context(createHarness(resourceLoader=resourceLoader, sessionManager=sessionManager))
    # `/reload` emits session_start only to bound extensions.
    await harness.session.bindExtensions({"uiContext": createTestUiContext()})
    return harness, connected


async def loadDocsSearch(harness: Harness) -> None:
    harness.setResponses([
        fauxAssistantMessage([fauxToolCall("tool_search", {"query": "search the docs", "limit": 1})], {"stopReason": "toolUse"}),
        fauxAssistantMessage("loaded"),
    ])
    await prompt(harness, "load")
    assert "mcp__docs__search" in harness.session.getActiveToolNames()


async def test_declares_tools_tool_search_loaded_again_on_resume_once_their_server_connects(stack):
    first, _connected = await setupResumable(stack)
    await loadDocsSearch(first)

    # The session restores its tools before the server connects again.
    second, _connected = await setupResumable(stack, first.sessionManager)
    await waitFor(lambda: "mcp__docs__search" in second.session.getActiveToolNames())
    second.setResponses([
        fauxAssistantMessage([fauxToolCall("mcp__docs__search", {"query": "again"})], {"stopReason": "toolUse"}),
        fauxAssistantMessage("done"),
    ])
    await prompt(second, "use it")

    assert getMessageText(toolResult(second, "mcp__docs__search")) == "again guide\nagain faq"
    removals = [
        message
        for message in second.session.messages
        if read_field(message, "role") == "system" and len(read_field(message, "toolsRemoved") or []) > 0
    ]
    assert removals == []


@pytest.mark.parametrize(("loadout", "kept"), [(["read"], False), (None, True)], ids=["drops", "keeps"])
async def test_restored_tools_when_an_extension_sets_the_loadout_before_they_register(stack, loadout, kept):
    first, _connected = await setupResumable(stack)
    await loadDocsSearch(first)

    # Like plan mode restoring its tools, or an extension adding one to the current loadout.
    def setLoadout(pi) -> None:
        pi.on(
            "session_start",
            lambda _event, _ctx: pi.setActiveTools([*loadout] if loadout else [*pi.getActiveTools(), "read"]),
        )

    second, _connected = await setupResumable(stack, first.sessionManager, [setLoadout])
    await waitFor(lambda: "mcp__docs__search" in toolNames(second))

    assert ("mcp__docs__search" in second.session.getActiveToolNames()) is kept


async def test_does_not_activate_restored_tools_that_register_after_the_next_prompt_starts(stack):
    first, _connected = await setupResumable(stack)
    await loadDocsSearch(first)

    # The first prompt does not wait for servers without direct tools.
    second, _connected = await setupResumable(stack, first.sessionManager, [], 200)
    second.setResponses([fauxAssistantMessage("done")])
    await prompt(second, "go")
    await waitFor(lambda: "mcp__docs__search" in toolNames(second))

    assert "mcp__docs__search" not in second.session.getActiveToolNames()


async def test_declares_tools_tool_search_loaded_again_after_reload(stack):
    harness, connected = await setupResumable(stack)
    await loadDocsSearch(harness)

    await harness.session.reload()

    await waitFor(lambda: connected == ["docs", "docs"])
    await waitFor(lambda: "mcp__docs__search" in harness.session.getActiveToolNames())


async def test_the_manager_shows_why_disabling_a_server_could_not_be_saved(stack, monkeypatch):
    import misaka.extensions.mcp as mcp_extension

    calls: list[str] = []
    entry = {"name": "docs", "config": {"url": "http://unused.invalid", "exposure": "direct"}, "source": "mcp.json"}

    def createTransport(*_args: Any) -> Any:
        pair = createFakeServer(calls)
        spawn(pair["server"].start(), _tasks)
        return pair["client"]

    def updateConfig(_entry: Any, _patch: Any) -> None:
        raise OSError("read-only file system")

    harness = await stack.enter_async_context(createHarness(extensionFactories=[
        createMcpExtension({
            "loadConfig": lambda _ctx: {"servers": [entry], "errors": []},
            "createTransport": createTransport,
            "updateConfig": updateConfig,
        }),
    ]))
    await harness.session.bindExtensions({"uiContext": createTestUiContext()})
    await waitFor(lambda: "mcp__docs__search" in toolNames(harness))

    answers = ["docs", "disable", None, None]
    menus: list[Any] = []

    class Ui:
        async def menu(self, build: Any, _subscribe: Any) -> Any:
            await asyncio.sleep(0.05)
            menus.append(build())
            return answers.pop(0)

    async def showMcpManager(_ctx: Any, run: Any) -> None:
        await run(Ui())

    monkeypatch.setattr(mcp_extension, "showMcpManager", showMcpManager)
    runner = harness.session.extensionRunner
    runner.mode = "tui"
    await runner.getCommand("mcp").handler("", runner.createCommandContext())

    assert "Could not update mcp.json: read-only file system" in repr(menus[2])
