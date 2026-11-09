"""Translated from pi's ``packages/coding-agent/test/startup-session-name.test.ts`` (v1.1.0).

MISAKA fork: ``main`` runs in process against the per-test ``MISAKA_HOME`` (pi spawns its CLI with a scratch agent dir).
"""

from __future__ import annotations

import io
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.cli.engine import main


def createSessionFile(projectDir: str, sessionFile: Path) -> None:
    timestamp = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    sessionFile.write_text(
        json.dumps({"type": "session", "version": 3, "id": "existing-session", "timestamp": timestamp, "cwd": projectDir})
        + "\n"
        + json.dumps({
            "type": "message",
            "id": "assistant-1",
            "parentId": None,
            "timestamp": timestamp,
            # MISAKA fork: messages are validated when the agent state is built, so the assistant message carries
            # the fields pi's fixture leaves out (api, usage, stopReason).
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hello"}],
                "api": "anthropic-messages",
                "provider": "anthropic",
                "model": "claude-sonnet-4-5",
                "usage": {
                    "input": 0,
                    "output": 0,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "totalTokens": 0,
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
                },
                "stopReason": "stop",
                "timestamp": int(time.time() * 1000),
            },
        })
        + "\n",
        encoding="utf-8",
    )


def readSessionInfoNames(sessionFile: Path) -> list[str]:
    entries = [json.loads(line) for line in sessionFile.read_text(encoding="utf-8").strip().split("\n")]
    return [entry.get("name") or "" for entry in entries if entry.get("type") == "session_info"]


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    projectDir = tmp_path / "project"
    projectDir.mkdir()
    sessionFile = tmp_path / "session.jsonl"
    createSessionFile(str(projectDir), sessionFile)
    monkeypatch.chdir(projectDir)
    monkeypatch.setenv("MISAKA_OFFLINE", "1")
    # pi spawns the CLI with stdin "ignore": an empty, non-TTY stdin.
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return SimpleNamespace(projectDir=projectDir, sessionFile=sessionFile)


async def test_sets_name_on_the_selected_session_before_runtime_model_validation(dirs: SimpleNamespace):
    code = await main(
        ["--session", str(dirs.sessionFile), "--name", "  CLI Named Session  ", "--model", "missing-model", "-p", "hi"]
    )

    assert code == 1
    assert readSessionInfoNames(dirs.sessionFile) == ["CLI Named Session"]
