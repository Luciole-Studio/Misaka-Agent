"""pi 1.0.0 -> 1.1.0 kernel follow-ups (plan docs/plans/pi-0.99-port-2026-09-30.md section 7.4, U5).

Upstream cases, translated: agent-session-bash-persistence (escape sequences split across chunks), default-tools-setting
(+name/-name tool options), regressions/5109-exclude-tools (patterns), system-prompt (hidden tools),
extensions-runner (tool renderer resolution), regressions/6363-agent-settled-event (`aborted`).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from misaka.agent.types import AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.settings_manager import DEFAULT_TOOL_NAMES
from misaka.core.system_prompt import buildSystemPrompt
from misaka.utils.values import read_field
from tests.test_tool_search import _assistant, _scripted, session_with


def _write_settings(session, settings: dict) -> None:
    session.settingsManager.storage.global_value = json.dumps(
        {"compaction": {"enabled": False}, "retry": {"enabled": False}, **settings}
    )


async def _ok(*_args):
    return AgentToolResult(content=[TextContent(text="ok")], details={})


def _tool(name: str, **fields) -> ToolDefinition:
    return ToolDefinition(
        name=name, label=name, description=f"{name} test tool", parameters={"type": "object", "properties": {}},
        execute=_ok, **fields,
    )


# --- agent-session-bash-persistence: escape sequences split across output chunks ------------------


async def _runChunks(tmp_path, chunks, beforeExit=None):
    deltas: list[str] = []

    class Operations:
        async def exec(self, _command, _cwd, options):
            for chunk in chunks:
                options["onData"](chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
            if beforeExit is not None:
                beforeExit("".join(deltas))
            return {"exitCode": 0}

    async with session_with(tmp_path, []) as session:
        result = await session.executeBash("custom", deltas.append, {"operations": Operations()})
        recorded = session.messages[-1]
        return {
            "output": result.output,
            "streamed": "".join(deltas),
            "recorded": recorded.output if getattr(recorded, "role", None) == "bashExecution" else None,
        }


async def test_strips_a_color_reset_split_inside_its_parameters(tmp_path):
    result = await _runChunks(tmp_path, ["\x1b[31mERROR: file.py:1\x1b[0", "m\n"])
    assert result["output"] == "ERROR: file.py:1\n"
    assert result["streamed"] == "ERROR: file.py:1\n"
    assert result["recorded"] == "ERROR: file.py:1\n"


async def test_strips_a_color_code_split_right_after_esc(tmp_path):
    result = await _runChunks(tmp_path, ["before\x1b", "[32mafter\n"])
    assert result["output"] == "beforeafter\n"
    assert result["streamed"] == "beforeafter\n"


async def test_strips_an_osc_sequence_split_before_its_terminator(tmp_path):
    result = await _runChunks(tmp_path, ["a\x1b]0;window ", "title\x1b", "\\b\n"])
    assert result["output"] == "ab\n"


async def test_flushes_an_incomplete_multi_byte_character_at_the_end_of_output(tmp_path):
    result = await _runChunks(tmp_path, ["ok", "\u00e9".encode()[:1]])
    assert result["output"] == "ok\ufffd"
    assert result["streamed"] == "ok\ufffd"


async def test_does_not_hold_back_output_behind_a_long_unterminated_sequence(tmp_path):
    long = "x" * 300
    seen: dict[str, str] = {}
    await _runChunks(tmp_path, [f"\x1b]{long}"], lambda streamed: seen.setdefault("before", streamed))
    assert seen["before"] == f"]{long}"


# --- default-tools-setting: +name/-name tool options ----------------------------------------------


def _inactive_and_active_tools(pi) -> None:
    pi.registerTool(_tool("inactive_tool", defaultActive=False))
    pi.registerTool(_tool("active_tool"))


async def test_applies_name_tool_options_to_the_default_selection(tmp_path):
    # MISAKA fork: the default selection is misaka's eight tools (`DEFAULT_TOOL_NAMES`), where pi has four.
    async with session_with(
        tmp_path, [_inactive_and_active_tools], settings={"defaultTools": ["+grep"]}, tools=["+inactive_tool", "-write"]
    ) as session:
        expected = [name for name in DEFAULT_TOOL_NAMES if name != "write"]
        assert sorted(session.getActiveToolNames()) == sorted([*expected, "active_tool", "inactive_tool"])

    async with session_with(
        tmp_path, [_inactive_and_active_tools], settings={"defaultTools": ["read"]}, noTools="all", tools=["+inactive_tool"]
    ) as toolLess:
        assert toolLess.getActiveToolNames() == ["inactive_tool"]


async def test_rejects_invalid_tool_modifier_options(tmp_path):
    with pytest.raises(ValueError, match=r"Invalid tools option: tool names cannot be mixed with \+name or -name entries"):
        async with session_with(tmp_path, [], tools=["read", "+grep"]):
            pass
    with pytest.raises(
        ValueError, match=r"Invalid tools option: \+name and -name entries take exact tool names, not patterns: -gr\*"
    ):
        async with session_with(tmp_path, [], tools=["-gr*"]):
            pass


def _inactive_tool(pi) -> None:
    pi.registerTool(_tool("inactive_tool", defaultActive=False))


async def test_keeps_tools_removed_by_name_tool_options_removed_on_reload(tmp_path):
    async with session_with(tmp_path, [_inactive_tool], settings={"defaultTools": ["read"]}, tools=["-bash", "+grep"]) as session:
        assert session.getActiveToolNames() == ["read", "grep"]

        _write_settings(session, {"defaultTools": ["read", "bash", "inactive_tool"]})
        await session.reload()
        assert sorted(session.getActiveToolNames()) == ["grep", "inactive_tool", "read"]


# --- regressions/5109-exclude-tools: patterns --------------------------------------------------------


def _ask_and_dynamic_tools(pi) -> None:
    pi.registerTool(_tool("ask_question", promptSnippet="Ask a question"))
    pi.registerTool(_tool("dynamic_tool", promptSnippet="Run dynamic test behavior"))


async def test_matches_allowlist_and_denylist_patterns(tmp_path):
    async with session_with(
        tmp_path, [_ask_and_dynamic_tools], tools=["*_tool", "ask_*", "re*"], excludeTools=["ask*"]
    ) as session:
        # Patterns that match a tool activate it like its name.
        assert sorted(info.name for info in session.getAllTools()) == ["dynamic_tool", "read"]
        assert sorted(session.getActiveToolNames()) == ["dynamic_tool", "read"]


# --- system-prompt: hidden tools -------------------------------------------------------------------


def test_leaves_hidden_tools_out_of_the_tool_list_and_rules():
    prompt = buildSystemPrompt(
        {
            "selectedTools": ["read", "bash", "run"],
            "hiddenTools": ["read", "bash"],
            "toolSnippets": {"read": "Read files", "bash": "Run commands", "run": "Run a task"},
            "toolGuidelines": {"read": ["Use read for files."], "run": ["Prefer run."]},
            "contextFiles": [],
            "cwd": "/fixture",
        }
    )

    assert "<tools>\n- run: Run a task\n" in prompt
    assert "- read: " not in prompt
    assert "Use read for files." not in prompt
    assert "Use bash for file operations" not in prompt
    assert "- Prefer run." in prompt


# --- extensions-runner: tool renderer resolution --------------------------------------------------


async def test_resolves_tool_renderers_in_extension_load_order_each_able_to_defer_to_the_next(tmp_path):
    from misaka.core.extensions.runner import ExtensionRunner
    from misaka.core.resource_loader import DefaultResourceLoader
    from misaka.core.session_manager import SessionManager

    def renderCall(*_args):
        return None

    def first(pi) -> None:
        pi.registerToolRenderer(lambda toolName, next: {"renderCall": renderCall} if toolName == "a" else next())

    def second(pi) -> None:
        pi.registerToolRenderer(lambda _toolName, next: next() or {"renderShell": "self"})

    loader = DefaultResourceLoader({"cwd": str(tmp_path), "agentDir": str(tmp_path / "agent"), "extensionFactories": [first, second]})
    await loader.reload()
    result = loader.getExtensions()
    runner = ExtensionRunner(result.extensions, result.runtime, str(tmp_path), SessionManager.inMemory(str(tmp_path)), None)

    assert runner.resolveToolRenderers("a", lambda: None) == {"renderCall": renderCall}
    assert runner.resolveToolRenderers("b", lambda: None) == {"renderShell": "self"}
    assert runner.resolveToolRenderers("b", lambda: {"renderCall": renderCall}) == {"renderCall": renderCall}


# --- regressions/6363-agent-settled-event: `aborted` ----------------------------------------------


async def test_agent_settled_reports_whether_the_run_was_aborted(tmp_path):
    from misaka.ai.types import ErrorEvent
    from misaka.ai.utils.event_stream import AssistantMessageEventStream

    async with session_with(tmp_path, []) as session:
        settled: list[dict] = []
        session.subscribe(lambda event: settled.append(dict(event)) if read_field(event, "type") == "agent_settled" else None)

        session.agent.streamFunction = _scripted([_assistant([TextContent(text="done")])])
        await session.prompt("hello")
        assert settled == [{"type": "agent_settled", "aborted": False}]

        started = asyncio.Event()

        def waitForAbort(model, _context, options=None):
            output = AssistantMessageEventStream()
            message = _assistant([TextContent(text="")]).model_copy(
                update={"api": model.api, "provider": model.provider, "model": model.id}
            )

            async def run():
                started.set()
                await options.signal.wait()
                aborted = message.model_copy(update={"stopReason": "aborted", "errorMessage": "aborted"})
                output.push(ErrorEvent(reason="aborted", error=aborted))
                output.end()

            asyncio.ensure_future(run())
            return output

        session.agent.streamFunction = waitForAbort
        running = asyncio.ensure_future(session.prompt("again"))
        await started.wait()
        await session.abort()
        await running
        assert settled[-1] == {"type": "agent_settled", "aborted": True}



# --- U6: args.test.ts ------------------------------------------------------------------------------


def test_ignores_empty_entries_in_models():
    from misaka.cli.args import parseArgs

    assert parseArgs(["--models", "gpt-4o, ,claude-sonnet,"]).models == ["gpt-4o", "claude-sonnet"]


def test_parses_no_mcp_flag():
    from misaka.cli.args import parseArgs

    result = parseArgs(["--no-mcp"])
    assert result.noMcp is True
    assert len(result.unknownFlags) == 0


def test_parses_name_tool_modifiers():
    from misaka.cli.args import parseArgs

    result = parseArgs(["-t", "+codemode,-write"])
    assert result.tools == ["+codemode", "-write"]
    assert result.diagnostics == []


def test_rejects_tool_names_mixed_with_modifiers():
    from misaka.cli.args import ArgDiagnostic, parseArgs

    result = parseArgs(["--tools", "read,+codemode"])
    assert result.tools is None
    assert result.diagnostics == [
        ArgDiagnostic(type="error", message="--tools: tool names cannot be mixed with +name or -name entries")
    ]


def test_rejects_patterns_in_tool_modifiers():
    from misaka.cli.args import ArgDiagnostic, parseArgs

    result = parseArgs(["-t", "+mcp__radius__*"])
    assert result.tools is None
    assert result.diagnostics == [
        ArgDiagnostic(
            type="error", message="-t: +name and -name entries take exact tool names, not patterns: +mcp__radius__*"
        )
    ]


# --- U6: tools.test.ts (read structuredContent, #10251) --------------------------------------------


async def test_read_returns_the_text_as_structured_content(tmp_path):
    from misaka.core.tools.read import createReadToolDefinition

    test_file = tmp_path / "test.txt"
    content = "Hello, world!\nLine 2\nLine 3"
    test_file.write_bytes(content.encode("utf-8"))

    result = await createReadToolDefinition(str(tmp_path)).execute("test-call-1", {"path": str(test_file)})

    assert result.content[0].text == content
    assert result.structuredContent == content


async def test_read_returns_an_image_block_as_structured_content(tmp_path):
    import base64

    from misaka.core.tools.read import createReadToolDefinition

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jE1sAAAAASUVORK5CYII="
    )
    image_file = tmp_path / "image.txt"  # the MIME type comes from the file's magic, not its extension
    image_file.write_bytes(png)

    result = await createReadToolDefinition(str(tmp_path)).execute("test-call-2", {"path": str(image_file)})

    text = "".join(block.text for block in result.content if block.type == "text")
    image = next(block for block in result.content if block.type == "image")
    assert image.mimeType == "image/png"
    assert result.structuredContent == {"type": "image", "data": image.data, "mimeType": "image/png", "note": text}


# --- U6: syntax-highlight.test.ts (#10143) ----------------------------------------------------------


def test_colors_each_line_of_python_docstrings_independently():
    from misaka.modes.interactive.theme.theme import (
        getMarkdownTheme,
        highlightCode,
        theme,
    )

    code = '"""\nline one\n\nline two\n"""\nafter'
    ansi = theme.getFgAnsi("syntaxString")
    expected = [
        f'{ansi}"""\x1b[39m',
        f"{ansi}line one\x1b[39m",
        "",
        f"{ansi}line two\x1b[39m",
        f'{ansi}"""\x1b[39m',
        "after",
    ]

    assert highlightCode(code, "python") == expected
    assert getMarkdownTheme().highlightCode(code, "python") == expected


# --- U6: config.test.ts (#10439) -----------------------------------------------------------------


def test_detect_install_change_reports_a_removed_install_instead_of_reading_metadata_further_up(tmp_path):
    from misaka.config.engine import detectInstallChange

    install_dir = tmp_path / "global" / "hash"
    install_dir.mkdir(parents=True)
    (tmp_path / "METADATA").write_bytes(b"Metadata-Version: 2.4\nName: misaka\nVersion: 0.0.1\n")
    install_dir.rmdir()

    assert detectInstallChange(install_dir / "METADATA") == {"kind": "removed"}


def test_detect_install_change_reports_an_updated_install(tmp_path):
    from misaka.config.engine import VERSION, detectInstallChange

    metadata = tmp_path / "METADATA"
    metadata.write_bytes(b"Metadata-Version: 2.4\nName: misaka\nVersion: 99.0.0\n")
    assert detectInstallChange(metadata) == {"kind": "updated", "version": "99.0.0"}
    metadata.write_bytes(f"Metadata-Version: 2.4\nName: misaka\nVersion: {VERSION}\n".encode())
    assert detectInstallChange(metadata) is None


def test_offline_is_a_flag_and_an_environment_variable(monkeypatch):
    from misaka.cli import app
    from misaka.cli.args import parseArgs

    assert parseArgs(["--offline"]).offline is True
    monkeypatch.delenv("MISAKA_OFFLINE", raising=False)
    seen: dict = {}
    monkeypatch.setattr(app, "_parser", lambda *_a: (_ for _ in ()).throw(SystemExit(0)))
    monkeypatch.setattr(app.bootstrap, "install", lambda **_k: seen.update(argv=True))
    try:
        app.main(["--offline", "--", "--offline"])
    except SystemExit:
        pass
    import os

    assert os.environ.get("MISAKA_OFFLINE") == "1"


async def test_a_radius_login_offers_the_radius_mcp_server(tmp_path, monkeypatch):
    import json
    import types

    import misaka.modes.interactive.interactive_mode as interactiveMode
    from misaka.config import home
    from misaka.core.radius import RADIUS_MCP_URL

    created: list = []
    reloads: list = []

    async def handleReloadCommand():
        reloads.append(True)

    fake = types.SimpleNamespace(
        sessionManager=types.SimpleNamespace(getCwd=lambda: str(tmp_path)),
        showSelector=created.append,
        showError=lambda message: (_ for _ in ()).throw(AssertionError(message)),
        handleReloadCommand=handleReloadCommand,
        _backgroundTasks=set(),
        ui=types.SimpleNamespace(requestRender=lambda: None),
    )
    interactiveMode.InteractiveMode.offerRadiusMcpServer(fake, "radius", "Radius")
    selector = created[0](lambda: None)["component"]
    selector.onSelectCallback("Yes")
    await asyncio.sleep(0)

    saved = json.loads(home.path("mcp_config").read_text(encoding="utf-8"))
    assert saved["mcpServers"]["radius"] == {"url": RADIUS_MCP_URL, "auth": {"provider": "radius"}}
    assert reloads == [True]

    created.clear()
    interactiveMode.InteractiveMode.offerRadiusMcpServer(fake, "radius", "Radius")
    assert created == []
