"""Every release's home opens on this build: the release gate of docs/plans/upgrade-install-uninstall-2026-09-29.md.

``tests/fixtures/homes/<version>/`` is what that release left on a user's disk (written by
``make_home.py`` at the release commit). A change to any stored format fails here until it ships
the migration that carries these homes forward. Before a release is tagged, regenerating its
sample records a format change instead; after, the sample never changes.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from misaka.config import CFG, current_config, home

SAMPLES = sorted(path for path in (Path(__file__).parent / "fixtures" / "homes").iterdir()
                 if path.is_dir() and path.name != "__pycache__")


@pytest.mark.parametrize("sample", SAMPLES, ids=[path.name for path in SAMPLES])
def test_a_release_home_opens_and_reads_on_this_build(sample, tmp_path, monkeypatch):
    from misaka.core.network import messages
    from misaka.core.platform import tasks
    from misaka.core.research import runs
    from misaka.core.session_manager import SessionManager

    shutil.copytree(sample, tmp_path / sample.name, symlinks=True)
    monkeypatch.setenv(home.ENV_HOME, str(tmp_path / sample.name / "home"))

    con = tasks.connect(CFG["db"])
    try:
        runs.init(con)
        assert [row["title"] for row in con.execute("SELECT title FROM tasks")] == ["Read the 1905 memorials"]
        assert [(row["status"], row["phase"]) for row in runs.listing(con)] == [("done", "done")]
    finally:
        con.close()
    mail = messages.connect()
    try:
        assert [row["body"] for row in mail.execute("SELECT body FROM messages")] == ["The memorials are in sources/."]
    finally:
        mail.close()
    (transcript,) = home.path("sessions").rglob("*.jsonl")
    session = SessionManager.open(str(transcript))
    assert [message["role"] for message in session.buildSessionContext().messages] == ["user", "assistant"]
    assert (current_config()["provider"], current_config()["default_model"]) == ("anthropic", "claude-sonnet-4-5")
    assert (home.path("profiles_root") / "10032").is_dir()
