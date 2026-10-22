"""Translated from pi's ``coding-agent/test/tool-execution-component.test.ts`` (v1.1.0).

MISAKA forks: "expands a collapsed tool result when clicked" is not ported (D1); the two "Pi documentation" read
scenarios are not ported (MISAKA ships no pi documentation, see ``core/tools/renderers/read.py``); ``vi.useFakeTimers``
is a patched clock in ``core/tools/render_utils.py`` (the tool timer the bash renderer uses since pi 1.2);
``{ ...definition }`` is ``dataclasses.replace``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import re
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.agent.types import AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools import render_utils
from misaka.core.tools.bash import createBashToolDefinition
from misaka.core.tools.read import createReadTool, createReadToolDefinition
from misaka.core.tools.renderers import withBuiltInRenderers
from misaka.core.tools.write import createWriteToolDefinition
from misaka.modes.interactive.components.tool_execution import ToolExecutionComponent
from misaka.modes.interactive.theme import theme as theme_module
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import Text
from misaka.ui.tui.components import image as image_module
from misaka.ui.tui.terminal_image import (
    TerminalCapabilities,
    resetCapabilitiesCache,
    setCapabilities,
)
from misaka.utils import image_convert
from misaka.utils.ansi import stripAnsi


async def _ok(*_args: Any) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text="ok")], details={})


def createBaseToolDefinition(name: str = "custom_tool") -> ToolDefinition[Any, Any]:
    return ToolDefinition(name=name, label=name, description="custom tool", parameters={}, execute=_ok)


# Small 2x2 blue JPEG image
TINY_JPEG = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMCAgMDAwMEAwMEBQgFBQQEBQoHBwYIDAoMDAsKCwsNDhIQDQ4RDgsLEBYQERMUFRUVDA8XGBYUGBIU"
    "FRT/2wBDAQMEBAUEBQkFBQkUDQsNFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBT/wAARCAACAAIDAREAAhEB"
    "AxEB/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAVAQEBAAAAAAAAAAAAAAAAAAAGCf/EABQRAQAAAAAAAAAAAAAA"
    "AAAAAAD/2gAMAwEAAhEDEQA/AD3VTB3/2Q=="
)


def createFakeTui() -> Any:
    return SimpleNamespace(requestRender=lambda: None)


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    initTheme("dark")
    # MISAKA: the PNG transcoder registration is process state; each test starts without it.
    monkeypatch.setattr(image_convert, "_png_transcoder_load", None)
    monkeypatch.setattr(image_convert, "_png_transcoder_registered", False)
    monkeypatch.setattr(image_module, "imageTranscoder", None)
    yield
    resetCapabilitiesCache()


class FakeClock:
    """MISAKA fork: ``vi.useFakeTimers()`` + ``vi.setSystemTime(0)`` for the bash renderer's clock."""

    def __init__(self, monkeypatch: Any) -> None:
        self.now = 0
        monkeypatch.setattr(render_utils, "_now", lambda: self.now)

    def advanceTimersByTime(self, ms: int) -> None:
        self.now += ms


# Issue #10292: the component loads the PNG transcoder itself, so this works in any TUI host.
# Issue #8577: a replaced partial image must not resurface.
async def test_converts_non_png_tool_images_once_the_transcoder_loads():
    setCapabilities(TerminalCapabilities(images="kitty", trueColor=True, hyperlinks=True))
    component = ToolExecutionComponent("tool", "id", {}, {}, None, createFakeTui(), os.getcwd())
    component.updateResult(
        {"content": [{"type": "image", "data": "cGFydGlhbA==", "mimeType": "image/jpeg"}], "isError": False},
        True,
    )
    component.updateResult({"content": [{"type": "image", "data": TINY_JPEG, "mimeType": "image/jpeg"}], "isError": False})

    for _ in range(200):
        if ";iVBORw0KGgo" in "\n".join(component.render(120)):
            break
        await asyncio.sleep(0.01)
    assert ";iVBORw0KGgo" in "\n".join(component.render(120))
    rendered = "\n".join(component.render(120))
    assert "cGFydGlhbA==" not in rendered

    # Invalidation reuses the converted Image, so the Kitty image ID stays the same.
    component.invalidate()
    assert "\n".join(component.render(120)) == rendered


def test_stacks_custom_call_and_result_renderers_like_the_old_implementation():
    toolDefinition = dataclasses.replace(
        createBaseToolDefinition(),
        renderCall=lambda *_args: Text("custom call", 0, 0),
        renderResult=lambda *_args: Text("custom result", 0, 0),
    )

    component = ToolExecutionComponent("custom_tool", "tool-1", {}, {}, toolDefinition, createFakeTui(), os.getcwd())
    assert "custom call" in stripAnsi("\n".join(component.render(120)))

    component.updateResult({"content": [{"type": "text", "text": "done"}], "details": {}, "isError": False}, False)

    rendered = stripAnsi("\n".join(component.render(120)))
    assert "custom call" in rendered
    assert "custom result" in rendered


def test_self_rendered_empty_tool_rows_take_no_layout_space():
    toolDefinition = dataclasses.replace(
        createBaseToolDefinition(),
        renderShell="self",
        renderCall=lambda *_args: Text("", 0, 0),
        renderResult=lambda *_args: Text("", 0, 0),
    )

    component = ToolExecutionComponent(
        "custom_tool", "tool-empty-self-render", {}, {}, toolDefinition, createFakeTui(), os.getcwd()
    )
    assert component.render(120) == []

    component.updateResult({"content": [], "details": {}, "isError": False}, False)

    assert component.render(120) == []


def test_uses_built_in_rendering_for_built_in_overrides_without_custom_renderers():
    overrideDefinition = createBaseToolDefinition("edit")

    component = ToolExecutionComponent(
        "edit",
        "tool-2",
        {"path": "README.md", "oldText": "before", "newText": "after"},
        {},
        withBuiltInRenderers("edit", overrideDefinition),
        createFakeTui(),
        os.getcwd(),
    )
    component.updateResult({"content": [], "details": {"diff": "+1 after", "firstChangedLine": 1}, "isError": False})
    rendered = stripAnsi("\n".join(component.render(120)))
    assert "edit" in rendered
    assert "README.md" in rendered
    assert ":1" not in rendered


def test_preserves_legacy_file_path_rendering_compatibility_for_built_in_tools():
    component = ToolExecutionComponent("read", "tool-3", {"file_path": "README.md"}, {}, None, createFakeTui(), os.getcwd())
    rendered = stripAnsi("\n".join(component.render(120)))
    assert "read" in rendered
    assert "README.md" in rendered


async def test_bash_execute_emits_an_initial_empty_partial_update_before_output_arrives():
    updates: list[Any] = []

    class Operations:
        async def exec(self, _command: str, _cwd: str, _options: Any) -> dict[str, int | None]:
            await asyncio.sleep(0.01)
            return {"exitCode": 0}

    tool = createBashToolDefinition(os.getcwd(), {"operations": Operations(), "exposeSessionEnvironment": False})
    task = asyncio.ensure_future(
        tool.execute("tool-bash-1", {"command": "sleep 10"}, None, lambda update: updates.append(update), None)
    )
    await asyncio.sleep(0)
    assert [(list(update.content), update.details) for update in updates] == [([], None)]
    await task


async def test_bash_renderer_does_not_duplicate_final_full_output_truncation_details():
    class Operations:
        async def exec(self, _command: str, _cwd: str, options: Any) -> dict[str, int | None]:
            for i in range(1, 4001):
                options["onData"](f"line-{str(i).rjust(4, '0')}\n".encode())
            return {"exitCode": 0}

    tool = createBashToolDefinition(os.getcwd(), {"operations": Operations(), "exposeSessionEnvironment": False})
    result = await tool.execute("tool-bash-1b", {"command": "generate output"}, None, None, None)
    component = ToolExecutionComponent(
        "bash", "tool-bash-1b", {"command": "generate output"}, {}, tool, createFakeTui(), os.getcwd()
    )
    component.setExpanded(True)
    component.updateResult(
        {"content": result.content, "details": result.details, "isError": False},
        False,
    )

    rendered = stripAnsi("\n".join(component.render(200)))
    assert len(re.findall(r"Full output:", rendered)) == 1
    assert re.search(r"line-4000[^\n]*\n[^\S\n]*\n \[Full output:", rendered)
    assert not re.search(r"line-4000[^\n]*\n[^\S\n]*\n[^\S\n]*\n \[Full output:", rendered)
    assert "Truncated: showing 2000 of 4000 lines" in rendered
    assert "[Showing lines 2001-4000 of 4000. Full output:" not in rendered


# Issue #9628: keep short durations precise and make long shell durations readable.
@pytest.mark.parametrize(
    ("ms", "formatted"),
    [
        (0, "0.0s"),
        (4_200, "4.2s"),
        (59_900, "59.9s"),
        (59_999, "60.0s"),
        (60_000, "1m 0s"),
        (90_900, "1m 30s"),
        (1_592_200, "26m 32s"),
        (3_599_999, "59m 59s"),
        (3_600_000, "1h 0m 0s"),
        (7_384_900, "2h 3m 4s"),
    ],
)
def test_bash_renderer_formats_ms_while_running_and_after_completion(monkeypatch, ms, formatted):
    clock = FakeClock(monkeypatch)
    component = ToolExecutionComponent(
        "bash",
        "tool-bash-duration",
        {"command": "long-running-command"},
        {},
        createBashToolDefinition(os.getcwd(), {"exposeSessionEnvironment": False}),
        createFakeTui(),
        os.getcwd(),
    )
    component.markExecutionStarted()
    component.updateResult({"content": [], "isError": False}, True)

    clock.advanceTimersByTime(ms)
    component.invalidate()
    running = stripAnsi("\n".join(component.render(120)))

    component.updateResult({"content": [], "isError": False}, False)
    completed = stripAnsi("\n".join(component.render(120)))

    clock.advanceTimersByTime(1_000)
    component.invalidate()
    assert stripAnsi("\n".join(component.render(120))) == completed
    assert f"Elapsed {formatted}" in running
    assert f"Took {formatted}" in completed


# #10549
def test_bash_renderer_shows_a_results_recorded_duration_also_for_a_result_restored_without_a_live_start(monkeypatch):
    def render(live: bool) -> str:
        clock = FakeClock(monkeypatch)
        component = ToolExecutionComponent(
            "bash",
            "tool-bash-recorded",
            {"command": "sleep 4"},
            {},
            createBashToolDefinition(os.getcwd(), {"exposeSessionEnvironment": False}),
            createFakeTui(),
            os.getcwd(),
        )
        if live:
            component.markExecutionStarted()
            component.updateResult({"content": [], "isError": False}, True)
            # The wall clock jumps; the recorded duration does not.
            clock.advanceTimersByTime(3_600_000)
        component.updateResult({"content": [], "isError": False, "durationMs": 4_200}, False)
        return stripAnsi("\n".join(component.render(120)))

    assert "Took 4.2s" in render(True)
    assert "Took 4.2s" in render(False)


def test_does_not_duplicate_built_in_headers_when_passed_the_active_built_in_definition():
    component = ToolExecutionComponent(
        "read", "tool-4", {"path": "README.md"}, {}, createReadToolDefinition(os.getcwd()), createFakeTui(), os.getcwd()
    )
    component.updateResult({"content": [{"type": "text", "text": "hello"}], "details": None, "isError": False}, False)
    rendered = stripAnsi("\n".join(component.render(120)))
    assert len(re.findall(r"\bread\b", rendered)) == 1


# Issue #9996: strict tool schemas make models send null for omitted optional fields.
def test_renders_read_calls_with_null_offset_and_limit_as_full_file_reads():
    component = ToolExecutionComponent(
        "read",
        "tool-read-null-range",
        {"path": "src/example.ts", "offset": None, "limit": None},
        {},
        createReadToolDefinition(os.getcwd()),
        createFakeTui(),
        os.getcwd(),
    )
    rendered = stripAnsi("\n".join(component.render(120)))
    assert "read src/example.ts" in rendered
    assert "src/example.ts:" not in rendered


def test_inherits_missing_built_in_result_renderer_slot_from_the_built_in_tool():
    overrideDefinition = dataclasses.replace(
        createBaseToolDefinition("read"), renderCall=lambda *_args: Text("override call", 0, 0)
    )

    component = ToolExecutionComponent(
        "read",
        "tool-4b",
        {"path": "notes.txt"},
        {},
        withBuiltInRenderers("read", overrideDefinition),
        createFakeTui(),
        os.getcwd(),
    )
    component.updateResult({"content": [{"type": "text", "text": "hello"}], "details": None, "isError": False}, False)
    component.setExpanded(True)
    rendered = stripAnsi("\n".join(component.render(120)))
    assert "override call" in rendered
    assert "hello" in rendered


def test_inherits_missing_built_in_call_renderer_slot_from_the_built_in_tool():
    overrideDefinition = dataclasses.replace(
        createBaseToolDefinition("read"), renderResult=lambda *_args: Text("override result", 0, 0)
    )

    component = ToolExecutionComponent(
        "read",
        "tool-4c",
        {"path": "README.md"},
        {},
        withBuiltInRenderers("read", overrideDefinition),
        createFakeTui(),
        os.getcwd(),
    )
    component.updateResult({"content": [{"type": "text", "text": "hello"}], "details": None, "isError": False}, False)
    rendered = stripAnsi("\n".join(component.render(120)))
    assert "read" in rendered
    assert "README.md" in rendered
    assert "override result" in rendered


def test_uses_custom_renderers_for_built_in_overrides_that_reuse_built_in_definition_parameters():
    builtInDefinition = createReadToolDefinition(os.getcwd())
    component = ToolExecutionComponent(
        "read",
        "tool-4d",
        {"path": "README.md"},
        {},
        dataclasses.replace(
            builtInDefinition,
            renderCall=lambda *_args: Text("override call", 0, 0),
            renderResult=lambda *_args: Text("override result", 0, 0),
        ),
        createFakeTui(),
        os.getcwd(),
    )
    component.updateResult({"content": [{"type": "text", "text": "hello"}], "details": None, "isError": False}, False)
    rendered = stripAnsi("\n".join(component.render(120)))
    assert "override call" in rendered
    assert "override result" in rendered
    assert "read README.md" not in rendered


def test_uses_custom_renderers_for_built_in_overrides_that_reuse_wrapped_built_in_tool_parameters():
    builtInTool = createReadTool(os.getcwd())
    component = ToolExecutionComponent(
        "read",
        "tool-4e",
        {"path": "README.md"},
        {},
        dataclasses.replace(
            createBaseToolDefinition("read"),
            parameters=builtInTool.parameters,
            renderCall=lambda *_args: Text("wrapped override call", 0, 0),
            renderResult=lambda *_args: Text("wrapped override result", 0, 0),
        ),
        createFakeTui(),
        os.getcwd(),
    )
    component.updateResult({"content": [{"type": "text", "text": "hello"}], "details": None, "isError": False}, False)
    rendered = stripAnsi("\n".join(component.render(120)))
    assert "wrapped override call" in rendered
    assert "wrapped override result" in rendered


def test_shares_renderer_state_across_custom_call_and_result_slots():
    def renderCall(_args: Any, _theme: Any, context: Any) -> Text:
        context.state.setdefault("token", "shared-token")
        return Text(f"custom call {context.state['token']}", 0, 0)

    def renderResult(_result: Any, _options: Any, _theme: Any, context: Any) -> Text:
        return Text(f"custom result {context.state.get('token')}", 0, 0)

    toolDefinition = dataclasses.replace(createBaseToolDefinition(), renderCall=renderCall, renderResult=renderResult)

    component = ToolExecutionComponent("custom_tool", "tool-5", {}, {}, toolDefinition, createFakeTui(), os.getcwd())
    component.updateResult({"content": [{"type": "text", "text": "done"}], "details": {}, "isError": False}, False)
    rendered = stripAnsi("\n".join(component.render(120)))
    assert "custom call shared-token" in rendered
    assert "custom result shared-token" in rendered


def test_exposes_args_in_render_result_context():
    toolDefinition = dataclasses.replace(
        createBaseToolDefinition(),
        renderCall=lambda *_args: Text("call", 0, 0),
        renderResult=lambda _result, _options, _theme, context: Text(f"arg:{context.args['foo']}", 0, 0),
    )

    component = ToolExecutionComponent(
        "custom_tool", "tool-5b", {"foo": "bar"}, {}, toolDefinition, createFakeTui(), os.getcwd()
    )
    component.updateResult({"content": [{"type": "text", "text": "done"}], "details": {}, "isError": False}, False)
    rendered = stripAnsi("\n".join(component.render(120)))
    assert "arg:bar" in rendered


def test_shows_arguments_in_the_fallback_call_header():
    longValue = "x" * 200
    component = ToolExecutionComponent(
        "custom_tool",
        "tool-args",
        {"query": "pi", "long": longValue, "text": "line one\nline two"},
        {},
        createBaseToolDefinition(),
        createFakeTui(),
        os.getcwd(),
    )

    collapsed = stripAnsi("\n".join(component.render(300)))
    assert 'custom_tool query="pi" long="xxx' in collapsed
    assert "..." in collapsed
    assert longValue not in collapsed

    component.setExpanded(True)
    expanded = stripAnsi("\n".join(component.render(300)))
    assert "  query: pi" in expanded
    assert longValue in expanded
    expandedLines = [line.rstrip() for line in expanded.split("\n")]
    textLine = next((index for index, line in enumerate(expandedLines) if line.endswith("  text: line one")), -1)
    assert textLine > -1
    assert re.match(r"^\s+ {4}line two$", expandedLines[textLine + 1])


def test_collapses_fallback_results_until_expanded():
    toolDefinition = createBaseToolDefinition()

    component = ToolExecutionComponent(
        "custom_tool", "tool-6", {"foo": "bar"}, {}, toolDefinition, createFakeTui(), os.getcwd()
    )
    output = "\n".join(f"line-{index + 1}" for index in range(15))
    component.updateResult({"content": [{"type": "text", "text": output}], "details": {}, "isError": False}, False)

    collapsed = stripAnsi("\n".join(component.render(120)))
    assert "custom_tool" in collapsed
    assert "line-10" in collapsed
    assert "line-11" not in collapsed
    assert "5 more lines" in collapsed
    assert "to expand" in collapsed

    component.setExpanded(True)
    expanded = stripAnsi("\n".join(component.render(120)))
    assert "line-15" in expanded
    assert "more lines" not in expanded


def test_trims_trailing_blank_display_lines_from_write_previews():
    component = ToolExecutionComponent(
        "write",
        "tool-7",
        {"path": "README.md", "content": "one\ntwo\n"},
        {},
        createWriteToolDefinition(os.getcwd()),
        createFakeTui(),
        os.getcwd(),
    )
    rendered = stripAnsi("\n".join(component.render(120)))
    assert "one" in rendered
    assert "two" in rendered
    assert "two\n\n" not in rendered


def test_trims_trailing_blank_display_lines_from_read_results():
    component = ToolExecutionComponent(
        "read", "tool-8", {"path": "notes.txt"}, {}, createReadToolDefinition(os.getcwd()), createFakeTui(), os.getcwd()
    )
    component.updateResult(
        {"content": [{"type": "text", "text": "one\ntwo\n"}], "details": None, "isError": False},
        False,
    )
    component.setExpanded(True)
    rendered = stripAnsi("\n".join(component.render(120)))
    assert "one" in rendered
    assert "two" in rendered
    assert "two\n\n" not in rendered


def test_does_not_syntax_highlight_read_errors_based_on_the_requested_file_path():
    component = ToolExecutionComponent(
        "read",
        "tool-read-error-highlighting",
        {"path": "config.exs", "offset": 120, "limit": 130},
        {},
        createReadToolDefinition(os.getcwd()),
        createFakeTui(),
        os.getcwd(),
    )
    error = "Offset 120 is beyond end of file (96 lines total)"
    component.updateResult({"content": [{"type": "text", "text": error}], "details": None, "isError": True}, False)

    rendered = "\n".join(component.render(120))
    assert error in stripAnsi(rendered)
    assert theme_module.theme.fg("toolOutput", error) in rendered


def test_collapses_ordinary_read_results_until_expanded():
    component = ToolExecutionComponent(
        "read",
        "tool-ordinary-read-collapsed",
        {"path": "notes.txt"},
        {},
        createReadToolDefinition(os.getcwd()),
        createFakeTui(),
        os.getcwd(),
    )
    component.updateResult(
        {"content": [{"type": "text", "text": "hidden content"}], "details": None, "isError": False},
        False,
    )

    collapsed = stripAnsi("\n".join(component.render(120)))
    assert "read" in collapsed
    assert "notes.txt" in collapsed
    assert "hidden content" not in collapsed

    component.setExpanded(True)
    expanded = stripAnsi("\n".join(component.render(120)))
    assert "hidden content" in expanded


_COMPACT_SCENARIOS = [
    {
        "title": "SKILL.md",
        "path": os.path.join(os.getcwd(), "attio", "SKILL.md"),
        "content": "---\nname: attio\ndescription: CRM helper\n---\n\n# Hidden skill instructions",
        "compact": "[skill] attio",
        "hidden": "Hidden skill instructions",
        "absent": "read skill attio",
    },
    {
        "title": "AGENTS.md",
        "path": os.path.join(os.getcwd(), ".pi", "AGENTS.md"),
        "content": "Hidden resource instructions",
        "compact": "read resource .pi/AGENTS.md",
        "hidden": "Hidden resource instructions",
        "absent": None,
    },
    {
        "title": "AGENTS.override.md",
        "path": os.path.join(os.getcwd(), ".pi", "AGENTS.override.md"),
        "content": "Hidden override instructions",
        "compact": "read resource .pi/AGENTS.override.md",
        "hidden": "Hidden override instructions",
        "absent": None,
    },
    {
        "title": "outside AGENTS.md",
        "path": os.path.abspath(os.path.join(os.getcwd(), "..", "AGENTS.md")),
        "content": "Hidden outside resource instructions",
        "compact": f"read resource {os.path.abspath(os.path.join(os.getcwd(), '..', 'AGENTS.md')).replace(os.sep, '/')}",
        "hidden": "Hidden outside resource instructions",
        "absent": None,
    },
]


@pytest.mark.parametrize("scenario", _COMPACT_SCENARIOS, ids=[scenario["title"] for scenario in _COMPACT_SCENARIOS])
def test_renders_read_results_compactly_until_expanded(scenario):
    component = ToolExecutionComponent(
        "read",
        f"tool-compact-{scenario['title']}",
        {"path": scenario["path"]},
        {},
        createReadToolDefinition(os.getcwd()),
        createFakeTui(),
        os.getcwd(),
    )
    component.updateResult(
        {"content": [{"type": "text", "text": scenario["content"]}], "details": None, "isError": False},
        False,
    )

    collapsed = stripAnsi("\n".join(component.render(120)))
    assert scenario["compact"] in collapsed
    assert scenario["hidden"] not in collapsed
    if scenario["absent"]:
        assert scenario["absent"] not in collapsed

    component.setExpanded(True)
    expanded = stripAnsi("\n".join(component.render(120)))
    assert scenario["hidden"] in expanded


def test_shows_the_read_line_range_in_compact_skill_reads_before_the_expand_hint():
    component = ToolExecutionComponent(
        "read",
        "tool-compact-range-SKILL.md",
        {"path": os.path.join(os.getcwd(), "attio", "SKILL.md"), "offset": 120, "limit": 210},
        {},
        createReadToolDefinition(os.getcwd()),
        createFakeTui(),
        os.getcwd(),
    )

    collapsed = stripAnsi("\n".join(component.render(120)))
    assert "[skill] attio:120-329" in collapsed
    assert collapsed.index(":120-329") < collapsed.index("to expand")
