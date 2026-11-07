"""A bundle rebuild removes only what the last build placed under ``sources/``."""
from pathlib import Path

import pytest

from misaka.core.platform import tasks
from misaka.core.research import bundle, runs


@pytest.fixture
def state(tmp_path, monkeypatch):
    con = tasks.connect(str(tmp_path / "board.db"))
    run = runs.create(con, workspace=str(tmp_path), question="fixture question", limits={"max_depth": 1})
    monkeypatch.setattr(bundle.corpus, "docs", lambda **k: [])
    yield con, run
    con.close()

def final(con, run, text):
    runs.write_text(con, run["id"], "final", "Final", runs.run_path(run, "final.md"), text)
    return Path(bundle.final_bundle(con, run)).read_text(encoding="utf-8")

def source(run, name="source.md"):
    path = Path(run["workspace"]) / "downloads" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("fixture source", encoding="utf-8")
    return path

def folder(run):
    return Path(run["workspace"]) / "final" / f"{run['id']}-sources"

def test_hand_saved_file_survives_a_rebuild(state):
    con, run = state
    source(run)
    final(con, run, "## Sources\n- downloads/source.md\n")
    hand = folder(run) / "books" / "scan.pdf"
    hand.parent.mkdir()
    hand.write_bytes(b"%PDF-1.4 only copy")
    final(con, run, "## Sources\n- downloads/source.md\n")
    assert hand.read_bytes() == b"%PDF-1.4 only copy"
    assert (folder(run) / "source.md").is_file()

def test_last_build_links_are_still_cleared(state):
    con, run = state
    source(run, "one.md"); source(run, "two.md")
    final(con, run, "## Sources\n- downloads/one.md\n")
    final(con, run, "## Sources\n- downloads/two.md\n")
    assert not (folder(run) / "one.md").exists()
    assert (folder(run) / "two.md").is_file()

def test_nothing_cited_removes_the_empty_folder(state):
    con, run = state
    source(run)
    final(con, run, "## Sources\n- downloads/source.md\n")
    final(con, run, "nothing cited\n")
    assert not folder(run).exists()

def test_no_manifest_means_nothing_is_ours(state):
    con, run = state
    hand = folder(run) / "scan.pdf"
    hand.parent.mkdir(parents=True)
    hand.write_bytes(b"%PDF-1.4")
    final(con, run, "nothing cited\n")
    assert hand.is_file()

def test_last_copy_is_kept_when_the_original_is_gone(state):
    con, run = state
    original = source(run)
    final(con, run, "## Sources\n- downloads/source.md\n")
    original.unlink()
    final(con, run, "nothing cited\n")
    assert (folder(run) / "source.md").read_text(encoding="utf-8") == "fixture source"

def test_new_link_does_not_collide_with_a_hand_saved_file(state):
    con, run = state
    source(run)
    hand = folder(run) / "source.md"
    hand.parent.mkdir(parents=True)
    hand.write_text("hand", encoding="utf-8")
    manifest = final(con, run, "## Sources\n- downloads/source.md\n")
    assert hand.read_text(encoding="utf-8") == "hand"
    assert "could not be placed" not in manifest
    assert sum(p.read_text(encoding="utf-8") == "fixture source" for p in folder(run).iterdir()) == 1


def test_a_cited_hand_saved_file_is_listed_as_cited_not_unresolved(state):
    con, run = state
    hand = folder(run) / "scan.md"
    hand.parent.mkdir(parents=True)
    hand.write_text("hand saved", encoding="utf-8")
    manifest = final(con, run, f"## Sources\n- final/{run['id']}-sources/scan.md\n")
    assert "disposable bundle source" not in manifest
    assert "## Cited, already in this folder" in manifest
    assert hand.is_file()
