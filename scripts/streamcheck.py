#!/usr/bin/env python3
"""Fail when a line-based asyncio stream has no explicit limit, or a UI timer thread
never returns to the event loop.

Two defects the audit found repeatedly, both of which a linter cannot see and both of
which already have a correct implementation somewhere in this repo:

1. ``asyncio``'s default ``limit`` is 64 KiB. ``StreamReader.readline()`` on a longer
   line raises ``ValueError`` and the stream can never resynchronise -- the tail of the
   over-long line is still arriving. ``extensions/mcp.py`` is the model: it passes an
   explicit ``limit=`` and treats the ``ValueError`` as "this stream is finished",
   reporting a readable reason. Two P1s (grep dying on a minified file, the panel daemon
   dropping its own panel connection and killing every pane) were the same omission.

2. Node's ``setTimeout``/``setInterval``/``fs.watch`` callbacks run on the JS event loop.
   Ported to ``threading.Timer``/``Thread``, they run somewhere else, so a callback that
   touches component state or wakes a coroutine must hand back via ``call_soon_threadsafe``
   or ``call_later``. ``ui/tui/terminal.py`` states this rule in its own module docstring;
   the interactive components did not follow it (a 1.0s dialog timeout was observed firing
   at 6.0s).

Both lists below are allowlists: an entry is a site that is deliberately exempt, with the
reason. Adding a site is a decision, which is the point -- the next stream or timer should
have to argue for itself.

Usage: ``python scripts/streamcheck.py`` (exit 1 and a list on failure).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = (
    "misaka/documents/pageindex",
    "misaka/extensions/hermes_lcm/vendor",
    "tests/hermes_lcm_vendor",
)

# Stream constructors whose reader is consumed line by line.
STREAM_FACTORIES = {
    "create_subprocess_exec",
    "create_subprocess_shell",
    "start_server",
    "start_unix_server",
    "open_connection",
    "open_unix_connection",
}

# Every line-framed stream site in the tree, with its verdict. A site is listed either
# because it is fine as written, or because a named finding will fix it -- delete the
# entry when that happens. A site NOT in this list is new and must argue for itself.
STREAM_ALLOWED = {
    # Reads to EOF via communicate() / a whole-stream read: no line framing, so the cap
    # cannot bite even though the module calls readline() elsewhere.
    ("misaka/core/tools/find.py", "create_subprocess_exec"): "communicate(), not line-framed",
    ("misaka/ui/tui/autocomplete.py", "create_subprocess_exec"): "whole-stream read",
    ("misaka/ui/tui/interactive/interactive_mode.py", "create_subprocess_exec"): "communicate() with timeout",
    ("misaka/ui/tui/interactive/components/extension_editor.py", "create_subprocess_exec"): "editor exit status only",
    ("misaka/extensions/sisters/subagent/hooks.py", "create_subprocess_exec"): "communicate()",
    ("misaka/extensions/sisters/subagent/hooks.py", "create_subprocess_shell"): "communicate()",
    ("misaka/extensions/web/backends/ddgs.py", "create_subprocess_exec"): "communicate()",
    ("misaka/extensions/sisters/subagent/runtime.py", "create_subprocess_exec"): "git helper, communicate()",
    # PENDING -- audit findings that will remove these entries:
    ("misaka/core/tools/grep.py", "create_subprocess_exec"): "PENDING core-tools-03 (P1)",
    ("misaka/ui/panel/daemon.py", "start_unix_server"): "PENDING ui-panel-01 (P1)",
    ("misaka/ai/utils/oauth/anthropic.py", "start_server"): "PENDING cross-cutting-01 (OAuth callback)",
    ("misaka/ai/utils/oauth/openai_codex.py", "start_server"): "PENDING cross-cutting-01 (OAuth callback)",
    ("misaka/ai/utils/oauth/openrouter.py", "start_server"): "PENDING cross-cutting-01 (OAuth callback)",
    ("misaka/ai/utils/oauth/radius.py", "start_server"): "PENDING cross-cutting-01 (OAuth callback)",
}

# Every threading.Timer / Thread site under misaka/ui, with its verdict. Deciding whether
# a callback may stay off-loop needs the callback's body and its callees, which a static
# pass cannot follow reliably; the audit judged these one by one, so the list -- not a
# heuristic -- is the record. A new site must be judged and added.
THREAD_ALLOWED = {
    ("misaka/ui/tui/stdin_buffer.py", 231): "hands off via call_soon_threadsafe at :281",
    ("misaka/ui/tui/terminal.py", 274): "hands off via call_soon_threadsafe at :607",
    ("misaka/ui/tui/terminal.py", 476): "progress keepalive writes bytes only, touches no component",
    ("misaka/ui/tui/tui.py", 479): "render tick re-enters through the loop's scheduled callback",
    ("misaka/ui/tui/tui.py", 526): "render scheduling, same path as :479",
    ("misaka/ui/tui/components/loader.py", 85): "spinner frame advance, no coroutine wake",
    ("misaka/ui/tui/interactive/interactive_mode.py", 1770): "no-op keep-alive reference",
    # PENDING -- audit findings that will remove these entries:
    ("misaka/ui/tui/interactive/components/countdown_timer.py", 26): "PENDING ui-interactive-components-07",
    ("misaka/ui/tui/interactive/components/tree_selector.py", 1081): "PENDING cross-cutting-03",
    ("misaka/ui/tui/interactive/components/user_message_selector.py", 110): "PENDING cross-cutting-03",
    ("misaka/ui/tui/interactive/theme/theme.py", 923): "PENDING ui-interactive-components-22",
}


def _iter_files():
    for path in sorted((ROOT / "misaka").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if any(rel.startswith(d) for d in SKIP_DIRS):
            continue
        yield path, rel


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _uses_readline(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) == "readline":
            return True
    return False


def main() -> int:
    stream_problems: list[str] = []
    thread_problems: list[str] = []

    for path, rel in _iter_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue

        line_framed = _uses_readline(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)

            if (
                name in STREAM_FACTORIES
                and line_framed
                and (rel, name) not in STREAM_ALLOWED
                and not any(kw.arg == "limit" for kw in node.keywords)
            ):
                stream_problems.append(
                        f"  {rel}:{node.lineno} {name}() has no explicit limit= "
                        f"while this module reads the stream with readline()"
                    )

            if (
                rel.startswith("misaka/ui/")
                and name in {"Timer", "Thread"}
                and (rel, node.lineno) not in THREAD_ALLOWED
            ):
                thread_problems.append(
                    f"  {rel}:{node.lineno} {name}(...) is a new off-loop timer/thread"
                )

    if stream_problems:
        print("streamcheck: line-based asyncio stream(s) without an explicit limit=:")
        print("\n".join(stream_problems))
        print(
            "Pass limit= (see misaka/utils/streams.py STREAM_LIMIT) and catch the "
            "ValueError from readline() as an unrecoverable stream, the way "
            "misaka/extensions/mcp.py does -- or add the site to STREAM_ALLOWED with why."
        )
    if thread_problems:
        print("streamcheck: unjudged UI timer/thread site(s):")
        print("\n".join(thread_problems))
        print(
            "Decide whether its callback may run off the event loop. If it touches component "
            "state or wakes a coroutine it must end with loop.call_soon_threadsafe(...) / "
            "call_later(...) -- the rule stated in misaka/ui/tui/terminal.py's module "
            "docstring. Then record the verdict in THREAD_ALLOWED."
        )
    if stream_problems or thread_problems:
        return 1
    print("streamcheck: stream limits and UI timer hand-offs ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
