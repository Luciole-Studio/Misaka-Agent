"""The graph stands on its own. It imports nothing of misaka but the board's transaction helpers, so
research, the board's logic and the extensions can come and go around it; and nothing outside it
reads or writes its tables, so every change to a graph passes its journal and its schema."""

from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "misaka"
GRAPH = PACKAGE / "core" / "graph"
ALLOWED = ("misaka.core.graph", "misaka.core.platform.tasks")
TABLES = re.compile(r"\bgraph_(?:nodes|edges|journal)\b|\b(?:FROM|INTO|UPDATE|JOIN)\s+graphs\b")


def _modules(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            yield node.module, node.lineno


def test_the_graph_imports_nothing_of_misaka_but_the_board_transaction():
    offenders = [
        f"{path.relative_to(PACKAGE.parent).as_posix()}:{line}: {module}"
        for path in sorted(GRAPH.rglob("*.py"))
        for module, line in _modules(ast.parse(path.read_text(encoding="utf-8")))
        if module.split(".")[0] == "misaka" and not module.startswith(ALLOWED)
    ]
    assert offenders == []


def test_nothing_outside_the_graph_touches_its_tables():
    offenders = [
        f"{path.relative_to(PACKAGE.parent).as_posix()}:{number}"
        for path in sorted(PACKAGE.rglob("*.py"))
        if GRAPH not in path.parents
        for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if TABLES.search(text)
    ]
    assert offenders == []
