"""One renderer, dispatched on suffix, guarded before any parser sees the bytes.

The package is the single answer to "what does this Office file say", used by both
``core/tools/read.py`` (a file in the working directory) and ``documents/index.py`` (a
document in the corpus). That is the whole point of it being one function: the text a model
reads in the working directory is the text it later cites out of the corpus, byte for byte,
so a quotation copied from a ``read`` verifies against the indexed document.

A suffix with no renderer is refused by name, the way ``index.py:_extractor`` refuses one:
a model told only "no" hands the same file back.
"""
from __future__ import annotations

import pytest

from misaka.core.documents import office


def test_the_suffix_table_and_the_dispatch_agree():
    """``read.py`` tests membership in ``SUFFIXES`` and then calls ``render``; a suffix in
    one and not the other is a file that routes to Office and then fails to render."""
    for suffix in office.SUFFIXES:
        assert office.format_of(f"x{suffix}") is not None


def test_format_of_normalises_case_and_macro_workbooks():
    assert office.format_of("Book.XLSX") == "xlsx"
    assert office.format_of("macros.xlsm") == "xlsx"
    assert office.format_of("data.csv") == "csv"
    assert office.format_of("data.tsv") == "csv"


def test_the_macro_bearing_variants_read_as_their_plain_format():
    """A .docm is a .docx that carries macros. The macros are not text and the body reads
    identically, so it is the same renderer -- the same reason .xlsm is an xlsx."""
    assert office.format_of("report.docm") == "docx"
    assert office.format_of("deck.pptm") == "pptx"
    assert office.format_of("Paper.DOCX") == "docx"


def test_a_format_this_package_does_not_read_is_not_claimed():
    """A suffix listed before its renderer exists routes the file here and then crashes on
    it, which is worse than the refusal that names what this reads. Legacy .doc/.xls/.ppt
    are OLE rather than zip and go through LibreOffice first (the plan's W22-I); .pdf is
    pageindex's."""
    assert office.format_of("old.doc") is None
    assert office.format_of("old.ppt") is None
    assert office.format_of("scan.pdf") is None
    assert office.format_of("book.epub") is None
    assert office.format_of("notes") is None


def test_rendering_an_unknown_suffix_names_what_is_read(tmp_path):
    path = tmp_path / "notes.rtf"
    path.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        office.render(str(path))
    message = str(caught.value)
    assert "notes.rtf" in message
    assert ".xlsx" in message and ".csv" in message


def test_a_zip_format_is_prechecked_before_the_parser_runs(tmp_path, monkeypatch):
    """The bound has to be taken before openpyxl reads the package, so the precheck runs
    even when the renderer would have failed on the same file anyway."""
    reached = []
    monkeypatch.setattr(office._xlsx, "render",
                        lambda path, **kw: reached.append(path) or "")
    path = tmp_path / "fake.xlsx"
    path.write_bytes(b"not a zip")
    with pytest.raises(ValueError) as caught:
        office.render(str(path))
    assert "not a zip archive" in str(caught.value)
    assert reached == []


def test_csv_is_not_prechecked(tmp_path, monkeypatch):
    """A .csv is not a package: running a zip precheck on it would refuse every csv."""
    seen = []
    monkeypatch.setattr(office._zip, "precheck", lambda path: seen.append(path))
    path = tmp_path / "rows.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    office.render(str(path))
    assert seen == []


def test_cell_range_reaches_only_the_workbook_renderer(tmp_path, monkeypatch):
    """``cell_range`` names a sheet and a range. A csv has neither, and silently dropping
    the argument reads to the model as a range it asked for and did not get."""
    monkeypatch.setattr(office._zip, "precheck", lambda path: None)
    got = {}
    monkeypatch.setattr(office._xlsx, "render",
                        lambda path, *, cell_range=None, meta=None:
                        got.setdefault("range", cell_range) or "")
    book = tmp_path / "b.xlsx"
    book.write_bytes(b"")
    office.render(str(book), cell_range="Sheet1!A1:B2")
    assert got["range"] == "Sheet1!A1:B2"

    rows = tmp_path / "rows.csv"
    rows.write_text("a,b\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        office.render(str(rows), cell_range="Sheet1!A1:B2")
    assert "cell_range" in str(caught.value)
