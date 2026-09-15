"""Shared fixtures: every test runs against a throwaway agent directory, never ~/.misaka."""
from __future__ import annotations

import os
import tempfile

_AGENT_DIR = tempfile.mkdtemp(prefix="misaka-test-agent-")

# Assigned, not `setdefault`. These names exist so a developer can point a *running* MISAKA
# at somewhere other than ~/.misaka -- exactly what someone does while debugging, and often
# by exporting the real path. `setdefault` honoured that export, so the suite would then run
# against the live board: 6900 tests writing the user's cards, sessions and credentials.
# Three cards with a test-only `claim_lock='ours'` were found on this author's real board,
# permanently unclaimable, and nobody could say how they got there.
#
# A test run has no business reading the developer's state, so the environment does not get
# a vote. `MISAKA_OFFLINE` is likewise forced: a suite that reaches the network because a
# shell said it could is not the suite anyone reviewed.
os.environ["MISAKA_CODING_AGENT_DIR"] = _AGENT_DIR
os.environ["MISAKA_CODING_AGENT_SESSION_DIR"] = os.path.join(_AGENT_DIR, "sessions")
os.environ["MISAKA_OFFLINE"] = "1"
for _name, _file in (("MISAKA_DB", "board.db"), ("MISAKA_MESSAGES", "messages.db"),
                     ("MISAKA_LCM_DB", "lcm.db"), ("MISAKA_TASKS", "tasks"), ("MISAKA_PAGEINDEX", "pageindex"),
                     ("MISAKA_NET_SOCK", "net.sock"), ("MISAKA_NET_SNAPSHOT", "net.json"),
                     # The profiles tree: personalities, skills and MCP config. Until it had an
                     # override the suite read -- and `shared_soul` seeded -- the developer's own.
                     ("MISAKA_PROFILES", "profiles"),
                     ("MISAKA_WEB_CONFIG", "web.json"), ("MISAKA_WEB_CACHE", "web-cache"),
                     # The product's session tree: chats, cards, research and nested agents.
                     ("MISAKA_SESSIONS", "sessions-tree")):
    os.environ[_name] = os.path.join(_AGENT_DIR, _file)

import pytest


@pytest.fixture
def workspace(tmp_path):
    """A git-free workspace directory."""
    return tmp_path / "ws"


@pytest.fixture(autouse=True)
def _make_workspace(workspace):
    workspace.mkdir(exist_ok=True)


@pytest.fixture(autouse=True)
def _close_board_connections(monkeypatch):
    """Every board connection a test opens is closed at teardown (``-W error`` treats leaks as failures)."""
    from misaka.core.platform import tasks

    opened, real_connect = [], tasks.connect

    def connect(path):
        con = real_connect(path)
        opened.append(con)
        return con

    monkeypatch.setattr(tasks, "connect", connect)
    yield
    for con in opened:
        try:
            con.close()
        except Exception:  # noqa: BLE001, S110 - already closed by the test
            pass


@pytest.fixture(autouse=True)
def _vault_test_scope(request, tmp_path, monkeypatch):
    if "vault" not in request.node.path.name:
        yield
        return
    from misaka.core.web.browser.vault.backends import unlock
    from misaka.core.web.scope import WebScope
    with WebScope(str(tmp_path / "profile")).activate() as scope:
        scope.config = {"vault": {"onepassword": {"enabled": False}, "bitwarden": {"enabled": False}}}
        scope.environment = {}
        yield
        unlock.lock()
