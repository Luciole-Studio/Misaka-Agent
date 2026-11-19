"""Plain-text and Markdown documents get an outline read from their own headings.

2026-09-23: a research round downloaded 800-1000-page transcriptions (Roederer's Oeuvres, Oman,
Cary's Itinerary as text) and none of them had a tree -- PageIndex reads PDFs only -- so the
Sisters grep'd. The text reader is MISAKA's own (documents/text_outline), outside the vendored
PageIndex slice, and writes the same tree.json shape."""
import json
from pathlib import Path

import pytest

from misaka.core.documents import index as corpus
from misaka.core.documents import outline, text_outline

PROSE = ("The morning of the seventh found the column still short of the river, the wagons a full "
         "day behind and the bread three days old. ") * 4


def gutenberg_pages():
    """Pages the way the corpus cuts them: a heading stands alone between blank lines."""
    running = "THE HISTORY OF THE WAR"
    pages = [
        f"{running}\n\nCONTENTS\n\nBook I. The Opening\nBook II. The March\n\n{PROSE}",
        f"{running}\n\nBOOK I.\n\nTHE OPENING OF THE CAMPAIGN\n\n{PROSE}",
        f"{running}\n\nCHAPTER I.\n\nTHE CROSSING OF THE RIVER\n\n{PROSE}\n\n{PROSE}",
        f"{running}\n\n{PROSE}",
        f"{running}\n\nCHAPTER II. THE SIEGE\n\n{PROSE}",
        f"{running}\n\nBOOK II.\n\nTHE MARCH\n\n{PROSE}",
        f"{running}\n\nCHAPTER III.\n\nAT THE PASSES\n\n{PROSE}\n\nHe wrote: NO.\n\n{PROSE}",
        f"{running}\n\n{PROSE}",
    ]
    return pages


def test_a_transcription_yields_nested_books_and_chapters():
    tree = text_outline.build_text_tree(gutenberg_pages())
    titles = [(n["title"], n["start_index"], n["end_index"]) for n in tree]
    assert titles == [("CONTENTS", 1, 1), ("BOOK I THE OPENING OF THE CAMPAIGN", 2, 5), ("BOOK II THE MARCH", 6, 8)]
    book_one = tree[1]["nodes"]
    assert [(n["title"], n["start_index"], n["end_index"]) for n in book_one] == [
        ("CHAPTER I THE CROSSING OF THE RIVER", 3, 4), ("CHAPTER II. THE SIEGE", 5, 5)]
    assert [n["node_id"] for n in tree] == ["0000", "0001", "0004"]          # depth-first, like PageIndex
    assert book_one[0]["node_id"] == "0002" and "nodes" not in book_one[0]
    flat = json.dumps(tree)
    assert "THE HISTORY OF THE WAR" not in flat                              # the running head is not a section
    assert "NO." not in flat                                                 # a shouted word in prose is not either


def test_markdown_levels_follow_the_hashes():
    pages = ["# Guide\n\nintro text\n\n## Setup\n\nsteps\n\n### Keys\n\nmore",
             "## Usage\n\ntext\n\n# Appendix\n\nend"]
    tree = text_outline.build_text_tree(pages, markdown=True)
    assert [n["title"] for n in tree] == ["Guide", "Appendix"]
    assert [n["title"] for n in tree[0]["nodes"]] == ["Setup", "Usage"]
    assert tree[0]["nodes"][0]["nodes"][0]["title"] == "Keys"
    assert (tree[0]["start_index"], tree[0]["end_index"], tree[1]["start_index"]) == (1, 1, 2)


@pytest.mark.parametrize("pages", [
    [PROSE] * 30,                                                            # no headings at all
    [f"{PROSE}\n\nCHAPTER I.\n\n{PROSE}", PROSE],                            # one heading is not an outline
    [f"SHOUTED LINE NUMBER {i}\n\n{PROSE}\n\nAND ANOTHER ONE {i}\n\n{PROSE}" for i in range(10)],  # caps everywhere
])
def test_documents_without_structure_get_no_tree(pages):
    assert text_outline.build_text_tree(pages) is None


def _book(chapters, words=2400):
    body = ("Long paragraphs of narrative follow here so that every chapter fills more than one corpus page. " * (words // 15))
    return "".join(f"\n\nCHAPTER {roman}.\n\n{body}" for roman in
                   ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"][:chapters])


def test_ingest_builds_a_tree_for_a_long_text_and_the_tools_read_it(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    book = workspace / "history.txt"
    book.write_text("A HISTORY\n" + _book(12), encoding="utf-8")
    doc_id, count = corpus.ingest(str(book), workspace=str(workspace))
    assert count >= outline.MIN_PAGES
    shown = corpus.tree_outline(doc_id, workspace=str(workspace))
    assert shown and "CHAPTER XII" in shown
    assert corpus.outline_note(doc_id, workspace=str(workspace)) == (
        "outline from the headings the text states on lines of their own")
    start, end = corpus.node_pages(doc_id, "0001", workspace=str(workspace))     # the title line is not a section; 0000 is CHAPTER I
    assert 1 <= start <= end <= count
    assert "CHAPTER II." in str(corpus.read_pages(doc_id, start, end, workspace=str(workspace)))


def test_re_ingest_backfills_a_text_document_indexed_without_a_tree(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    book = workspace / "memoirs.txt"
    book.write_text(_book(10), encoding="utf-8")
    doc_id, _count = corpus.ingest(str(book), workspace=str(workspace), with_tree=False)
    assert corpus.tree_outline(doc_id, workspace=str(workspace)) is None
    again, _count = corpus.ingest(str(book), workspace=str(workspace))
    assert again == doc_id
    assert "CHAPTER X" in corpus.tree_outline(doc_id, workspace=str(workspace))


def test_a_long_text_without_headings_is_settled_as_none_found(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    plain = workspace / "plain.txt"
    plain.write_text(("\n\n".join([PROSE] * 160)), encoding="utf-8")
    doc_id, count = corpus.ingest(str(plain), workspace=str(workspace))
    assert count >= outline.MIN_PAGES
    assert corpus.tree_outline(doc_id, workspace=str(workspace)) is None
    meta = json.loads(Path(corpus.resolve_doc(doc_id, workspace=str(workspace)), "meta.json").read_text(encoding="utf-8"))
    assert meta["outline"] == {"status": "none", "version": outline.VERSION}
