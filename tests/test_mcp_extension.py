"""pi's ``test/mcp-extension.test.ts``: config loading, tool conversion, connections and the
``mcp_servers`` section, plus the places where Python would diverge by default."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

from misaka.codemode.declarations import renderToolOutputType
from misaka.config import home
from misaka.core.auth_storage import InMemoryAuthStorageBackend
from misaka.core.tools.truncate import truncateMiddle
from misaka.extensions.mcp import MAX_SERVERS_SECTION_CHARS, renderServersSection
from misaka.extensions.mcp.config import (
    getMcpToolExposure,
    loadMcpConfig,
    updateMcpServerConfig,
)
from misaka.extensions.mcp.runtime import (
    McpOAuthCredentialStore,
    McpServerConnection,
    McpServerLog,
    createDefaultTransport,
)
from misaka.extensions.mcp.tools import (
    convertMcpResult,
    createMcpResultSchema,
    createMcpToolName,
)
from misaka.mcp import (
    LATEST_PROTOCOL_VERSION,
    McpAuthRequiredError,
    McpHttpError,
    McpSessionExpiredError,
)
from misaka.mcp.testing import createInMemoryTransportPair
from misaka.utils.js import spawn
from misaka.utils.whatwg_fetch import Response
from tests.mcp_helpers import closeServers, listen, readBody

# Config values are resolved at connect time, so the literal reference must survive loading.
TOKEN_HEADER = "Bearer ${TOKEN}"


@pytest.fixture(autouse=True)
def _noProxy(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
async def _servers():
    yield
    await closeServers()


def dumped(content) -> list[dict]:
    return [block.model_dump(exclude_none=True) for block in content]


# -- MCP config -----------------------------------------------------------------------------------


def setup(tmp_path, globalConfig, project):
    agentDir = tmp_path / "agent"
    cwd = tmp_path / "project"
    agentDir.mkdir()
    projectDir = home.project_dir(cwd)
    assert projectDir is not None
    projectDir.mkdir(parents=True)
    (agentDir / "mcp.json").write_text(json.dumps(globalConfig), encoding="utf-8")
    (projectDir / "mcp.json").write_text(json.dumps(project), encoding="utf-8")
    return {"agentDir": str(agentDir), "cwd": str(cwd)}


def test_merges_global_and_trusted_project_servers_and_validates_entries(tmp_path):
    paths = setup(
        tmp_path,
        {
            "mcpServers": {
                "shared": {"command": "global-cmd"},
                "remote": {"url": "https://example.com/mcp", "headers": {"Authorization": TOKEN_HEADER}},
                "off": {"command": "x", "enabled": False},
                "bad": {"args": ["no command"]},
                "legacy": {"type": "sse", "url": "https://example.com/sse"},
                "badUrl": {"url": "example.com/mcp"},
                "bad name": {"command": "x"},
            },
        },
        {"mcpServers": {"shared": {"command": "project-cmd", "exposure": "direct"}}},
    )

    trusted = loadMcpConfig({**paths, "projectTrusted": True})
    # Disabled servers are kept so /mcp can enable them again.
    assert [(server["name"], server.get("scope"), server["config"]) for server in trusted["servers"]] == [
        ("shared", "project", {"command": "project-cmd", "exposure": "direct"}),
        ("remote", "global", {"url": "https://example.com/mcp", "headers": {"Authorization": TOKEN_HEADER}}),
        ("off", "global", {"command": "x", "enabled": False}),
    ]
    assert len(trusted["errors"]) == 4
    assert 'server "bad" needs either "command"' in trusted["errors"][0]
    assert "legacy SSE transport is not supported" in trusted["errors"][1]
    assert 'server "badUrl": url must be an http or https URL' in trusted["errors"][2]
    assert 'invalid server name "bad name"' in trusted["errors"][3]

    # Untrusted projects cannot add or override servers, since stdio servers run commands.
    untrusted = loadMcpConfig({**paths, "projectTrusted": False})
    shared = next(server for server in untrusted["servers"] if server["name"] == "shared")
    assert shared["config"] == {"command": "global-cmd"}


# #10277
def test_lets_project_entries_override_enabled_and_exposure_of_global_servers(tmp_path):
    paths = setup(
        tmp_path,
        {"mcpServers": {"tools": {"command": "x", "env": {"TOKEN": "secret"}}}},
        # An override cannot change the command, which would run with the global env.
        {"mcpServers": {"tools": {"enabled": False, "args": ["y"]}, "missing": {"enabled": False}}},
    )
    projectDir = home.project_dir(paths["cwd"])
    assert projectDir is not None
    project = str(projectDir / "mcp.json")
    loaded = loadMcpConfig({**paths, "projectTrusted": True})
    assert [(server["name"], server.get("override"), server["config"]) for server in loaded["servers"]] == [
        ("tools", None, {"command": "x", "env": {"TOKEN": "secret"}}),
    ]
    assert len(loaded["errors"]) == 2
    assert 'server "tools": an override can only set enabled, exposure, toolExposure' in loaded["errors"][0]
    assert 'server "missing" needs "command" or "url", or a global server to override' in loaded["errors"][1]

    Path(project).write_text(json.dumps({"mcpServers": {"tools": {"enabled": False}}}), encoding="utf-8")
    [tools] = loadMcpConfig({**paths, "projectTrusted": True})["servers"]
    assert [tools.get("override"), tools["config"]] == [
        project,
        {"command": "x", "env": {"TOKEN": "secret"}, "enabled": False},
    ]

    # Overrides keep `enabled: true`, since it replaces the global value.
    updateMcpServerConfig(project, "tools", {"enabled": True})
    assert json.loads(Path(project).read_text(encoding="utf-8"))["mcpServers"]["tools"] == {"enabled": True}


# Regression: #10239.
def test_rejects_server_names_that_differ_only_in_dash_and_underscore(tmp_path):
    paths = setup(tmp_path, {"mcpServers": {"work-files": {"command": "a"}, "work_files": {"command": "b"}}}, {})
    loaded = loadMcpConfig({**paths, "projectTrusted": False})
    assert [server["name"] for server in loaded["servers"]] == ["work-files"]
    assert len(loaded["errors"]) == 1 and 'server "work_files" conflicts with "work-files"' in loaded["errors"][0]


def test_validates_exposure_and_reads_auto_enable_codemode_with_project_precedence(tmp_path):
    paths = setup(
        tmp_path,
        {
            "autoEnableCodemode": False,
            "mcpServers": {
                "later": {"command": "x", "exposure": "deferred"},
                # `codemode-deferred` is an alias for `codemode`.
                "scripts": {"command": "x", "exposure": "codemode-deferred", "toolExposure": {"a": "codemode-deferred"}},
                "off": {"command": "x", "exposure": "hidden"},
                "wrong": {"command": "x", "exposure": "model-only"},
                "described": {"command": "x", "description": "Docs search"},
                "badDescription": {"command": "x", "description": 1},
            },
        },
        {"autoEnableCodemode": "yes", "mcpServers": {}},
    )

    untrusted = loadMcpConfig({**paths, "projectTrusted": False})
    assert untrusted["autoEnableCodemode"] is False
    assert [(server["name"], server["config"].get("exposure")) for server in untrusted["servers"]] == [
        ("later", "deferred"),
        ("scripts", "codemode"),
        ("off", "hidden"),
        ("described", None),
    ]
    assert untrusted["servers"][1]["config"]["toolExposure"] == {"a": "codemode"}
    assert untrusted["servers"][3]["config"]["description"] == "Docs search"
    assert len(untrusted["errors"]) == 2
    assert 'server "wrong": exposure must be one of' in untrusted["errors"][0]
    assert 'server "badDescription": description must be a string' in untrusted["errors"][1]

    trusted = loadMcpConfig({**paths, "projectTrusted": True})
    assert trusted["autoEnableCodemode"] is False
    assert any("autoEnableCodemode must be a boolean" in error for error in trusted["errors"])


def test_validates_the_oauth_callback_url_scope_and_client_name(tmp_path):
    paths = setup(
        tmp_path,
        {
            "mcpServers": {
                "ok": {"url": "https://a.example/mcp", "oauth": {"callbackUrl": "http://localhost:8080/callback", "scope": "a b"}},
                "ipv6": {"url": "https://a.example/mcp", "oauth": {"callbackUrl": "http://[::1]/cb", "callbackPort": 9000}},
                "same": {"url": "https://a.example/mcp", "oauth": {"callbackUrl": "http://127.0.0.1:2/cb", "callbackPort": 2}},
                "remote": {"url": "https://a.example/mcp", "oauth": {"callbackUrl": "https://example.com/callback"}},
                "both": {"url": "https://a.example/mcp", "oauth": {"callbackUrl": "http://127.0.0.1:1/cb", "callbackPort": 2}},
                "scope": {"url": "https://a.example/mcp", "oauth": {"scope": ["a"]}},
                "named": {"url": "https://a.example/mcp", "oauth": {"clientName": "Claude Code"}},
                "unnamed": {"url": "https://a.example/mcp", "oauth": {"clientName": " "}},
                "metadata": {"url": "https://a.example/mcp", "oauth": {"authServerMetadataUrl": "https://idp.example/m"}},
                "plainMetadata": {"url": "https://a.example/mcp", "oauth": {"authServerMetadataUrl": "http://idp.example/m"}},
                # #10302
                "cimd": {
                    "url": "https://a.example/mcp",
                    "oauth": {"clientRegistration": "cimd", "callbackUrl": "http://localhost/callback"},
                },
                "badRegistration": {"url": "https://a.example/mcp", "oauth": {"clientRegistration": "auto"}},
                "cimdClient": {"url": "https://a.example/mcp", "oauth": {"clientRegistration": "cimd", "clientId": "x"}},
                "cimdPath": {
                    "url": "https://a.example/mcp",
                    "oauth": {"clientRegistration": "cimd", "callbackUrl": "http://127.0.0.1/cb"},
                },
            },
        },
        {},
    )
    loaded = loadMcpConfig({**paths, "projectTrusted": False})
    # MISAKA fork: a valid `cimd` entry is rejected too, so "cimd" is not among the servers.
    assert [server["name"] for server in loaded["servers"]] == ["ok", "ipv6", "same", "named", "metadata"]
    expected = [
        'server "remote": oauth.callbackUrl must be an http URI on localhost',
        'server "both": oauth.callbackUrl and oauth.callbackPort name different ports',
        'server "scope": oauth.scope must be a string',
        'server "unnamed": oauth.clientName must be a non-empty string',
        'server "plainMetadata": oauth.authServerMetadataUrl must be an https URL',
        'server "cimd": oauth.clientRegistration "cimd" is not supported',
        'server "badRegistration": oauth.clientRegistration must be "dcr" or "cimd"',
        'server "cimdClient": oauth.clientRegistration "cimd" cannot be combined',
        'server "cimdPath": oauth.clientRegistration "cimd" requires oauth.callbackUrl',
    ]
    assert len(loaded["errors"]) == len(expected)
    for error, fragment in zip(loaded["errors"], expected, strict=True):
        assert fragment in error


def test_resolves_per_tool_exposure_from_exact_names_then_patterns_in_order(tmp_path):
    paths = setup(
        tmp_path,
        {
            "mcpServers": {
                "gh": {
                    "command": "x",
                    "exposure": "deferred",
                    "toolExposure": {"get_*": "codemode", "get_me": "direct", "*delete*": "hidden", "get_file.*": "direct"},
                },
                "bad": {"command": "x", "toolExposure": {"a": "visible"}},
            },
        },
        {},
    )
    loaded = loadMcpConfig({**paths, "projectTrusted": False})
    assert len(loaded["errors"]) == 1 and 'server "bad": toolExposure "a" must be one of' in loaded["errors"][0]
    config = loaded["servers"][0]["config"]
    assert getMcpToolExposure(config, "get_me") == "direct"
    assert getMcpToolExposure(config, "get_issue") == "codemode"
    assert getMcpToolExposure(config, "get_delete_hint") == "codemode"
    assert getMcpToolExposure(config, "delete_repo") == "hidden"
    assert getMcpToolExposure(config, "list_issues") == "deferred"
    # Only `*` is special.
    assert getMcpToolExposure({"command": "x", "toolExposure": {"get_file.*": "direct"}}, "get_file_x") == "codemode"


def test_validates_provider_auth_and_accepts_it_only_in_the_global_mcp_json(tmp_path):
    paths = setup(
        tmp_path,
        {
            "mcpServers": {
                "radius": {"url": "https://radius.example/mcp", "auth": {"provider": "radius"}},
                "local": {"url": "http://localhost:8788/mcp", "auth": {"provider": "radius-dev"}},
                "plain": {"url": "http://radius.example/mcp", "auth": {"provider": "radius"}},
                "empty": {"url": "https://radius.example/mcp", "auth": {"provider": ""}},
            },
        },
        {"mcpServers": {"radius": {"url": "https://evil.example/mcp", "auth": {"provider": "radius"}}}},
    )
    loaded = loadMcpConfig({**paths, "projectTrusted": True})
    # The project entry cannot replace the global one: it would send the credential to its own URL.
    assert [(server["name"], server.get("scope"), server["config"].get("url")) for server in loaded["servers"]] == [
        ("radius", "global", "https://radius.example/mcp"),
        ("local", "global", "http://localhost:8788/mcp"),
    ]
    expected = [
        'server "plain": auth requires an https URL',
        'server "empty": auth.provider must be a provider name',
        'server "radius": auth is only allowed in the global mcp.json',
    ]
    assert len(loaded["errors"]) == len(expected)
    for error, fragment in zip(loaded["errors"], expected, strict=True):
        assert fragment in error


# -- MCP tools ------------------------------------------------------------------------------------


def test_creates_provider_safe_tool_names():
    assert createMcpToolName("docs", "search") == "mcp__docs__search"
    assert createMcpToolName("my-server", "get.item/v2") == "mcp__my_server__get_item_v2"
    long = createMcpToolName("server", "x" * 100)
    assert len(long) == 64
    assert re.fullmatch(r"mcp__server__x+_[0-9a-f]{8}", long)
    assert createMcpToolName("server", f"{'x' * 100}y") != long
    # Names that sanitize to one already taken by another tool get a hash suffix.
    taken = createMcpToolName("s", "a_b")
    second = createMcpToolName("s", "a-b", lambda name: name == taken)
    assert re.fullmatch(r"mcp__s__a_b_[0-9a-f]{8}", second)
    # A character outside the BMP is two UTF-16 units, so two underscores.
    assert createMcpToolName("s", "a\U0001f600b") == "mcp__s__a__b"


def test_declares_the_call_tool_result_scripts_receive_keeping_references_in_the_servers_output_schema():
    content = (
        'content: Array<{ data?: string; mimeType?: string; text?: string; type: "text" | "image" | "audio" | '
        '"resource_link" | "resource"; [key: string]: unknown; }>; isError?: boolean;'
    )
    # #10416: without an output schema, structured content may be any JSON value.
    assert renderToolOutputType(createMcpResultSchema(None)) == f"{{ {content} structuredContent?: unknown; [key: string]: unknown; }}"

    hit = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}
    schema = createMcpResultSchema({
        "type": "object",
        "properties": {
            "hits": {"type": "array", "items": {"$ref": "#/$defs/Hit~1v1"}},
            "again": {"$ref": "#/properties/hits"},
            "children": {"type": "array", "items": {"$ref": "#"}},
            # A property named like a keyword whose value is data.
            "default": {"$ref": "#/definitions/Hit"},
            "remote": {"$ref": "https://example.com/schema.json"},
        },
        "required": ["hits"],
        "$defs": {"Hit/v1": {**hit, "default": {"$ref": "#/stays"}}},
        "definitions": {"Hit": hit},
    })
    # Data that looks like a reference is kept as is.
    assert schema["properties"]["structuredContent"]["$defs"]["Hit/v1"]["default"] == {"$ref": "#/stays"}
    assert renderToolOutputType(schema) == (
        f"{{ {content} structuredContent?: {{ again?: Array<{{ id: string; }}>; children?: Array<{{ again?: "
        "Array<{ id: string; }>; children?: Array<unknown>; default?: { id: string; }; hits: Array<{ id: string; }>; "
        "remote?: unknown; }>; default?: { id: string; }; hits: Array<{ id: string; }>; remote?: unknown; }; "
        "[key: string]: unknown; }"
    )


def test_relocates_only_local_references_and_leaves_data_and_names_alone():
    """MISAKA adaptation: ``relocateSchema`` on Python values -- lists map item by item, other non-dicts pass
    through, map keywords keep their keys, data keywords are copied as they are."""
    from misaka.extensions.mcp.tools import relocateSchema

    pointer = "#/properties/structuredContent"
    assert relocateSchema({"$ref": "#"}, pointer) == {"$ref": pointer}
    assert relocateSchema([{"$ref": "#/a"}, 1, None], pointer) == [{"$ref": f"{pointer}/a"}, 1, None]
    assert relocateSchema({"$ref": "other.json#/a"}, pointer) == {"$ref": "other.json#/a"}
    assert relocateSchema({"enum": [{"$ref": "#"}], "examples": [{"$ref": "#"}]}, pointer) == {
        "enum": [{"$ref": "#"}],
        "examples": [{"$ref": "#"}],
    }
    assert relocateSchema({"properties": {"$ref": {"$ref": "#/x"}}}, pointer) == {
        "properties": {"$ref": {"$ref": f"{pointer}/x"}}
    }
    # A map keyword whose value is not an object is a schema like any other.
    assert relocateSchema({"properties": [{"$ref": "#"}]}, pointer) == {"properties": [{"$ref": pointer}]}


async def test_converts_results_passing_the_call_tool_result_to_scripts_and_flagging_errors():
    blocks = [
        {"type": "resource_link", "uri": "file:///a", "name": "a"},
        {"type": "resource", "resource": {"uri": "file:///b", "text": "b text"}},
        {"type": "audio", "data": "", "mimeType": "audio/wav"},
    ]
    converted = await convertMcpResult(
        "docs", "t", {"content": blocks, "structuredContent": {"ok": True}, "_meta": {"trace": "x"}}
    )
    assert dumped(converted.content) == [
        {"type": "text", "text": '[Resource file:///a "a"]'},
        {"type": "text", "text": "b text"},
        {"type": "text", "text": "[audio audio/wav omitted]"},
    ]
    assert converted.details == {"server": "docs", "tool": "t"}
    # Scripts get the server's blocks as sent, without `_meta`.
    assert converted.structuredContent == {"content": blocks, "structuredContent": {"ok": True}}
    assert converted.isError is None

    empty = await convertMcpResult("docs", "t", {"content": [], "structuredContent": {"n": 1}})
    assert dumped(empty.content) == [{"type": "text", "text": '{\n  "n": 1\n}'}]
    # #10416: array structured content reaches the model as JSON and scripts as sent.
    listed = await convertMcpResult("docs", "t", {"content": [], "structuredContent": [{"id": 1}]})
    assert dumped(listed.content) == [{"type": "text", "text": '[\n  {\n    "id": 1\n  }\n]'}]
    assert listed.details == {"server": "docs", "tool": "t"}
    assert listed.structuredContent == {"content": [], "structuredContent": [{"id": 1}]}

    failed = await convertMcpResult("docs", "t", {"content": [{"type": "text", "text": "nope"}], "isError": True})
    assert dumped(failed.content) == [{"type": "text", "text": "nope"}]
    assert failed.details == {"server": "docs", "tool": "t"}
    assert failed.structuredContent == {"content": [{"type": "text", "text": "nope"}], "isError": True}
    assert failed.isError is True

    silent = await convertMcpResult("docs", "t", {"content": [], "isError": True})
    assert dumped(silent.content) == [{"type": "text", "text": "MCP tool docs/t returned an error"}]


async def test_blocks_missing_fields_convert_as_javascript_reads_them():
    blocks = [
        {"type": "text"},
        {"type": "resource_link", "name": "a"},
        {"type": "resource", "resource": []},
        {"type": "resource", "resource": {"uri": "file:///b", "mimeType": None, "blob": ""}},
        "plain",
    ]
    async def saveOutput(_data, _extension):
        return "/tmp/saved"

    converted = await convertMcpResult("docs", "t", {"content": blocks}, {"saveOutput": saveOutput})
    assert dumped(converted.content) == [
        {"type": "text", "text": ""},
        {"type": "text", "text": '[Resource undefined "a"]'},
        {"type": "text", "text": "[binary resource undefined (unknown type) omitted]"},
        {"type": "text", "text": "[Binary resource file:///b (unknown type, 0B) saved to /tmp/saved]"},
        {"type": "text", "text": "[unsupported MCP content undefined]"},
    ]


def test_tool_annotations_that_are_not_an_object_have_no_hints():
    from misaka.extensions.mcp.tools import toToolAnnotations

    assert toToolAnnotations({"name": "t", "inputSchema": {}, "annotations": "readOnly"}) is None  # type: ignore[typeddict-item]


def test_a_json_rpc_id_past_the_double_range_is_not_an_id():
    from misaka.mcp.protocol.jsonrpc import isJsonRpcId

    assert not isJsonRpcId(10**400)
    assert isJsonRpcId(7) and isJsonRpcId("7")


async def test_points_resource_links_to_read_mcp_resource_and_saves_binary_resources():
    saved: list = []

    async def saveOutput(data, extension):
        saved.append((data, extension))
        return f"/tmp/saved{extension}"

    converted = await convertMcpResult(
        "docs",
        "t",
        {
            "content": [
                {
                    "type": "resource_link",
                    "uri": "docs://guide",
                    "name": "guide",
                    "title": "The Guide",
                    "mimeType": "text/markdown",
                    "size": 2048,
                    "description": "How to use it",
                },
                {"type": "resource", "resource": {"uri": "file:///r/report.pdf", "mimeType": "application/pdf", "blob": "JVBERg=="}},
                {"type": "resource", "resource": {"uri": "docs://logo", "mimeType": "image/png", "blob": "AAAA"}},
            ],
        },
        {"saveOutput": saveOutput, "readableResources": True},
    )
    assert dumped(converted.content) == [
        {
            "type": "text",
            "text": '[Resource docs://guide "The Guide" (text/markdown, 2.0KB): How to use it. Read it with read_mcp_resource (server "docs")]',
        },
        {"type": "text", "text": "[Binary resource file:///r/report.pdf (application/pdf, 4B) saved to /tmp/saved.pdf]"},
        {"type": "image", "data": "AAAA", "mimeType": "image/png"},
    ]
    assert saved == [(b"%PDF", ".pdf")]


async def test_cuts_the_middle_of_model_facing_text_over_20kb_and_keeps_the_full_result_for_scripts():
    saved: list = []

    async def saveOutput(data, _extension):
        saved.append(data)
        return "/tmp/full.txt"

    lines = [f"line {index + 1}" for index in range(3000)]
    full = "\n".join(lines)
    image = {"type": "image", "data": "AAAA", "mimeType": "image/png"}
    result = {"content": [{"type": "text", "text": full}, image]}
    converted = await convertMcpResult("docs", "snapshot", result, {"saveOutput": saveOutput})
    assert len(converted.content) == 2
    text = converted.content[0].text
    # Codex's format: a header, the start and end of the text, then the file with the full text.
    assert re.match(
        f"^Warning: truncated output \\(original token count: {-(-len(full) // 4)}\\)\nTotal output lines: 3000\n\nline 1\nline 2\n",
        text,
    )
    assert re.search(r"…\d+ chars truncated…", text)
    assert text.endswith("line 3000\n\n[Full output: /tmp/full.txt (read it with offset/limit)]")
    assert len(text.encode("utf-8")) < 21 * 1024
    assert converted.content[1].model_dump(exclude_none=True) == image
    assert converted.details == {"server": "docs", "tool": "snapshot", "fullOutputPath": "/tmp/full.txt"}
    assert saved == [full]
    assert converted.structuredContent == result

    # Text within the limit is not saved.
    await convertMcpResult("docs", "small", {"content": [{"type": "text", "text": "ok"}]}, {"saveOutput": saveOutput})
    assert len(saved) == 1


def test_cuts_multi_byte_text_only_at_character_boundaries():
    text = f"{'é' * 20_000}end"
    result = truncateMiddle(text, 1001)
    assert result.truncated is True
    assert "\ufffd" not in result.content
    assert result.content.endswith("end")
    head, tail = re.split(r"…\d+ chars truncated…", result.content)
    assert len(head.encode("utf-8")) <= 500
    assert len(tail.encode("utf-8")) <= 501
    assert len(head) + len(tail) + result.removedChars == len(text)


def test_a_lone_surrogate_counts_as_the_replacement_character_it_is_written_as():
    """`Buffer.from(text, "utf-8")` writes a lone surrogate as U+FFFD (3 bytes); `str.encode` would raise."""
    result = truncateMiddle("\ud800" * 10, 12)
    assert result.truncated is True and result.totalBytes == 30


# -- MCP connections ------------------------------------------------------------------------------


class Transports:
    """In-memory servers that answer initialize, tools/list, and tools/call with "ok"."""

    def __init__(self) -> None:
        self.servers: list = []
        self.tasks: set[asyncio.Task] = set()

    def create(
        self, *, expireFirstCall: bool = False, methods: list[str] | None = None, noTools: bool = False, resources: bool = False
    ):
        pair = createInMemoryTransportPair()
        self.servers.append(pair["server"])

        def onMessage(message) -> None:
            if "id" not in message or "method" not in message:
                return
            if methods is not None:
                methods.append(message["method"])
            if message["method"] == "initialize":
                response = {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {
                        "protocolVersion": LATEST_PROTOCOL_VERSION,
                        "capabilities": {
                            **({"prompts": {}} if noTools else {"tools": {}}),
                            **({"resources": {}} if resources else {}),
                        },
                        "serverInfo": {"name": "fake", "version": "1.0.0"},
                    },
                }
            elif message["method"] == "tools/list":
                response = (
                    {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Method not found"}}
                    if noTools
                    else {"jsonrpc": "2.0", "id": message["id"], "result": {"tools": []}}
                )
            elif message["method"] == "resources/read":
                response = {"jsonrpc": "2.0", "id": message["id"], "result": {"contents": [{"uri": "docs://a", "text": "ok"}]}}
            else:
                response = {"jsonrpc": "2.0", "id": message["id"], "result": {"content": [{"type": "text", "text": "ok"}]}}
            asyncio.get_running_loop().call_soon(lambda: spawn(pair["server"].send(response), self.tasks))

        pair["server"].onMessage(onMessage)
        spawn(pair["server"].start(), self.tasks)
        if expireFirstCall:
            send = pair["client"].send

            # Simulates the HTTP transport's 404 for a session the server no longer knows.
            async def expiring(message, options=None):
                if "method" in message and message["method"] == "tools/call":
                    raise McpSessionExpiredError("gone")
                return await send(message, options)

            pair["client"].send = expiring
        return pair["client"]


@pytest.fixture
def transports():
    created = Transports()
    yield created
    for task in created.tasks:
        task.cancel()


def connect(entry, factories, log=None):
    opened = [0]

    def createTransport(_entry, _cwd, _authProvider):
        transport = factories[opened[0]]()
        opened[0] += 1
        return transport

    connection = McpServerConnection({
        "entry": entry,
        "cwd": os.getcwd(),
        "createTransport": createTransport,
        "credentials": McpOAuthCredentialStore(InMemoryAuthStorageBackend()),
        **({"log": log} if log is not None else {}),
        "onTools": lambda _connection: None,
    })
    return connection, lambda: opened[0]


STDIO_ENTRY = {"name": "fake", "config": {"command": "unused"}, "source": "test"}


async def test_starts_a_new_session_and_retries_once_when_the_session_expired(transports):
    connection, opened = connect(
        STDIO_ENTRY, [lambda: transports.create(expireFirstCall=True), lambda: transports.create()]
    )
    results = await asyncio.gather(connection.callTool("echo", {}, {}), connection.callTool("echo", {}, {}))
    assert results == [
        {"content": [{"type": "text", "text": "ok"}]},
        {"content": [{"type": "text", "text": "ok"}]},
    ]
    assert opened() == 2
    await connection.close()


@pytest.mark.skipif(sys.platform == "win32", reason="`~` expands through HOME on POSIX")
async def test_expands_tilde_in_the_command_arguments_and_cwd_of_stdio_servers(tmp_path, monkeypatch):
    homeDir = tmp_path / "home"
    (homeDir / "work").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(homeDir))
    # Answers every tool call with its working directory.
    (homeDir / "server.py").write_text(
        "import json, os, sys\n"
        "for line in sys.stdin.buffer:\n"
        "    message = json.loads(line)\n"
        "    if 'id' not in message:\n"
        "        continue\n"
        "    method = message.get('method')\n"
        "    if method == 'initialize':\n"
        "        result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'cwd', 'version': '1'}}\n"
        "    elif method == 'tools/list':\n"
        "        result = {'tools': [{'name': 'cwd', 'inputSchema': {'type': 'object'}}]}\n"
        "    else:\n"
        "        result = {'content': [{'type': 'text', 'text': os.getcwd()}]}\n"
        "    sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}) + '\\n')\n"
        "    sys.stdout.flush()\n",
        encoding="utf-8",
    )
    connection = McpServerConnection({
        "entry": {
            "name": "home",
            "config": {"command": sys.executable, "args": ["~/server.py"], "cwd": "~/work"},
            "source": "test",
        },
        "cwd": tempfile.gettempdir(),
        "createTransport": createDefaultTransport,
        "credentials": McpOAuthCredentialStore(InMemoryAuthStorageBackend()),
        "onTools": lambda _connection: None,
    })
    try:
        result = await connection.callTool("cwd", {}, {})
        assert os.path.realpath(result["content"][0]["text"]) == os.path.realpath(homeDir / "work")
    finally:
        await connection.close()


async def test_connects_to_servers_without_the_tools_capability_without_listing_tools(transports):
    methods: list[str] = []
    connection, _opened = connect(STDIO_ENTRY, [lambda: transports.create(noTools=True, methods=methods)])
    await connection.getClient()
    assert connection.state == "connected"
    assert connection.tools == []
    assert methods == ["initialize"]
    await connection.close()


async def test_a_tools_capability_that_is_an_empty_object_lists_tools(transports):
    """`{}` is truthy in JavaScript: the usual `"tools": {}` capability must list tools."""
    methods: list[str] = []
    connection, _opened = connect(STDIO_ENTRY, [lambda: transports.create(methods=methods)])
    await connection.getClient()
    assert methods == ["initialize", "tools/list"]
    await connection.close()


async def test_marks_a_dropped_connection_and_reconnects_on_the_next_call(transports):
    connection, opened = connect(STDIO_ENTRY, [lambda: transports.create(), lambda: transports.create()])
    await connection.getClient()
    await transports.servers[-1].close()
    for _ in range(50):
        if connection.state == "disconnected":
            break
        await asyncio.sleep(0)
    assert connection.state == "disconnected"
    assert connection.error == "Connection closed"
    assert await connection.callTool("echo", {}, {}) == {"content": [{"type": "text", "text": "ok"}]}
    assert connection.state == "connected"
    assert opened() == 2
    await connection.close()


HTTP_ENTRY = {"name": "fake", "config": {"url": "http://unused.invalid", "headers": {"Authorization": "x"}}, "source": "test"}


def failing(transports, status: int, message: str):
    transport = transports.create()

    async def send(_message, _options=None):
        raise McpHttpError(status, message)

    transport.send = send
    return transport


async def test_retries_http_connections_that_fail_with_a_transient_error(transports):
    connection, opened = connect(
        HTTP_ENTRY, [lambda: failing(transports, 503, "MCP HTTP request failed with status 503"), lambda: transports.create()]
    )
    await connection.getClient()
    assert connection.state == "connected"
    assert opened() == 2
    await connection.close()

    failed, failedOpened = connect(HTTP_ENTRY, [lambda: failing(transports, 400, "MCP HTTP request failed with status 400: bad")])
    with pytest.raises(RuntimeError, match="status 400: bad"):
        await failed.getClient()
    assert failed.state == "failed"
    assert failedOpened() == 1


# #10249
async def test_closes_a_connection_that_is_still_initializing_before_close_returns(transports):
    pair = createInMemoryTransportPair()
    # The server receives initialize but never answers it.
    initializing = asyncio.get_running_loop().create_future()
    pair["server"].onMessage(lambda _message: initializing.done() or initializing.set_result(None))
    spawn(pair["server"].start(), transports.tasks)
    transportClosed = False

    def onClose() -> None:
        nonlocal transportClosed
        transportClosed = True

    pair["client"].onClose(onClose)
    connection, _opened = connect(STDIO_ENTRY, [lambda: pair["client"]])
    failure = None

    async def opening() -> None:
        nonlocal failure
        try:
            await connection.getClient()
        except Exception as error:  # noqa: BLE001 - upstream `.catch((error) => { failure = error; })`
            failure = error

    spawn(opening(), transports.tasks)
    await initializing
    await connection.close()
    assert transportClosed is True
    assert "failed to connect" in str(failure)


# #10249
async def test_stops_waiting_to_retry_a_connection_when_closed(transports):
    connection, _opened = connect(HTTP_ENTRY, [lambda: failing(transports, 503, "MCP HTTP request failed with status 503")])
    failure = None

    async def opening() -> None:
        nonlocal failure
        try:
            await connection.getClient()
        except Exception as error:  # noqa: BLE001 - upstream `.catch((error) => { failure = error; })`
            failure = error

    spawn(opening(), transports.tasks)
    # The first attempt failed; the retry is 250ms away.
    await asyncio.sleep(0.01)
    await connection.close()
    assert "status 503" in str(failure)


async def test_retries_resource_reads_but_not_tool_calls_after_a_transient_http_error(transports):
    transport = transports.create()
    send = transport.send
    failedMethods: set[str] = set()

    # The first read and the first call fail with 502.
    async def flaky(message, options=None):
        method = message.get("method", "") if "method" in message else ""
        if method in ("resources/read", "tools/call") and method not in failedMethods:
            failedMethods.add(method)
            raise McpHttpError(502, "MCP HTTP request failed with status 502")
        return await send(message, options)

    transport.send = flaky
    connection, _opened = connect(STDIO_ENTRY, [lambda: transport])
    assert await connection.readResource("docs://a", {}) == {"contents": [{"uri": "docs://a", "text": "ok"}]}
    with pytest.raises(McpHttpError, match="status 502"):
        await connection.callTool("echo", {}, {})
    await connection.close()


async def test_asks_oauth_servers_that_keep_rejecting_requests_for_a_new_sign_in(transports):
    def rejecting():
        transport = transports.create()

        async def send(_message, _options=None):
            raise McpAuthRequiredError(Response(None, status=401))

        transport.send = send
        return transport

    connection, _opened = connect({"name": "fake", "config": {"url": "http://unused.invalid"}, "source": "test"}, [rejecting])
    with pytest.raises(RuntimeError, match=re.escape('MCP server "fake" requires sign-in. Run /mcp to sign in.')):
        await connection.getClient()
    assert connection.state == "needs-auth"
    await connection.close()


async def test_sends_the_provider_token_and_asks_for_the_provider_login_when_the_server_rejects_it():
    authorizations: list = []

    async def handler(request, response, _origin):
        authorizations.append(request.headers.get("authorization"))
        await readBody(request)
        response.writeHead(401).end()

    origin = await listen(handler)

    async def providerToken(provider: str) -> str | None:
        return "tok" if provider == "radius" else None

    connection = McpServerConnection({
        "entry": {"name": "radius", "config": {"url": f"{origin}/mcp", "auth": {"provider": "radius"}}, "source": "test"},
        "cwd": os.getcwd(),
        "createTransport": createDefaultTransport,
        "credentials": McpOAuthCredentialStore(InMemoryAuthStorageBackend()),
        "providerToken": providerToken,
        "onTools": lambda _connection: None,
    })
    try:
        assert connection.oauthUrl is None
        with pytest.raises(RuntimeError, match=re.escape('MCP server "radius" requires sign-in. Run /login radius to sign in.')):
            await connection.getClient()
        assert connection.state == "needs-auth"
        assert authorizations == ["Bearer tok"]
    finally:
        await connection.close()


async def test_appends_server_log_messages_to_the_log_file(tmp_path, transports):
    path = tmp_path / "mcp.log"
    connection, _opened = connect(STDIO_ENTRY, [lambda: transports.create()], McpServerLog(str(path)))
    await connection.getClient()
    server = transports.servers[-1]
    await server.send({
        "jsonrpc": "2.0",
        "method": "notifications/message",
        "params": {"level": "warning", "logger": "db", "data": "slow\nquery"},
    })
    await server.send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "error", "data": {"code": 7}}})
    for _ in range(20):
        await asyncio.sleep(0)
    lines = re.sub(r"(?m)^\S+ ", "", path.read_bytes().decode("utf-8"))
    assert lines == '[fake] warning db: slow\n    query\n[fake] error {"code":7}\n'
    await connection.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows share modes")
def test_the_log_rotates_while_another_process_holds_it_open(tmp_path, monkeypatch):
    from misaka.extensions.mcp import log as mcpLog
    from misaka.utils import libuv_windows

    monkeypatch.setattr(mcpLog, "MAX_LOG_BYTES", 10)
    path = tmp_path / "mcp.log"
    path.write_bytes(b"x" * 20)
    other = libuv_windows.openAppend(str(path))
    try:
        McpServerLog(str(path)).write("fake", {"data": "after"})
    finally:
        os.close(other)

    assert (tmp_path / "mcp.log.1").read_bytes() == b"x" * 20
    assert path.read_bytes().decode("utf-8").endswith("[fake] info after\n")


async def test_resolves_the_oauth_client_secret_lazily(transports):
    connection, _opened = connect(
        {"name": "fake", "config": {"url": "http://unused.invalid", "oauth": {"clientSecret": "!exit 1"}}, "source": "test"},
        [lambda: transports.create()],
    )
    assert await connection.callTool("echo", {}, {}) == {"content": [{"type": "text", "text": "ok"}]}
    with pytest.raises(Exception, match="oauth.clientSecret"):
        connection.oauthSettings()
    await connection.close()


# #10526: listing every resource can take long (a large drive), so connecting must not wait for it.
async def test_connects_to_servers_with_resources_without_listing_them(transports):
    methods: list[str] = []
    connection, _opened = connect(STDIO_ENTRY, [lambda: transports.create(resources=True, methods=methods)])
    await connection.getClient()
    assert connection.state == "connected"
    assert connection.hasResources is True
    assert methods == ["initialize", "tools/list"]
    await connection.close()


# -- MCP servers section --------------------------------------------------------------------------


def server(name: str, description: str | None = None, exposure: str | None = None) -> dict:
    return {
        "entry": {
            "name": name,
            "config": {
                "command": "x",
                **({"description": description} if description else {}),
                **({"exposure": exposure} if exposure else {}),
            },
            "source": "test",
        },
    }


def test_lists_servers_with_how_their_tools_are_reached_and_the_first_line_of_their_description():
    section = renderServersSection([
        server("docs", "Docs search.\nMore."),
        server("later", None, "deferred"),
        server("direct", "Declared.", "direct"),
        {"entry": server("plain")["entry"], "connection": {"instructions": "From instructions."}},
    ])
    assert section is not None
    assert section.split("\n")[1:] == [
        "- mcp__docs (codemode): Docs search.",
        "- mcp__later (tool_search)",
        "- mcp__plain (codemode): From instructions.",
    ]
    assert renderServersSection([server("direct", "Declared.", "direct")]) is None


def test_shortens_descriptions_to_fit_the_size_limit():
    servers = [server(f"server{index}", "x" * 400) for index in range(40)]
    section = renderServersSection(servers) or ""
    assert len(section) <= MAX_SERVERS_SECTION_CHARS
    assert len(section.split("\n")) == 41
    assert "- mcp__server39 (codemode): x" in section


def test_leaves_out_the_last_servers_when_their_names_alone_do_not_fit():
    servers = [server(f"server-with-a-long-name-{index}", "desc") for index in range(200)]
    section = renderServersSection(servers) or ""
    assert len(section) <= MAX_SERVERS_SECTION_CHARS
    lines = section.split("\n")
    assert re.fullmatch(r"- … \d+ more servers; find their tools with searchTools\(\)", lines[-1])
    omitted = int(re.search(r"(\d+) more", lines[-1]).group(1))
    assert len(lines) - 2 + omitted == 200


def test_an_mcp_json_that_is_a_directory_reports_nodes_read_error(tmp_path):
    (tmp_path / "mcp.json").mkdir()
    loaded = loadMcpConfig({"agentDir": str(tmp_path), "cwd": str(tmp_path), "projectTrusted": False})
    assert loaded["errors"] == [f"{tmp_path / 'mcp.json'}: EISDIR: illegal operation on a directory, read"]


def test_servers_named_like_integers_come_first_as_in_a_javascript_object(tmp_path):
    (tmp_path / "mcp.json").write_text(
        json.dumps({"mcpServers": {"b": {"command": "b"}, "10": {"command": "ten"}, "2": {"command": "two"}}}),
        encoding="utf-8",
    )
    loaded = loadMcpConfig({"agentDir": str(tmp_path), "cwd": str(tmp_path), "projectTrusted": False})
    assert [entry["name"] for entry in loaded["servers"]] == ["2", "10", "b"]


def test_an_update_keeps_the_indentation_of_a_crlf_file(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_bytes(b'{\r\n\t"mcpServers": {\r\n\t\t"docs": {"command": "d"}\r\n\t}\r\n}\r\n')
    updateMcpServerConfig(str(path), "docs", {"enabled": False})
    assert path.read_text(encoding="utf-8") == '{\n\t"mcpServers": {\n\t\t"docs": {\n\t\t\t"command": "d",\n\t\t\t"enabled": false\n\t\t}\n\t}\n}\n'
