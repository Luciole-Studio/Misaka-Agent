"""Translated from pi's ``packages/coding-agent/test/session-id-readonly.test.ts`` (v1.1.0).

MISAKA fork: pi's metadata-command case spawns the CLI with a scratch agent dir; this runs ``main`` in process against
the per-test ``MISAKA_HOME`` the conftest sets up, and ``console.error`` spies read pytest's captured stderr.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from misaka.cli.args import Args
from misaka.cli.engine import createSessionManager, main
from misaka.config.sessions import sessions_root
from misaka.core.session_manager import NewSessionOptions, SessionManager
from misaka.core.settings_manager import SettingsManager


def hasSessionWithId(root: str, sessionId: str) -> bool:
    if not os.path.exists(root):
        return False
    for entry in os.scandir(root):
        if entry.is_dir() and hasSessionWithId(entry.path, sessionId):
            return True
        if not entry.is_file() or not entry.name.endswith(".jsonl"):
            continue
        try:
            with open(entry.path, encoding="utf-8") as handle:
                header = json.loads(handle.readline())
            if header.get("type") == "session" and header.get("id") == sessionId:
                return True
        except (OSError, ValueError, AttributeError):
            # Ignore malformed session files.
            pass
    return False


def args(**overrides: Any) -> Args:
    return Args(**overrides)


def persistSession(session: SessionManager, content: str) -> None:
    session.appendMessage({"role": "user", "content": content, "timestamp": int(time.time() * 1000)})
    session.appendMessage({
        "role": "assistant",
        "content": [{"type": "text", "text": "persisted"}],
        "api": "anthropic-messages",
        "provider": "anthropic",
        "model": "test",
        "usage": {
            "input": 1,
            "output": 1,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 2,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
        },
        "stopReason": "stop",
        "timestamp": int(time.time() * 1000),
    })


@pytest.fixture
def tempRoot(tmp_path: Path) -> Path:
    return tmp_path.resolve()


async def test_does_not_persist_a_custom_id_for_metadata_commands(
    tempRoot: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    projectDir = tempRoot / "project"
    projectDir.mkdir()
    monkeypatch.chdir(projectDir)
    monkeypatch.setenv("MISAKA_OFFLINE", "1")

    code = await main(["--session-id", "read-only-help", "--help"])

    assert code == 0
    assert hasSessionWithId(sessions_root(), "read-only-help") is False


async def test_creates_missing_ids_and_reopens_existing_ids_in_process(
    tempRoot: Path, capsys: pytest.CaptureFixture[str]
):
    projectDir = tempRoot / "project"
    sessionDir = str(tempRoot / "sessions")
    projectDir.mkdir()
    settingsManager = SettingsManager.inMemory()

    readOnly = await createSessionManager(args(sessionId="read-only", help=True), str(projectDir), sessionDir, settingsManager)
    assert readOnly.getSessionId() == "read-only"
    assert readOnly.getSessionFile() is None

    created = await createSessionManager(args(sessionId="persisted-id"), str(projectDir), sessionDir, settingsManager)
    persistSession(created, "persist me")
    assert "creating a new session" in capsys.readouterr().err

    reopened = await createSessionManager(args(sessionId="persisted-id"), str(projectDir), sessionDir, settingsManager)
    assert reopened.getSessionFile() == created.getSessionFile()
    assert capsys.readouterr().err == ""


# Regression test for #9440.
async def test_looks_up_exact_ids_without_building_full_session_listings(
    tempRoot: Path, monkeypatch: pytest.MonkeyPatch
):
    projectDir = tempRoot / "project"
    sessionDir = str(tempRoot / "sessions")
    projectDir.mkdir()
    unrelated = SessionManager.create(str(projectDir), sessionDir, NewSessionOptions(id="unrelated-id"))
    persistSession(unrelated, "large transcript contents must not be loaded")
    calls: list[Any] = []

    async def list_(*a: Any) -> Any:
        calls.append(a)
        raise RuntimeError("unexpected full listing")

    monkeypatch.setattr(SessionManager, "list", list_)

    created = await createSessionManager(
        args(sessionId="fresh-id"), str(projectDir), sessionDir, SettingsManager.inMemory()
    )

    assert created.getSessionId() == "fresh-id"
    assert calls == []


async def test_reopens_an_exact_id_from_a_renamed_session_file(tempRoot: Path):
    projectDir = tempRoot / "project"
    sessionDir = tempRoot / "sessions"
    projectDir.mkdir()
    original = SessionManager.create(str(projectDir), str(sessionDir), NewSessionOptions(id="renamed-id"))
    persistSession(original, "persist me")
    renamedPath = str(sessionDir / "imported-session.jsonl")
    os.rename(original.getSessionFile(), renamedPath)

    reopened = await createSessionManager(
        args(sessionId="renamed-id"), str(projectDir), str(sessionDir), SettingsManager.inMemory()
    )

    assert reopened.getSessionFile() == renamedPath


def test_filters_exact_ids_by_cwd_in_a_custom_session_directory(tempRoot: Path):
    projectA = tempRoot / "project-a"
    projectB = tempRoot / "project-b"
    sessionDir = str(tempRoot / "sessions")
    projectA.mkdir()
    projectB.mkdir()
    foreign = SessionManager.create(str(projectB), sessionDir, NewSessionOptions(id="foreign-id"))
    persistSession(foreign, "foreign session")

    assert SessionManager.findById(str(projectA), "foreign-id", sessionDir) is None
    assert SessionManager.findById(str(projectB), "foreign-id", sessionDir) == foreign.getSessionFile()


async def test_rejects_an_existing_fork_target_in_process(tempRoot: Path):
    projectDir = tempRoot / "project"
    sessionDir = str(tempRoot / "sessions")
    projectDir.mkdir()
    source = SessionManager.create(str(projectDir), sessionDir, NewSessionOptions(id="source-id"))
    persistSession(source, "source")
    target = SessionManager.create(str(projectDir), sessionDir, NewSessionOptions(id="existing-id"))
    persistSession(target, "target")

    with pytest.raises(SystemExit) as exit_info:
        await createSessionManager(
            args(fork="source-id", sessionId="existing-id"),
            str(projectDir),
            sessionDir,
            SettingsManager.inMemory(),
        )
    assert exit_info.value.code == 1
