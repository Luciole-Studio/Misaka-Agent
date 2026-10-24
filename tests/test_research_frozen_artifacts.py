"""B126 (2026-10-02, r_8fef527ac2): the final red team sent her critique to Last Order before she
completed her card, and Last Order, between phases, edited the saved report draft by hand. The
draft's frozen digest no longer matched, the run failed at its last step with a partial report, and
every resume failed the same way. What the workflow saved is not the file tools' to change, and a
checkpoint changed anyway is put back from the command it was saved from."""
import hashlib
from contextlib import closing
from pathlib import Path

import pytest

from misaka.config import CFG
from misaka.core.platform import tasks
from misaka.core.research import report, runs, workflow

DRAFT = "# Report\n\nThe crisis escalated in discourse first.\n"


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "_bundle", lambda *a, **k: None)
    with closing(tasks.connect(CFG["db"])) as con:
        runs.init(con)
        run = runs.create(con, workspace=str(tmp_path), question="Why did the crisis escalate?")
        assert runs.acquire_driver(con, run["id"], "driver:test")
        run = runs.get(con, run["id"])
        yield con, run


def _saved_draft(con, run):
    root = runs.root(con, run["id"])
    runs.record_action(con, run, root, "draft", {"markdown": DRAFT},
                       session_file=str(Path(run["workspace"]) / "lo.jsonl"), tool_call_id="t1")
    report._save(con, run, "draft", DRAFT.strip() + "\n")
    return report._checkpoint(con, run, "draft")


def test_the_file_tools_may_not_write_what_a_running_research_saved(lab):
    con, run = lab
    draft = _saved_draft(con, run)
    reason = runs.frozen_refusal(draft["path"])
    assert reason and "draft" in reason and "final adjudication" in reason
    # A card's own deliverable stays its Sister's; an unregistered file is anyone's.
    card_file = Path(run["workspace"]) / "nodes" / "b" / "cards" / "t_card" / "notes.md"
    card_file.parent.mkdir(parents=True)
    card_file.write_text("mine\n", encoding="utf-8")
    runs.register_file(con, run["id"], "task_output", card_file.name, str(card_file),
                       sha256=hashlib.sha256(card_file.read_bytes()).hexdigest(), task_id="t_card")
    assert runs.frozen_refusal(str(card_file)) is None
    assert runs.frozen_refusal(str(Path(run["workspace"]) / "scratch.md")) is None
    # Once the run is over its files are anyone's again.
    con.execute("UPDATE research_runs SET status='done' WHERE id=?", (run["id"],))
    assert runs.frozen_refusal(draft["path"]) is None


def test_a_draft_changed_after_it_was_saved_is_put_back_and_the_change_kept(lab):
    con, run = lab
    draft = _saved_draft(con, run)
    path = Path(draft["path"])
    path.write_text(DRAFT + "\nAn edit made between phases.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed since it was registered"):
        runs.artifact_text(draft)
    again = report._checkpoint(con, run, "draft")              # the resume that used to fail
    assert runs.artifact_text(again) == DRAFT.strip() + "\n"
    [kept] = path.parent.glob(f"{path.stem}.edited-*{path.suffix}")
    assert "An edit made between phases." in kept.read_text(encoding="utf-8")


def test_a_draft_moved_away_is_rebuilt_from_the_run(lab):
    """A missing file raised past the restore: the final review failed, and every resume after it,
    until the file was put back by hand (0.18.10 sweep)."""
    con, run = lab
    draft = _saved_draft(con, run)
    path = Path(draft["path"])
    path.rename(path.with_name("moved.md"))
    again = report._checkpoint(con, run, "draft")
    assert runs.artifact_text(again) == DRAFT.strip() + "\n"


def test_a_changed_checkpoint_with_nothing_to_rebuild_it_from_stays_an_error(lab):
    con, run = lab
    report._save(con, run, "draft", DRAFT)                      # no recorded command behind it
    path = Path(runs.artifacts(con, run["id"], kind="draft", run_level=True)[-1]["path"])
    path.write_text("something else\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed since it was registered"):
        report._checkpoint(con, run, "draft")


def test_a_failed_run_says_why_where_it_is_looked_up(lab):
    con, run = lab
    workflow._partial_result(con, run, "Final-report red-team review failed.", status="failed",
                             driver_lock="driver:test")
    assert runs.get(con, run["id"])["last_error"] == "Final-report red-team review failed."


def test_the_path_guard_refuses_last_order_s_own_window_too(lab):
    """The edit came from the root's window, a foreground session the home's rule leaves alone."""
    import asyncio
    from types import SimpleNamespace

    from misaka.core.platform.home_guard import HomeGuard

    con, run = lab
    draft = _saved_draft(con, run)
    guard = HomeGuard("foreground", run["workspace"], guard_paths=True)

    def call(tool, **arguments):
        return asyncio.run(guard.tool_call({"toolName": tool, "input": arguments}, SimpleNamespace()))

    refused = call("edit", path=draft["path"], oldText="Report", newText="Rapport")
    assert refused["block"] and "final adjudication" in refused["reason"]
    assert call("write", path=draft["path"], content="x")["block"]
    assert call("write", path=str(Path(run["workspace"]) / "notes.md"), content="x") is None


def test_a_frozen_file_is_refused_in_any_spelling_its_file_system_takes(tmp_path, monkeypatch):
    """APFS takes a name's NFC and NFD spellings for one file, and folds case: a frozen plan or
    conclusion was edited past the check under another spelling (0.18.10 sweep)."""
    import unicodedata
    from contextlib import closing

    from misaka.config import CFG, home
    from misaka.core.platform import tasks
    monkeypatch.setattr(home, "FOLDS_CASE", True)
    monkeypatch.setattr(home, "FOLDS_UNICODE", True)
    workspace = tmp_path / unicodedata.normalize("NFD", "研究データ")
    workspace.mkdir()
    with closing(tasks.connect(CFG["db"])) as con:
        run = runs.create(con, workspace=str(workspace), question="Why?")
        runs.acquire_driver(con, run["id"], "driver:test")
        run = runs.get(con, run["id"])
        draft = report._save(con, run, "draft", DRAFT)
    stored = draft["path"]
    assert runs.frozen_refusal(stored)
    assert runs.frozen_refusal(unicodedata.normalize("NFC", stored))
    assert runs.frozen_refusal(stored.replace("final", "FINAL"))
