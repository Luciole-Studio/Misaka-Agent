"""``misaka init`` writes a ``.gitignore`` for the material MISAKA derives or caches, so a
project's ``git status`` shows the work and not 38 lines of caches, download folders and
rebuilt source bundles (2026-09-23). A fresh repository commits it with the skeleton; an
existing repository gets the file but, as with everything init does there, no commit; a
project that already has one keeps its own."""
import subprocess

import pytest

from misaka.core.platform import cards


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False).stdout


@pytest.fixture
def home_db(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "_cfg_db", lambda: str(tmp_path / "home-board.db"))


def test_a_fresh_project_commits_the_ignore_file_with_the_skeleton(tmp_path, home_db):
    folder = tmp_path / "proj"
    folder.mkdir()
    actions = cards.init_project(str(folder))
    text = (folder / ".gitignore").read_text(encoding="utf-8")
    for line in (".misaka/", ".pageindex/", "downloads/", "__pycache__/", "*.pyc", "nodes/**/sources/", "final/*-SOURCES.md"):
        assert line in text.split("\n")
    assert "sources/" not in text.split("\n")
    assert ".gitignore written" in actions
    assert ".gitignore" in _git(folder, "ls-files").split()


def test_an_existing_repository_gets_the_file_but_no_commit(tmp_path, home_db):
    folder = tmp_path / "proj"
    folder.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=folder, check=True)
    actions = cards.init_project(str(folder))
    assert (folder / ".gitignore").exists() and ".gitignore written" in actions
    assert _git(folder, "rev-parse", "--verify", "HEAD").strip() == ""


def test_a_project_with_its_own_ignore_file_keeps_it(tmp_path, home_db):
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / ".gitignore").write_text("mine\n", encoding="utf-8")
    actions = cards.init_project(str(folder))
    assert (folder / ".gitignore").read_text(encoding="utf-8") == "mine\n"
    assert ".gitignore written" not in actions
