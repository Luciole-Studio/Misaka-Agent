"""Translated from pi's ``coding-agent/test/format-resume-command.test.ts`` (v1.1.0).

MISAKA fork: a conversation resumes through ``misaka chat --session <id>``, which finds the session in the window's own
store, so no ``--session-dir`` is ever named; a Sister's window adds ``--as <name>``.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from misaka.config import APP_NAME
from misaka.modes.interactive.interactive_mode import formatResumeCommand


@pytest.fixture
def setStdoutIsTTY(monkeypatch):
    def set(value: bool) -> None:
        monkeypatch.setattr(sys, "stdout", SimpleNamespace(isatty=lambda: value, write=lambda _text: None))

    monkeypatch.delenv("MISAKA_WHO", raising=False)
    return set


def createTempFile(tmp_path) -> str:
    file = os.path.join(tmp_path, "session.jsonl")
    with open(file, "w", encoding="utf-8") as handle:
        handle.write("\n")
    return file


def createSessionManager(
    *,
    persisted: bool = True,
    sessionFile: str | None = None,
    sessionId: str = "0197f6e4-4cf9-7f44-a2d8-f8f7f49ee9d3",
    sessionDir: str = "/tmp/pi-sessions",
    usesDefaultSessionDir: bool = True,
):
    return SimpleNamespace(
        isPersisted=lambda: persisted,
        getSessionFile=lambda: sessionFile,
        getSessionId=lambda: sessionId,
        getSessionDir=lambda: sessionDir,
        usesDefaultSessionDir=lambda: usesDefaultSessionDir,
    )


def test_returns_a_session_resume_command_for_default_session_dirs(setStdoutIsTTY, tmp_path):
    setStdoutIsTTY(True)
    sessionManager = createSessionManager(sessionFile=createTempFile(tmp_path), sessionId="test-session")

    assert formatResumeCommand(sessionManager) == f"{APP_NAME} chat --session test-session"


def test_names_no_session_dir_for_non_default_session_dirs(setStdoutIsTTY, tmp_path):
    # MISAKA fork: pi adds `--session-dir /tmp/custom-pi-sessions`.
    setStdoutIsTTY(True)
    sessionManager = createSessionManager(
        sessionFile=createTempFile(tmp_path),
        sessionId="test-session",
        sessionDir="/tmp/custom pi's sessions",
        usesDefaultSessionDir=False,
    )

    assert formatResumeCommand(sessionManager) == f"{APP_NAME} chat --session test-session"


def test_a_sister_window_names_the_sister(setStdoutIsTTY, tmp_path, monkeypatch):
    # MISAKA fork: a Sister's sessions live in her own store.
    setStdoutIsTTY(True)
    monkeypatch.setenv("MISAKA_WHO", "Sister 10032")
    sessionManager = createSessionManager(sessionFile=createTempFile(tmp_path), sessionId="test-session")

    assert formatResumeCommand(sessionManager) == f"{APP_NAME} chat --as 'Sister 10032' --session test-session"


def test_returns_undefined_when_stdout_is_not_a_tty(setStdoutIsTTY, tmp_path):
    setStdoutIsTTY(False)
    sessionManager = createSessionManager(sessionFile=createTempFile(tmp_path))

    assert formatResumeCommand(sessionManager) is None


def test_returns_undefined_for_in_memory_sessions(setStdoutIsTTY, tmp_path):
    setStdoutIsTTY(True)
    sessionManager = createSessionManager(persisted=False, sessionFile=createTempFile(tmp_path))

    assert formatResumeCommand(sessionManager) is None


def test_returns_undefined_when_the_session_file_is_missing(setStdoutIsTTY, tmp_path):
    setStdoutIsTTY(True)
    sessionManager = createSessionManager(sessionFile=os.path.join(tmp_path, "pi-missing-session.jsonl"))

    assert formatResumeCommand(sessionManager) is None


def test_returns_undefined_when_the_session_file_is_not_set(setStdoutIsTTY):
    setStdoutIsTTY(True)
    sessionManager = createSessionManager(sessionFile=None)

    assert formatResumeCommand(sessionManager) is None
