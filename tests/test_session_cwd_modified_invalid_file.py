"""Translated from pi's ``packages/coding-agent/test/session-cwd.test.ts``, ``session-info-modified-timestamp.test.ts``
and ``session-file-invalid.test.ts`` (v1.1.0).

MISAKA fork: pi's invalid-file case runs the CLI and checks its stderr; this checks the same contract in process (the
file is refused with a session error and left unchanged), since MISAKA's message and CLI wrapping are its own.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from misaka.core.agent_session_runtime import createAgentSessionRuntime
from misaka.core.session_cwd import MissingSessionCwdError, getMissingSessionCwdIssue
from misaka.core.session_manager import InvalidSessionFileError, SessionManager
from misaka.utils.values import read_field


def writeSessionFile(path: Path, cwd: str) -> None:
    path.write_text(
        json.dumps({"type": "session", "version": 3, "id": "session-id", "timestamp": "2026-01-01T00:00:00.000Z", "cwd": cwd})
        + "\n",
        encoding="utf-8",
    )


# --- session cwd handling -----------------------------------------------------------------------


def test_detects_missing_session_cwd_from_persisted_sessions(tmp_path: Path):
    fallbackCwd = tmp_path / "fallback"
    fallbackCwd.mkdir()
    missingCwd = str(fallbackCwd / "does-not-exist")
    sessionFile = tmp_path / "session.jsonl"
    writeSessionFile(sessionFile, missingCwd)

    sessionManager = SessionManager.open(str(sessionFile))
    issue = getMissingSessionCwdIssue(sessionManager, str(fallbackCwd))
    assert {key: read_field(issue, key) for key in ("sessionFile", "sessionCwd", "fallbackCwd")} == {
        "sessionFile": sessionManager.getSessionFile(),
        "sessionCwd": missingCwd,
        "fallbackCwd": str(fallbackCwd),
    }


def test_supports_overriding_the_effective_cwd_when_opening_a_session(tmp_path: Path):
    fallbackCwd = tmp_path / "fallback"
    fallbackCwd.mkdir()
    sessionFile = tmp_path / "session.jsonl"
    writeSessionFile(sessionFile, str(fallbackCwd / "does-not-exist"))

    sessionManager = SessionManager.open(str(sessionFile), None, str(fallbackCwd))
    assert sessionManager.getCwd() == str(fallbackCwd)
    assert getMissingSessionCwdIssue(sessionManager, str(fallbackCwd)) is None


async def test_throws_a_controlled_error_before_runtime_creation_when_the_stored_cwd_is_missing(tmp_path: Path):
    fallbackCwd = tmp_path / "fallback"
    fallbackCwd.mkdir()
    sessionFile = tmp_path / "session.jsonl"
    writeSessionFile(sessionFile, str(fallbackCwd / "does-not-exist"))

    sessionManager = SessionManager.open(str(sessionFile))
    state = {"createRuntimeCalled": False}

    async def createRuntime(*_: Any) -> Any:
        state["createRuntimeCalled"] = True
        raise RuntimeError("should not be called")

    with pytest.raises(MissingSessionCwdError):
        await createAgentSessionRuntime(
            createRuntime, {"cwd": str(fallbackCwd), "agentDir": str(fallbackCwd), "sessionManager": sessionManager}
        )
    assert state["createRuntimeCalled"] is False


# --- SessionInfo.modified -----------------------------------------------------------------------


def assistantMessage(text: str, timestamp: int) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "api": "openai-completions",
        "provider": "openai",
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
        "timestamp": timestamp,
    }


async def test_uses_last_user_assistant_message_timestamp_instead_of_file_mtime(tmp_path: Path):
    filePath = tmp_path / "pi-session-modified.jsonl"
    filePath.write_text(
        json.dumps({"type": "session", "id": "test-session", "version": 3, "timestamp": "1970-01-01T00:00:00.000Z", "cwd": "/tmp"})
        + "\n",
        encoding="utf-8",
    )
    # SessionManager only persists once it has seen at least one assistant message.
    SessionManager.open(str(filePath)).appendMessage(assistantMessage("hi", int(time.time() * 1000)))

    before = os.stat(filePath)
    # Ensure the file mtime can differ from our message timestamp even on coarse filesystems.
    await asyncio.sleep(0.01)

    mgr = SessionManager.open(str(filePath))
    msgTime = int(time.time() * 1000)
    mgr.appendMessage(assistantMessage("later", msgTime))

    sessions = await SessionManager.list("/tmp", str(filePath.parent))
    session = next((info for info in sessions if os.path.normcase(info.path) == os.path.normcase(str(filePath))), None)
    assert session is not None
    modifiedMs = round(session.modified.timestamp() * 1000)
    assert modifiedMs == msgTime
    assert modifiedMs != round(before.st_mtime * 1000)


# --- --session invalid file handling ------------------------------------------------------------


def test_refuses_a_non_session_file_and_preserves_its_content(tmp_path: Path):
    sessionFile = tmp_path / "not-a-session.log"
    originalContent = '{"type":"event","data":"not a session"}\n'
    sessionFile.write_text(originalContent, encoding="utf-8")

    with pytest.raises(InvalidSessionFileError) as error:
        SessionManager.open(str(sessionFile))
    assert str(sessionFile) in str(error.value)
    assert sessionFile.read_text(encoding="utf-8") == originalContent
