"""Bundled extensions are pluggable: the rest of misaka reaches them only through the extension API
and the discovery package, so a folder removed from ``misaka/extensions`` takes its feature with it
and breaks nothing else.

The setup wizard is the one exception, by the user's decision (2026-09-29): it installs and
configures what ships in the package, so it may know a bundled extension by name."""
import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "misaka"
WIZARD = PACKAGE / "cli" / "setup_wizard.py"
# What the bundled extensions are called, and what they register: their tools, their skill, their
# command. None of it belongs in the text or the imports of anything else.
NAMES = ("lcm", "LCM", "coverage_scan", "coverage maps", "coverage-maps")
# The owner's second exception (2026-10-12): the interactive mode offers the Radius MCP server after a Radius
# login and writes it with the MCP extension's config helpers, as upstream pi does.
ALLOWED_IMPORTS = {("misaka/modes/interactive/interactive_mode.py", "misaka.extensions.mcp.config.addMcpServerConfig"),
                   ("misaka/modes/interactive/interactive_mode.py", "misaka.extensions.mcp.config.loadMcpConfig")}


def _files():
    for path in sorted(PACKAGE.rglob("*.py")):
        parts = path.relative_to(PACKAGE).parts
        if parts[0] != "extensions" and "assets" not in parts and path != WIZARD:
            yield path, ast.parse(path.read_text(encoding="utf-8"))


def _docstrings(tree):
    return {id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}


def test_nothing_outside_the_extensions_imports_one_of_them():
    """2026-09-29: with the LCM plugin's folder removed, every subagent died -- the subagent runtime
    imported the plugin's storage to hand the child its project. An extension that needs something
    in a child process adds it on ``subagent_start``. The process entry may use the discovery
    package itself (``discover``, ``commands``), never a module inside it."""
    offenders = []
    for path, tree in _files():
        for node in ast.walk(tree):
            modules = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                       else [f"{node.module}.{alias.name}" for alias in node.names]
                       if isinstance(node, ast.ImportFrom) and node.module else [])
            where = path.relative_to(PACKAGE.parent).as_posix()
            if any(name.startswith("misaka.extensions.") and (where, name) not in ALLOWED_IMPORTS for name in modules):
                offenders.append(f"{where}:{node.lineno}")
    assert offenders == []


def test_nothing_outside_the_extensions_names_one_in_its_own_text():
    """2026-09-29: the research contracts named the coverage extension's tool and the LCM plugin's
    tools, research restored the rules of a tool it named, and a command's help named the plugin.
    Each extension's tools say how they are used; comments and docstrings may explain history."""
    offenders = []
    for path, tree in _files():
        docstrings = _docstrings(tree)
        offenders += [f"{path.relative_to(PACKAGE.parent)}:{node.lineno}: {node.value[:60]!r}"
                      for node in ast.walk(tree)
                      if isinstance(node, ast.Constant) and isinstance(node.value, str)
                      and id(node) not in docstrings and any(name in node.value for name in NAMES)]
    assert offenders == []


def test_an_extension_offers_its_own_command():
    """``misaka lcm`` is the LCM plugin's: listed in help and dispatched when the plugin is there,
    an unknown command when it is not (it was wired into the CLI, and crashed without the plugin)."""
    pytest.importorskip("misaka.extensions.misaka_lcm")
    from misaka import extensions
    from misaka.cli import app

    commands = extensions.commands()
    assert "lcm" in commands and "Inspect LCM" in (commands["lcm"].__doc__ or "")
    assert "lcm" in app._parser(commands).format_help()
    assert "lcm" not in app._parser().format_help()
