"""Shared fixtures for the web tests. The throwaway home comes from the root conftest."""
from __future__ import annotations

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
