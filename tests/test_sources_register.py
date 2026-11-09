"""A file saved by hand under ``sources/`` is the card's own, and is registered as such."""
# ruff: noqa: F811 - ``state`` is a fixture shared with test_research_artifact_drift
import json
from pathlib import Path

from misaka.core.documents import index as corpus
from misaka.core.platform import tasks
from misaka.core.research import report, runs, workflow
from tests.test_research_artifact_drift import _accept, state  # noqa: F401


def named(paths, tail):
    return next(path for rel, path in paths.items() if rel.endswith(tail))

def rows(con, run, tid):
    return {Path(r["path"]).name: r for r in runs.artifacts(con, run["id"], task_id=tid)}

def test_a_file_saved_in_sources_is_registered_and_marked(state):
    con, run, root = state
    tid, _paths = _accept(con, run, root, "hand", {"notes.md": "own\n", "sources/books/scan.md": "only copy\n"})
    workflow.settle_done_tasks(con, run_id=run["id"])
    got = rows(con, run, tid)
    assert set(got) == {"notes.md", "scan.md"}
    assert json.loads(got["scan.md"]["metadata_json"])["saved_by_hand"] is True
    assert "saved_by_hand" not in json.loads(got["notes.md"]["metadata_json"])
    assert tasks.get(con, tid)["status"] == "done"

def test_an_old_record_of_a_deleted_sources_file_does_not_fail_the_card(state):
    con, run, root = state
    tid, paths = _accept(con, run, root, "gone", {"notes.md": "own\n", "sources/books/gone.md": "x\n"})
    named(paths, "sources/books/gone.md").unlink()
    workflow.settle_done_tasks(con, run_id=run["id"])
    assert set(rows(con, run, tid)) == {"notes.md"}
    assert tasks.get(con, tid)["status"] == "done"
    assert not tasks.latest_payload(con, tid, "research_artifact_drift", generation=1)

def test_a_link_the_bundle_placed_is_not_registered(state):
    con, run, root = state
    tid, paths = _accept(con, run, root, "link", {"notes.md": "own\n", "sources/a.md": "link\n", "SOURCES.md": "x\n"})
    rel = named(paths, "notes.md").relative_to(run["workspace"]).as_posix()
    named(paths, "SOURCES.md").write_text(f"- `sources/a.md` ← `{rel}`\n", encoding="utf-8")
    workflow.settle_done_tasks(con, run_id=run["id"])
    assert set(rows(con, run, tid)) == {"notes.md"}
    assert tasks.get(con, tid)["status"] == "done"

def test_closing_a_node_does_not_read_a_saved_file_again(state, monkeypatch):
    con, run, root = state
    _, _paths = _accept(con, run, root, "scan", {"notes.md": "own\n", "sources/scan.md": "refused scan\n"})
    workflow.settle_done_tasks(con, run_id=run["id"])
    asked = []
    monkeypatch.setattr(report, "doc_id", lambda _run, path: asked.append(Path(path).name) or "c" * 12)
    found = report.index_run(con, run)
    assert "notes.md" in asked and "scan.md" not in asked
    assert "scan.md" not in {Path(p).name for p in found}

def test_a_saved_file_the_corpus_took_keeps_its_id(state, monkeypatch):
    con, run, root = state
    _, paths = _accept(con, run, root, "kept", {"notes.md": "own\n", "sources/book.md": "text long enough to count as a document\n"})
    workflow.settle_done_tasks(con, run_id=run["id"])
    book = named(paths, "sources/book.md")
    doc, _pages = corpus.ingest(str(book), workspace=run["workspace"])
    monkeypatch.setattr(report, "doc_id", lambda _run, path: None)
    assert report.index_run(con, run)[str(Path(book).resolve())] == doc
