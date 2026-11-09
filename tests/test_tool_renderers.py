"""Every built-in tool's own call/result renderer runs without raising.

The TUI catches a renderer that raises and quietly falls back to the generic view, so a
broken renderer never shows up as an error anywhere: office's renderCall passed two
arguments to a three-argument helper from the day it was written, and nothing noticed.
This test is the place that notices.
"""
from __future__ import annotations

import pytest

from misaka.core.tools import createAllToolDefinitions
from misaka.core.tools.download_file import create_download_file_tool_definition
from misaka.core.tools.web_fetch import create_web_fetch_tool_definition
from misaka.modes.interactive.components.tool_execution import ToolRenderContext
from misaka.modes.interactive.theme.theme import theme

ARGS = {
    "read": {"path": "notes.md", "offset": 3, "limit": 10},
    "bash": {"command": "ls -la", "timeout": 5},
    "powershell": {"command": "Get-ChildItem"},
    "edit": {"path": "notes.md", "edits": [{"oldText": "a", "newText": "b"}]},
    "write": {"path": "notes.md", "content": "# Title\n\nbody\n"},
    "office": {"path": "report.docx", "ops": [{"create": {"blocks": []}}]},
    "grep": {"pattern": "needle", "path": "."},
    "find": {"pattern": "*.md"},
    "ls": {"path": "."},
    "web_fetch": {"url": "https://example.com/page"},
    "download_file": {"url": "https://example.com/paper.pdf"},
}


def _definitions(cwd):
    definitions = dict(createAllToolDefinitions(cwd))
    definitions["web_fetch"] = create_web_fetch_tool_definition(cwd)
    definitions["download_file"] = create_download_file_tool_definition(cwd)
    return definitions


def _context(args, cwd, *, partial, expanded, error=False):
    return ToolRenderContext(
        args=args, toolCallId="call_1", invalidate=lambda: None, lastComponent=None, state={},
        cwd=cwd, executionStarted=True, argsComplete=True, isPartial=partial, expanded=expanded,
        showImages=False, isError=error, durationMs=None, outputPad=1,
    )


def test_every_built_in_tool_is_covered(tmp_path):
    assert set(_definitions(str(tmp_path))) <= set(ARGS)


@pytest.mark.parametrize("name", sorted(ARGS))
@pytest.mark.parametrize("partial,expanded", [(True, False), (False, False), (False, True)])
def test_renderers_do_not_raise(tmp_path, name, partial, expanded):
    definition = _definitions(str(tmp_path)).get(name)
    if definition is None:
        pytest.skip(f"{name} is not built in on this platform")
    args = ARGS[name]
    render_call = definition.renderCall
    if callable(render_call):
        component = render_call(args, theme, _context(args, str(tmp_path), partial=partial, expanded=expanded))
        assert component is not None
        component.render(80)
    render_result = definition.renderResult
    if callable(render_result):
        for error in (False, True):
            result = {"content": [{"type": "text", "text": "line one\nline two"}], "details": None}
            component = render_result(result, {"expanded": expanded, "isPartial": partial}, theme,
                                      _context(args, str(tmp_path), partial=partial, expanded=expanded, error=error))
            if component is not None:
                component.render(80)
