"""Translated from pi's ``packages/coding-agent/test/read-line-range-display.test.ts`` (v1.3.0).

MISAKA: "shows string ranges in the TUI, session tree, and HTML export" is split by surface. ``loadHtmlViewer`` runs the
exported viewer in QuickJS (``misaka.codemode.quickjs_wasi``) where upstream uses Node's ``vm.runInNewContext``, with
the same browser stand-ins (``atob``, ``TextDecoder``, ``URLSearchParams``, ``document``, ``window``) written in
JavaScript.
"""

from __future__ import annotations

import json
import os
import re
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.codemode import quickjs_wasi
from misaka.codemode.wasm import loadQuickJSWasm
from misaka.core import formatReadLineRange as exportedFormatReadLineRange
from misaka.core.export_html import exportFromFile
from misaka.core.session_manager import CURRENT_SESSION_VERSION, SessionManager
from misaka.core.tools.read import createReadToolDefinition
from misaka.core.tools.render_utils import formatReadLineRange
from misaka.modes.interactive.components.tool_execution import ToolExecutionComponent
from misaka.modes.interactive.components.tree_selector import TreeSelectorComponent
from misaka.modes.interactive.theme.theme import initTheme
from misaka.utils.ansi import stripAnsi
from misaka.utils.js import UNDEFINED

# #9887
CASES: list[tuple[Any, Any, str]] = [
    (25, 13, ":25-37"),
    ("25", "13", ":25-37"),
    (None, "13", ":1-13"),
    ("25", None, ":25"),
    (UNDEFINED, UNDEFINED, ""),
    ("abc", 13, ""),
    (25, "", ""),
    (25, "Infinity", ""),
]


@pytest.fixture(autouse=True)
def _theme():
    initTheme("dark")


@pytest.mark.parametrize(("offset", "limit", "expected"), CASES)
def test_formats_raw_read_arguments(offset, limit, expected):
    assert formatReadLineRange(offset, limit) == expected


def test_formats_raw_read_arguments_python_values():
    # MISAKA: values a Python caller can pass. A bool is not a number (JavaScript's `typeof true` is "boolean"),
    # and an integer beyond the double range is JavaScript's Infinity.
    assert formatReadLineRange(True, 3) == ""
    assert formatReadLineRange(10**400, 1) == ""
    assert formatReadLineRange(2.0, 3) == ":2-4"
    assert formatReadLineRange(1, 0) == ":1"


def test_is_exported_from_the_package():
    assert exportedFormatReadLineRange is formatReadLineRange


def test_shows_string_ranges_in_the_tui(tmp_path):
    args = {"path": "notes.txt", "offset": "25", "limit": "13"}
    tool = ToolExecutionComponent(
        "read",
        "call",
        args,
        {},
        createReadToolDefinition(str(tmp_path)),
        SimpleNamespace(requestRender=lambda: None),
        str(tmp_path),
    )
    assert "read notes.txt:25-37" in stripAnsi("\n".join(tool.render(120)))


def test_a_numeric_string_limit_no_longer_breaks_the_read_row(tmp_path):
    # MISAKA: before 1.3 the row computed `offset + limit - 1` with whatever the model sent, which raised
    # TypeError in Python for a string limit (JavaScript concatenated instead).
    tool = ToolExecutionComponent(
        "read", "call", {"path": "notes.txt", "offset": 3, "limit": "abc"}, {},
        createReadToolDefinition(str(tmp_path)), SimpleNamespace(requestRender=lambda: None), os.getcwd(),
    )
    rendered = stripAnsi("\n".join(tool.render(120)))
    assert "read notes.txt" in rendered and ":3" not in rendered


def _readSession(tmp_path) -> tuple[str, dict[str, Any], dict[str, Any]]:
    args = {"path": "notes.txt", "offset": "25", "limit": "13"}
    timestamp = "1970-01-01T00:00:00.000Z"
    assistant = {
        "type": "message",
        "id": "assistant",
        "parentId": None,
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "content": [{"type": "toolCall", "id": "call", "name": "read", "arguments": args}],
            "api": "anthropic-messages",
            "provider": "anthropic",
            "model": "test-model",
            "usage": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 0,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
            "stopReason": "toolUse",
            "timestamp": 0,
        },
    }
    result = {
        "type": "message",
        "id": "result",
        "parentId": assistant["id"],
        "timestamp": timestamp,
        "message": {
            "role": "toolResult",
            "toolCallId": "call",
            "toolName": "read",
            "content": [],
            "isError": False,
            "timestamp": 0,
        },
    }
    header = {"type": "session", "version": CURRENT_SESSION_VERSION, "id": "range-test", "timestamp": timestamp,
              "cwd": str(tmp_path)}
    sessionPath = tmp_path / "session.jsonl"
    sessionPath.write_text("".join(f"{json.dumps(entry)}\n" for entry in (header, assistant, result)), encoding="utf-8")
    return str(sessionPath), assistant, result


def test_shows_string_ranges_in_the_session_tree(tmp_path):
    sessionPath, _assistant, result = _readSession(tmp_path)
    tree = TreeSelectorComponent(SessionManager.open(sessionPath).getTree(), result["id"], 40, lambda _id: None,
                                 lambda: None)
    assert "[read: notes.txt:25-37]" in "\n".join(stripAnsi(line) for line in tree.getTreeList().render(120))


@pytest.mark.parametrize(("offset", "limit", "expected"), CASES)
def test_the_session_tree_formats_raw_read_arguments(offset, limit, expected):
    tree = TreeSelectorComponent([], None, 40, lambda _id: None, lambda: None)
    args: dict[str, Any] = {"path": "notes.txt"}
    if offset is not UNDEFINED:
        args["offset"] = offset
    if limit is not UNDEFINED:
        args["limit"] = limit
    assert tree.getTreeList().formatToolCall("read", args) == f"[read: notes.txt{expected}]"


# Browser stand-ins for the viewer's data loading; the viewer returns before it touches the DOM.
_VIEWER_GLOBALS = r"""
const BASE64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
function atob(input) {
  let bits = 0, value = 0, output = '';
  for (const char of input.replace(/=+$/, '')) {
    value = (value << 6) | BASE64.indexOf(char);
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      output += String.fromCharCode((value >> bits) & 0xff);
    }
  }
  return output;
}
class TextDecoder {
  decode(bytes) {
    let binary = '';
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return decodeURIComponent(escape(binary));
  }
}
class URLSearchParams {
  get() { return null; }
}
const window = { location: { search: '' } };
"""


def _jsString(value: str) -> str:
    return json.dumps(value)


async def loadHtmlViewer(html: str, calls: str) -> Any:
    """Run the exported viewer's real data loading and renderers, stopping before DOM initialization, then ``calls``
    (a JavaScript expression over ``viewer``); its value comes back through JSON."""
    sessionData = re.search(r'<script id="session-data" type="application/json">([^<]+)</script>', html)
    scripts = re.findall(r"<script>([\s\S]*?)</script>", html)
    if not sessionData or not scripts:
        raise AssertionError("Missing exported session data or viewer script")
    script = scripts[-1].replace(
        "marked.use({", "return { formatReadLineRange, getTreeNodeDisplayHtml, renderEntry }; marked.use({", 1
    )
    source = (
        f"{_VIEWER_GLOBALS}\n"
        f"const document = {{ getElementById: () => ({{ textContent: {_jsString(sessionData.group(1))} }}), "
        "querySelector: () => null };\n"
        f"const viewer = {script};\n"
        f"JSON.stringify({calls})"
    )
    vm = quickjs_wasi.QuickJS.create(wasm=await loadQuickJSWasm())
    try:
        handle = vm.evalCode(source, "viewer.js")
        try:
            return json.loads(handle.toString())
        finally:
            handle.dispose()
    finally:
        vm.dispose()


def _jsArgument(value: Any) -> str:
    return "undefined" if value is UNDEFINED else json.dumps(value)


async def test_shows_string_ranges_in_the_html_export(tmp_path):
    sessionPath, assistant, result = _readSession(tmp_path)
    html = (tmp_path / "out.html")
    await exportFromFile(sessionPath, str(html))
    cases = ", ".join(f"[{_jsArgument(offset)}, {_jsArgument(limit)}]" for offset, limit, _range in CASES)

    viewed = await loadHtmlViewer(html.read_text(encoding="utf-8"), (
        "{"
        f"ranges: [{cases}].map(([offset, limit]) => viewer.formatReadLineRange(offset, limit)), "
        f"tree: viewer.getTreeNodeDisplayHtml({json.dumps(result)}), "
        f"entry: viewer.renderEntry({json.dumps(assistant)})"
        "}"
    ))

    assert viewed["ranges"] == [expected for _offset, _limit, expected in CASES]
    assert viewed["tree"] == '<span class="tree-role-tool">[read: notes.txt:25-37]</span>'
    assert '<span class="line-numbers">:25-37</span>' in viewed["entry"]
