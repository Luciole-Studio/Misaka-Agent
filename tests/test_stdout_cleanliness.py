"""Translated from pi's ``packages/coding-agent/test/stdout-cleanliness.test.ts`` (v1.1.0).

MISAKA forks: ``main`` runs in process against the per-test ``MISAKA_HOME`` (pi spawns its CLI), and the npm-package
startup chatter pi routes to stderr has no counterpart (MISAKA has no package installer), so those assertions are
dropped; what remains is where the help text goes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from misaka.cli.engine import main


@pytest.fixture
def projectDir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv("MISAKA_OFFLINE", "1")
    return project


async def test_prints_plain_help_to_stdout_when_stdout_is_redirected(
    projectDir: Path, capsys: pytest.CaptureFixture[str]
):
    code = await main(["--help"])
    result = capsys.readouterr()

    assert code == 0
    assert "Usage:" in result.out
    assert "Usage:" not in result.err


async def test_keeps_stdout_empty_for_mode_json_help(projectDir: Path, capsys: pytest.CaptureFixture[str]):
    code = await main(["--mode", "json", "--help", "--approve"])
    result = capsys.readouterr()

    assert code == 0
    assert result.out == ""
    assert "Usage:" in result.err
