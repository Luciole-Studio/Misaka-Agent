"""A PDF's outline, read by the real PageIndex reader from a real file.

Every other document test stands something in for the reader, so nothing ran it: 2,540 tests
entered it 0 times (2026-11-18). These build a small PDF and index it."""
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from PyPDF2 import PdfWriter
from PyPDF2.generic import DecodedStreamObject, DictionaryObject, NameObject

from misaka.core.documents import index as corpus
from misaka.core.documents import mineru, outline, pdf_outline
from misaka.core.documents.wiring import documents
from misaka.core.wiring import ToolCollector

PROSE = "The column was still short of the river and the wagons a full day behind."
PAGES = 24
BOOKMARKS = [("Part One", 0, None), ("Chapter 1 The Crossing", 2, 0), ("Chapter 2 The Siege", 7, 0),
             ("Part Two", 12, None), ("Chapter 3 At the Passes", 13, 3), ("Chapter 4 The Retreat", 18, 3),
             ("Index", 22, None)]


def book(path, bookmarks=()):
    """A 24-page PDF of running prose, with ``bookmarks`` as ``(title, page, parent)`` rows."""
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica")}))
    for number in range(1, PAGES + 1):
        writer.add_blank_page(width=420, height=595)
        page = writer.pages[-1]
        lines = "".join(f"({PROSE} {number}.{line}) Tj T* " for line in range(1, 31))
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 9 Tf 14 TL 36 550 Td {lines}ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    made = []
    for title, at, parent in bookmarks:
        made.append(writer.add_outline_item(title, at, parent=None if parent is None else made[parent]))
    with open(path, "wb") as out:
        writer.write(out)
    return str(path)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    # Where MinerU is installed, a PDF PageIndex finds nothing in is handed to it for headings:
    # 34 s for the 24 pages below (2026-11-19). That reader has its own tests.
    monkeypatch.setattr(mineru, "headings", lambda path, count: {})
    root = tmp_path / "ws"
    root.mkdir()
    return root


def stored(workspace, doc_id, name):
    return json.loads(Path(corpus.resolve_doc(doc_id, workspace=str(workspace)), name).read_text(encoding="utf-8"))


@pytest.fixture
def asked(monkeypatch):
    """The PDFs the real reader was called on, in order."""
    calls, real = [], pdf_outline.read

    def read(pdf, workers=None):
        calls.append(Path(pdf).name)
        return real(pdf, workers=workers)

    monkeypatch.setattr(pdf_outline, "read", read)
    return calls


async def doc_outline(workspace, doc_id):
    collector = ToolCollector()
    documents.register(collector)
    tool = next(t for t in collector.tools if t.name == "doc_outline")
    result = await tool.execute("call", {"doc_id": doc_id}, None, None, NS(cwd=str(workspace), model=None))
    return result["content"][0]["text"]


def test_a_pdf_with_bookmarks_is_outlined_by_them(workspace):
    doc_id, count = corpus.ingest(book(workspace / "campaign.pdf", BOOKMARKS), workspace=str(workspace))

    assert count == PAGES
    tree = stored(workspace, doc_id, "tree.json")
    assert [(node["title"], node["node_id"], node["start_index"], node["end_index"]) for node in tree] == [
        ("Part One", "0000", 1, 13), ("Part Two", "0003", 13, 23), ("Index", "0006", 23, 24)]
    assert [(node["title"], node["start_index"], node["end_index"]) for node in tree[0]["nodes"]] == [
        ("Chapter 1 The Crossing", 3, 8), ("Chapter 2 The Siege", 8, 13)]
    assert corpus.node_pages(doc_id, "0004", workspace=str(workspace)) == (14, 19)
    assert "- [0005] Chapter 4 The Retreat  p19-23" in corpus.tree_outline(doc_id, workspace=str(workspace))


def test_a_pdf_that_states_no_outline_has_none(workspace):
    doc_id, count = corpus.ingest(book(workspace / "prose.pdf"), workspace=str(workspace))

    assert count == PAGES
    assert corpus.tree_outline(doc_id, workspace=str(workspace)) is None


def test_the_corpus_keeps_where_an_outline_came_from(workspace):
    doc_id, _count = corpus.ingest(book(workspace / "campaign.pdf", BOOKMARKS), workspace=str(workspace))

    assert stored(workspace, doc_id, "meta.json")["outline"] == {
        "status": "built", "version": outline.VERSION, "source": "bookmarks"}
    assert corpus.outline_note(doc_id, workspace=str(workspace)) == "outline from the bookmarks the file carries"


async def test_doc_outline_says_where_the_outline_came_from(workspace):
    doc_id, _count = corpus.ingest(book(workspace / "campaign.pdf", BOOKMARKS), workspace=str(workspace))

    text = await doc_outline(workspace, doc_id)

    said, _, quoted = text.partition("<")
    assert "Outline from the bookmarks the file carries.\n" in said
    assert "- [0001] Chapter 1 The Crossing  p3-8" in quoted


def test_a_pdf_with_no_outline_is_asked_once(workspace, asked):
    path = book(workspace / "prose.pdf")
    doc_id, _count = corpus.ingest(path, workspace=str(workspace))
    corpus.ingest(path, workspace=str(workspace))

    assert asked == ["prose.pdf"]
    assert stored(workspace, doc_id, "meta.json")["outline"] == {"status": "none", "version": outline.VERSION}
    assert corpus.outline_note(doc_id, workspace=str(workspace)) == (
        "no outline: the file states none and none could be read from its pages")


async def test_doc_outline_says_why_there_is_none(workspace):
    doc_id, _count = corpus.ingest(book(workspace / "prose.pdf"), workspace=str(workspace))

    text = await doc_outline(workspace, doc_id)

    assert "No outline: the file states none and none could be read from its pages.\n" in text.partition("<")[0]
    assert "p24 " in text


def test_a_pdf_the_reader_fails_on_keeps_its_pages_and_the_reason(workspace, monkeypatch):
    calls = []

    def fail(pdf, workers=None):
        calls.append(pdf)
        raise pdf_outline.PageIndexFailed("TypeError: bidirectional() argument must be a unicode character, not str")

    monkeypatch.setattr(pdf_outline, "read", fail)
    path = book(workspace / "campaign.pdf", BOOKMARKS)

    doc_id, count = corpus.ingest(path, workspace=str(workspace))
    corpus.ingest(path, workspace=str(workspace))

    assert count == PAGES and len(calls) == 1
    assert stored(workspace, doc_id, "meta.json")["outline"] == {
        "status": "failed", "version": outline.VERSION,
        "reason": "TypeError: bidirectional() argument must be a unicode character, not str"}
    assert corpus.outline_note(doc_id, workspace=str(workspace)) == (
        "no outline: the outline reader failed on this file (TypeError), which says nothing about "
        "how the document is organised")


def test_an_attempt_by_older_readers_is_made_again(workspace, monkeypatch):
    path = book(workspace / "campaign.pdf", BOOKMARKS)
    monkeypatch.setattr(pdf_outline, "read", lambda pdf, workers=None: {"structure": []})
    doc_id, _count = corpus.ingest(path, workspace=str(workspace))
    assert corpus.tree_outline(doc_id, workspace=str(workspace)) is None
    monkeypatch.undo()
    monkeypatch.setattr(mineru, "headings", lambda path, count: {})

    monkeypatch.setattr(outline, "VERSION", outline.VERSION + 1)
    corpus.ingest(path, workspace=str(workspace))

    assert "Chapter 3 At the Passes" in corpus.tree_outline(doc_id, workspace=str(workspace))
    assert stored(workspace, doc_id, "meta.json")["outline"]["version"] == outline.VERSION


def test_a_document_settled_before_attempts_were_recorded_is_asked_again(workspace, asked):
    path = book(workspace / "campaign.pdf", BOOKMARKS)
    doc_id, _count = corpus.ingest(path, workspace=str(workspace), with_tree=False)
    meta = Path(corpus.resolve_doc(doc_id, workspace=str(workspace)), "meta.json")
    meta.write_text(json.dumps({**json.loads(meta.read_text(encoding="utf-8")), "tree_attempted_version": 1}),
                    encoding="utf-8")

    corpus.ingest(path, workspace=str(workspace))

    assert asked == ["campaign.pdf"]
    assert "Part Two" in corpus.tree_outline(doc_id, workspace=str(workspace))


def test_an_install_that_cannot_read_outlines_leaves_the_question_open(workspace, monkeypatch, capsys):
    def missing(pdf, workers=None):
        raise pdf_outline.PageIndexUnavailable("PageIndex cannot run in this install")

    monkeypatch.setattr(pdf_outline, "read", missing)
    monkeypatch.setattr(pdf_outline, "available", lambda: False)
    monkeypatch.setattr(outline, "_warned_unavailable", False)
    path = book(workspace / "campaign.pdf", BOOKMARKS)
    doc_id, _count = corpus.ingest(path, workspace=str(workspace))

    assert "outline" not in stored(workspace, doc_id, "meta.json")
    assert "PageIndex's packages are missing" in capsys.readouterr().err
    assert "this install cannot read PDF outlines" in corpus.outline_note(doc_id, workspace=str(workspace))

    monkeypatch.undo()
    monkeypatch.setattr(mineru, "headings", lambda path, count: {})
    corpus.ingest(path, workspace=str(workspace))
    assert "Part One" in corpus.tree_outline(doc_id, workspace=str(workspace))


def test_a_format_no_reader_takes_is_not_asked(workspace, asked):
    page = workspace / "notes.html"
    page.write_text("<html><body>" + "".join(f"<h2>Section {n}</h2><p>{PROSE * 40}</p>" for n in range(60))
                    + "</body></html>", encoding="utf-8")

    doc_id, _count = corpus.ingest(str(page), workspace=str(workspace))

    assert asked == []
    assert "outline" not in stored(workspace, doc_id, "meta.json")
    assert corpus.structure(doc_id, workspace=str(workspace))["mode"] == "pages only, no outline: this format has none"


def test_a_pdf_the_reader_takes_too_long_over_keeps_its_pages(workspace, monkeypatch):
    monkeypatch.setattr(pdf_outline, "TIMEOUT", 0.01)

    doc_id, count = corpus.ingest(book(workspace / "campaign.pdf", BOOKMARKS), workspace=str(workspace))

    assert count == PAGES
    assert stored(workspace, doc_id, "meta.json")["outline"] == {
        "status": "failed", "version": outline.VERSION, "reason": "timeout: no outline within 0.01 s"}


def test_a_file_in_the_workspace_named_like_a_module_is_not_what_the_reader_imports(workspace, monkeypatch):
    path = book(workspace / "campaign.pdf", BOOKMARKS)
    doc_id, _count = corpus.ingest(path, workspace=str(workspace), with_tree=False)
    (workspace / "json.py").write_text("raise SystemExit('the workspace was imported')", encoding="utf-8")
    monkeypatch.chdir(workspace)

    corpus.ingest(path, workspace=str(workspace))

    assert stored(workspace, doc_id, "meta.json")["outline"]["status"] == "built"
