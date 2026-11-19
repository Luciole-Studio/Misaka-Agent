"""The corpus reads DjVu: its text layer, or a rendering through the PDF path.

2026-09-18 (B10): CADAL and Wikimedia carry most scanned Chinese classics as DjVu. download_file
refused the format, the Sister fetched it by hand with bash, and the corpus then skipped it at
submission (``index_skipped``). The machine already had djvulibre installed."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from misaka.core.documents import index as corpus

TOOLS = ("cjb2", "djvm", "djvused", "djvutxt")


def build_djvu(directory, words_per_page=60, pages=2):
    """A tiny multi-page DjVu with a hidden text layer, made with djvulibre itself."""
    if any(shutil.which(tool) is None for tool in TOOLS):
        pytest.skip("djvulibre is not installed")
    parts = []
    for n in range(1, pages + 1):
        rows = ["0" * 16 for _ in range(16)]
        rows[n] = "1" * 16
        pbm = directory / f"p{n}.pbm"
        pbm.write_text("P1\n16 16\n" + "\n".join(" ".join(r) for r in rows) + "\n", encoding="ascii")
        part = directory / f"p{n}.djvu"
        subprocess.run(["cjb2", str(pbm), str(part)], check=True, capture_output=True)
        parts.append(str(part))
    book = directory / "book.djvu"
    subprocess.run(["djvm", "-c", str(book), *parts], check=True, capture_output=True)
    script = "".join(
        f'select {n}; set-txt\n(page 0 0 16 16 "page {n} ' + " ".join(f"w{i}" for i in range(words_per_page)) + '")\n.\n'
        for n in range(1, pages + 1))
    dsed = directory / "text.dsed"
    dsed.write_text(script, encoding="ascii")
    subprocess.run(["djvused", str(book), "-f", str(dsed), "-s"], check=True, capture_output=True)
    return book


def test_djvu_is_a_format_the_corpus_reads_and_sweeps():
    assert ".djvu" in corpus.SCAN_SUFFIXES
    assert corpus._extractor("scan.djvu") is corpus._djvu_pages


def test_a_djvu_text_layer_comes_out_one_page_per_form_feed(tmp_path):
    book = build_djvu(tmp_path)
    pages = corpus.extract_pages(str(book))
    assert len(pages) == 2
    assert pages[0].startswith("page 1 w0 w1") and pages[1].startswith("page 2 w0 w1")


def test_a_thin_text_layer_is_kept_rather_than_replaced_by_an_empty_rendering(tmp_path):
    """A rendering that reads nothing must not overwrite the words the file already had."""
    book = build_djvu(tmp_path, words_per_page=1)
    meta = {}
    pages = corpus._djvu_pages(str(book), meta)
    assert [p.strip() for p in pages] == ["page 1 w0", "page 2 w0"]


def test_a_page_without_hidden_text_keeps_the_pages_after_it_in_place(tmp_path):
    """2026-10-07: ``djvutxt`` prints nothing at all -- not even the form feed -- for a page with
    no hidden text, so every page after a blank or a plate was stored one page early."""
    book = build_djvu(tmp_path, pages=3)
    subprocess.run(["djvused", str(book), "-e", "select 1; remove-txt", "-s"], check=True, capture_output=True)
    pages = corpus._djvu_pages(str(book), {})
    assert len(pages) == 3 and not pages[0].strip()
    assert pages[1].startswith("page 2 w0") and pages[2].startswith("page 3 w0")


def test_a_djvu_stored_out_of_step_is_renumbered_when_ingested_again(tmp_path):
    """Every stored page was full -- djvutxt had dropped the empty one -- so only the count shows it."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    book = build_djvu(workspace, pages=3)
    subprocess.run(["djvused", str(book), "-e", "select 1; remove-txt", "-s"], check=True, capture_output=True)
    doc_id, _ = corpus.ingest(str(book), workspace=str(workspace), with_tree=False)
    ddir = corpus.resolve_doc(doc_id, workspace=str(workspace))
    meta = json.loads((Path(ddir) / "meta.json").read_text(encoding="utf-8"))
    for n, text in ((1, corpus.read_page(doc_id, 2, workspace=str(workspace))),
                    (2, corpus.read_page(doc_id, 3, workspace=str(workspace)))):
        (Path(ddir) / "pages" / f"p{n:04d}.txt").write_text(text, encoding="utf-8")
    (Path(ddir) / "pages" / "p0003.txt").unlink()
    meta.update(pages=2)
    meta.pop("extract_version")
    meta.pop("unread_pages", None)
    (Path(ddir) / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    _, count = corpus.ingest(str(book), workspace=str(workspace), with_tree=False)
    assert count == 3
    assert corpus.read_page(doc_id, 2, workspace=str(workspace)).startswith("page 2 w0")


def test_without_djvulibre_the_reader_says_what_to_install(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus.shutil, "which", lambda name: None)
    meta = {}
    assert corpus._djvu_pages(str(tmp_path / "missing.djvu"), meta) == []
    assert "djvulibre" in meta["djvu_error"]


def test_a_broken_djvu_reports_the_tool_error(tmp_path):
    if shutil.which("djvused") is None:
        pytest.skip("djvulibre is not installed")
    broken = tmp_path / "broken.djvu"
    broken.write_bytes(b"AT&TFORM" + b"\x00" * 32)
    meta = {}
    pages = corpus._djvu_pages(str(broken), meta)
    assert not corpus._has_text_layer(pages)
    assert meta.get("djvu_error"), "djvused names the damage"


def test_a_downloaded_djvu_is_indexed_on_arrival(tmp_path, monkeypatch):
    """download_file indexes whatever SCAN_SUFFIXES names; a DjVu now goes in like a PDF."""
    monkeypatch.setenv("MISAKA_PAGEINDEX", str(tmp_path / "corpus"))
    workspace = tmp_path / "ws"
    (workspace / "downloads").mkdir(parents=True)
    book = build_djvu(workspace / "downloads")
    doc_id, count = corpus.ingest(str(book), workspace=str(workspace))
    assert doc_id and count == 2
