"""The board does not know the workflows built on it. Its code (``core/platform``, ``core/network``)
and the session catalog import nothing of research and never name it: what the board needs from
research it asks its card owners (``platform.owners``), which research registers at process entry."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "misaka"
BOARD = [*sorted((PACKAGE / "core" / "platform").rglob("*.py")), *sorted((PACKAGE / "core" / "network").rglob("*.py")),
         PACKAGE / "core" / "session_catalog.py"]
# The one line that still says it: the migration of the event kind v9 named after research.
CARRIED = "UPDATE events SET kind='card_evidence' WHERE kind='research_evidence'"


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            yield node.module, node.lineno


def test_the_board_imports_nothing_of_research():
    offenders = [f"{path.relative_to(PACKAGE.parent).as_posix()}:{line}: {module}"
                 for path in BOARD for module, line in _imports(ast.parse(path.read_text(encoding="utf-8")))
                 if module == "misaka.core.research" or module.startswith("misaka.core.research.")]
    assert offenders == []


def test_the_board_never_names_research():
    offenders = [f"{path.relative_to(PACKAGE.parent).as_posix()}:{number}: {text.strip()}"
                 for path in BOARD
                 for number, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
                 if "research" in text.lower() and CARRIED not in text]
    assert offenders == []


def test_a_board_without_owners_treats_every_card_as_its_own(monkeypatch):
    from misaka.core.platform import owners

    monkeypatch.setattr(owners, "_OWNERS", [])
    assert owners.card(None, "t_any") == (None, None)
    assert owners.budget_scope(None, "t_any") is None
    assert owners.sender_scope(None, "t_any", None) == (None, None, None)
    assert owners.owns_window() is False


def test_the_panel_daemon_registers_the_card_owners(monkeypatch):
    """The daemon claims, deletes and lists cards without going through the CLI's entry: without its
    own ``bootstrap.install`` a research card's process holds no host slot there, a deleted card
    keeps its research link, and the sidebar loses which run and node a session belongs to."""
    import asyncio

    from misaka.cli import bootstrap
    from misaka.config import engine, env, home
    from misaka.ui.panel import daemon
    from misaka.utils import loop_watchdog

    calls = []
    monkeypatch.setattr(bootstrap, "install", lambda **kwargs: calls.append(kwargs))
    for module, name in ((home, "ensure"), (engine, "configure_logging"), (env, "load"),
                         (loop_watchdog, "configure"), (loop_watchdog, "watched")):
        monkeypatch.setattr(module, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(daemon, "Daemon", lambda: type("Stub", (), {"run": lambda self: None})())
    monkeypatch.setattr(asyncio, "run", lambda coroutine: None)
    daemon.main()
    assert calls == [{"check_settings": False}]
