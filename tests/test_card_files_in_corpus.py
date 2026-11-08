"""What a card leaves behind joins the corpus by itself only if it is something to find there.

Every file changed in a card's folder used to be offered to the corpus. A Sister reading a scan
page by page therefore left one document per page, with no book's name and no way back to the
book: 29,000 of one project's 33,000 documents. What a card hands over stands directly in its
folder or under ``sources/`` and ``reports/``; below any other folder only an original document
(a PDF it fetched, say) counts, because nothing else would put that book in the corpus."""
# ruff: noqa: F811 - ``state`` is a fixture shared with test_research_artifact_drift
import os
from pathlib import Path

import pytest

from misaka.core.documents import index as corpus
from misaka.core.documents import workspace as ws_index
from misaka.core.research import report, workflow
from tests.test_research_artifact_drift import _accept, state  # noqa: F401

PAGE = "A page long enough to count as the text of a document in the corpus, and read as such. " * 3


@pytest.mark.parametrize("place, joins", [
    ("notes.md", True),
    ("critique.md", True),
    ("sources/books/scan.txt", True),
    ("reports/read_p22.md", True),
    ("raw/book.pdf", True),
    ("originals/Book.PDF", True),
    ("work/gazette.djvu", True),
    ("raw/deep/er/memo.docx", True),
    ("work/ocr/p0001.txt", False),
    ("work/reads/p22.md", False),
    ("raw/page.html", False),
    ("data/results.json", False),
    ("ndlfts/hits.tsv", False),
])
def test_what_joins_from_a_card_folder(tmp_path, place, joins):
    card = {"id": "t_1", "workspace": str(tmp_path), "output_dir": str(tmp_path / "nodes/n/cards/t_1")}
    assert ws_index.belongs_in_corpus(card, str(Path(card["output_dir"], place))) is joins


def test_a_file_outside_the_card_folder_is_not_judged_by_it(tmp_path):
    card = {"id": "t_1", "workspace": str(tmp_path), "output_dir": str(tmp_path / "outputs/t_1")}
    assert ws_index.belongs_in_corpus(card, str(tmp_path / "chapters/draft/one.md"))


def test_a_card_without_a_folder_is_taken_as_it_was(tmp_path):
    assert ws_index.belongs_in_corpus({"id": "t_1", "workspace": str(tmp_path)}, str(tmp_path / "work/ocr/p1.txt"))
    card = {"id": "t_1", "workspace": str(tmp_path), "output_dir": None}
    assert ws_index.belongs_in_corpus(card, str(tmp_path / "work/ocr/p1.txt"))


def test_a_finished_card_leaves_its_working_files_out(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "extract_pages", lambda p, meta=None: [PAGE + os.path.basename(p)])
    folder = tmp_path / "nodes/n/cards/t_1"
    names = ["findings.md", "reports/read_p22.md", "raw/book.pdf", "work/ocr/p0001.txt", "work/ocr/p0002.txt"]
    for name in names:
        Path(folder, name).parent.mkdir(parents=True, exist_ok=True)
        Path(folder, name).write_text(name, encoding="utf-8")
    task = {"id": "t_1", "workspace": str(tmp_path), "output_dir": str(folder)}

    got = ws_index.ingest_artifacts(None, task, [str(Path("nodes/n/cards/t_1", name)) for name in names])

    assert sorted(Path(relative).name for _doc, relative in got) == ["book.pdf", "findings.md", "read_p22.md"]
    assert len(corpus.docs(workspace=str(tmp_path))) == 3


def test_a_whole_card_ends_with_only_what_it_handed_over(state, monkeypatch):
    con, run, root = state
    monkeypatch.setattr(corpus, "extract_pages", lambda p, meta=None: [PAGE + os.path.basename(p)])
    _accept(con, run, root, "scan", {"notes.md": "own\n", "reports/read.md": "transcribed\n",
                                     "raw/book.pdf": "a scan\n", "work/ocr/p0001.txt": "one page\n"})
    workflow.settle_done_tasks(con, run_id=run["id"])
    report.index_run(con, run)

    held = {Path(doc["orig_path"]).name for doc in corpus.docs(workspace=run["workspace"])}
    assert {"notes.md", "read.md", "book.pdf"} <= held
    assert "p0001.txt" not in held


def test_closing_a_node_does_not_offer_a_working_file(state, monkeypatch):
    con, run, root = state
    _accept(con, run, root, "scan", {"notes.md": "own\n", "work/ocr/p0001.txt": "one page\n"})
    workflow.settle_done_tasks(con, run_id=run["id"])
    asked = []
    monkeypatch.setattr(report, "doc_id", lambda _run, path: asked.append(Path(path).name) or "c" * 12)

    found = report.index_run(con, run)

    assert "notes.md" in asked and "p0001.txt" not in asked
    assert "p0001.txt" not in {Path(path).name for path in found}


def test_a_working_file_the_card_indexed_itself_keeps_its_id(state, monkeypatch):
    con, run, root = state
    _, paths = _accept(con, run, root, "kept", {"notes.md": "own\n", "work/table.md": PAGE})
    workflow.settle_done_tasks(con, run_id=run["id"])
    table = next(path for rel, path in paths.items() if rel.endswith("work/table.md"))
    doc, _pages = corpus.ingest(str(table), workspace=run["workspace"])
    monkeypatch.setattr(report, "doc_id", lambda _run, path: None)

    assert report.index_run(con, run)[str(Path(table).resolve())] == doc
