"""Pages of vertical text are read down their columns.

``pdftotext -layout`` reads such a page across its columns, one character at a time. In a real
run two Japanese papers and a gazetteer could be neither read nor searched (2026-11-19)."""
import shutil

import pytest
from PyPDF2 import PdfWriter
from PyPDF2.generic import DecodedStreamObject, DictionaryObject, NameObject

from misaka.core.documents import index as corpus
from misaka.core.documents import vertical_text

SENTENCE = "Thecolumnwasstillshortoftheriverandthewagonsafulldaybehind"
ROWS, COLUMNS = 30, 16

needs_pdftotext = pytest.mark.skipif(not shutil.which("pdftotext"), reason="pdftotext reads the text layer here")


def pdf(path, cells, pages=20):
    """A PDF whose every page prints ``cells(page)``: ``(x, y, text)`` rows, in that order."""
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica")}))
    for number in range(1, pages + 1):
        writer.add_blank_page(width=420, height=595)
        page = writer.pages[-1]
        stream = DecodedStreamObject()
        stream.set_data("".join(f"BT /F1 10 Tf {x} {y} Td ({text}) Tj ET " for x, y, text in cells(number)).encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    with open(path, "wb") as out:
        writer.write(out)
    return str(path)


def columns_of_text(number):
    """One character a cell, down each column, the columns right to left."""
    text = (SENTENCE * 12)[:ROWS * COLUMNS]
    return [(380 - 22 * (at // ROWS), 560 - 17 * (at % ROWS), character) for at, character in enumerate(text)]


def columns_of_digits(number):
    """A table of one-digit cells, written column by column."""
    return [(40 + 22 * column, 560 - 17 * row, str((row * 7 + column * 3 + number) % 10))
            for column in range(COLUMNS) for row in range(ROWS)]


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


@needs_pdftotext
def test_a_page_set_in_columns_is_read_down_them(workspace):
    path = pdf(workspace / "gazette.pdf", columns_of_text)

    doc_id, count = corpus.ingest(path, workspace=str(workspace), with_tree=False)

    assert count == 20
    page = (workspace / ".pageindex" / doc_id / "pages" / "p0003.txt").read_text(encoding="utf-8")
    assert SENTENCE + SENTENCE[:20] in page
    hits = corpus.search_literal("shortoftheriver", workspace=str(workspace))
    assert sorted({hit["page"] for hit in hits}) == list(range(1, 21))


@needs_pdftotext
def test_a_table_of_single_digits_keeps_its_rows(workspace):
    path = pdf(workspace / "returns.pdf", columns_of_digits)

    doc_id, _count = corpus.ingest(path, workspace=str(workspace), with_tree=False)

    page = (workspace / ".pageindex" / doc_id / "pages" / "p0001.txt").read_text(encoding="utf-8")
    assert page.splitlines()[0].split() == [str((column * 3 + 1) % 10) for column in range(COLUMNS)]


def test_a_page_cut_across_is_told_from_prose_and_from_a_short_page():
    cut = "\n".join("   ".join("abcdefghijklmnop") for _row in range(20))
    prose = "The column was still short of the river and the wagons a full day behind. " * 5

    assert vertical_text.cut_across(cut, 200)
    assert not vertical_text.cut_across(prose, 200)
    assert not vertical_text.cut_across("a b c d e f", 200)


def test_columns_are_joined_and_a_line_that_is_not_one_is_kept():
    raw = "Gazette 12\n" + "\n".join("洛寗縣志卷三") + "\n1917\n" + "\n".join("學務")

    assert vertical_text.in_reading_order(raw) == "Gazette 12\n洛寗縣志卷三\n1917\n學務"


def test_text_that_is_not_in_columns_is_left_alone():
    assert vertical_text.in_reading_order("The column was still short\nof the river\na\nb") is None
    assert vertical_text.in_reading_order("\n".join("1852963074" * 3)) is None
