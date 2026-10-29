"""Which pages of a PDF go through OCR, and where what OCR read is put.

2026-10-07: the corpus decided OCR for a whole file at once -- none of it when a fifth of its
pages carried text. A scanned book with a typeset introduction, a document collection whose
facsimiles follow the editor's pages, and a scan stamped "Downloaded from ..." on every page all
had their scanned pages stored empty, with nothing to say so. And when OCR did run, the sidecar's
one placeholder for a run of skipped pages was stored as a page: a 14-page PDF with two typeset
pages became 13 pages, the typeset text replaced by "[OCR skipped on page(s) 1-2]" and every
scanned page numbered one too low -- so doc_verify located quotations on the wrong page."""
import ctypes
import json
import os
import shutil
import subprocess

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw, ImageFont
from pypdfium2 import raw

from misaka.core.documents import index as corpus
from misaka.core.documents.wiring import documents as tools

_real_extract_pages = corpus.extract_pages

TYPESET = "Typeset page {n}. The editor's introduction to the documents that follow."
PLACES = ("Aachen", "Bremen", "Cologne", "Dresden", "Erfurt", "Frankfurt", "Gotha", "Hanover", "Jena", "Kassel")


def typeset(n):
    """A typeset page: its own words, under a first line every typeset page shares."""
    return TYPESET.format(n=n) + f"\nThe letters from {PLACES[n % len(PLACES)]} are printed in full.\n"
SCANNED = "SCANNED PAGE {n} report of the district magistrate on the harvest"
STAMP = "Downloaded from archive.example.org on 2026-10-07, page {n}"


# -- what the file says about its pages ------------------------------------------------------------

def test_a_stamp_on_every_page_is_not_a_text_layer():
    pages = [STAMP.format(n=n) + "\n" for n in range(1, 6)]
    assert corpus._route(pages) == {n: corpus.ROUTE_EMPTY for n in range(1, 6)}


def test_a_statistical_table_is_not_taken_for_a_running_head():
    """Its numeric rows all read "# # # #": stripping while they matched emptied every page of an
    appendix, and the whole table went to OCR as pages with no text layer."""
    pages = ["\n".join(f"{1850 + 30 * n + r}   {100 + r}   {200 + 2 * r}   {300 + 3 * r}" for r in range(30))
             for n in range(4)]
    assert corpus._route(pages) == {}


def test_a_fifth_of_typeset_pages_no_longer_hides_the_scanned_rest():
    pages = [typeset(n) for n in (1, 2, 3)] + [""] * 7
    assert sorted(corpus._route(pages)) == list(range(4, 11))


def test_a_blank_page_is_not_sent_to_ocr():
    pages = [typeset(1), "", typeset(3)]
    assert corpus._route(pages, lambda numbers: {2: (0.0, False)}) == {}
    assert corpus._route(pages, lambda numbers: {2: (0.0, True)}) == {2: corpus.ROUTE_EMPTY}


def test_a_garbled_layer_is_sent_to_ocr():
    pages = ["" * 20 + " ok", typeset(2)]
    assert corpus._route(pages) == {1: corpus.ROUTE_GARBLED}


def test_an_image_with_a_line_of_text_is_sent_to_ocr_and_a_page_of_prose_is_not():
    heading = "Document 12: a letter from the governor, 1932"
    prose = "A page of a born-digital book with a figure on it. " * 6
    seen = {}

    def visuals(numbers):
        seen["asked"] = numbers
        return {n: (0.9, True) for n in numbers}

    routed = corpus._route([heading, prose], visuals)
    assert routed == {1: corpus.ROUTE_IMAGE}
    assert seen["asked"] == [1], "only pages short of SPARSE_TEXT are measured"
    assert corpus._route([heading], lambda numbers: {1: (0.05, True)}) == {}


# -- reading ocrmypdf's sidecar --------------------------------------------------------------------

def test_a_placeholder_stands_for_the_run_of_pages_it_names():
    text = "[OCR skipped on page(s) 1-2]\fscan three\fscan four"
    assert corpus._sidecar_pages(text, 4) == {3: "scan three", 4: "scan four"}
    text = "one\f[OCR skipped on page(s) 2-3]\ffour"
    assert corpus._sidecar_pages(text, 4) == {1: "one", 4: "four"}


def test_a_sidecar_that_does_not_line_up_is_refused():
    assert corpus._sidecar_pages("[OCR skipped on page(s) 2]\fone", 2) is None
    assert corpus._sidecar_pages("one\ftwo", 3) is None


def test_a_closing_form_feed_and_an_empty_last_page_both_read():
    assert corpus._sidecar_pages("one\ftwo\f", 2) == {1: "one", 2: "two"}
    assert corpus._sidecar_pages("one\f", 2) == {1: "one", 2: ""}


def test_page_spec():
    assert corpus._page_spec([7, 1, 2, 3, 9, 10]) == "1-3,7,9-10"


# -- putting what OCR read where it belongs ---------------------------------------------------------

def test_a_text_layer_keeps_its_words_and_ocr_is_added_after_them():
    pages = ["Plate 3. The caption the publisher typeset.", "", "" * 30]
    routed = {1: corpus.ROUTE_IMAGE, 2: corpus.ROUTE_EMPTY, 3: corpus.ROUTE_GARBLED}
    ocr = {1: "Plate 3. The caption read again by OCR", 2: "a scanned page read by OCR", 3: "the words a broken font hid"}
    merged, ocred, unread = corpus._merge_ocr(pages, ocr, routed, "unused")
    assert merged[0].startswith("Plate 3. The caption the publisher typeset.") and merged[0].endswith("read again by OCR")
    assert merged[1] == "a scanned page read by OCR"
    assert merged[2] == "the words a broken font hid", "a garbled layer is replaced"
    assert ocred == [1, 2, 3] and unread == {}


def test_a_page_ocr_could_not_read_is_named_with_the_reason():
    pages = ["", ""]
    routed = {1: corpus.ROUTE_EMPTY, 2: corpus.ROUTE_EMPTY}
    merged, ocred, unread = corpus._merge_ocr(pages, {1: "|~ ;' . , - = _"}, routed, corpus.OCR_MISSING)
    assert merged == ["", ""] and ocred == []
    assert unread[1].endswith("OCR found no text on it")
    assert unread[2].endswith(corpus.OCR_MISSING)


# -- the whole path, with a stand-in ocrmypdf -------------------------------------------------------

@pytest.fixture
def fake_ocrmypdf(monkeypatch):
    """ocrmypdf as it behaves: OCR the --pages it is given, one placeholder per run of the rest."""
    calls = []
    real_which = shutil.which

    def run(argv, **kwargs):
        if argv[0] != corpus.OCR_BINARY:
            raise AssertionError(f"unexpected command {argv}")
        calls.append(argv)
        count = calls_pages["count"]
        wanted = set(range(1, count + 1))
        if "--pages" in argv:
            wanted = {n for part in argv[argv.index("--pages") + 1].split(",")
                      for a, _, b in [part.partition("-")] for n in range(int(a), int(b or a) + 1)}
        entries, n = [], 1
        while n <= count:
            if n in wanted:
                entries.append(calls_pages.get("read", SCANNED).format(n=n)); n += 1
                continue
            start = n
            while n <= count and n not in wanted:
                n += 1
            entries.append(f"[OCR skipped on page(s) {start}-{n - 1}]" if n - 1 > start
                           else f"[OCR skipped on page(s) {start}]")
        with open(argv[argv.index("--sidecar") + 1], "w", encoding="utf-8") as f:
            f.write("\f".join(entries))
        return subprocess.CompletedProcess(argv, calls_pages.get("exit", 0), "", calls_pages.get("stderr", ""))

    calls_pages = {"count": 0}
    monkeypatch.setattr(corpus.shutil, "which", lambda name: f"/fake/{name}" if name == corpus.OCR_BINARY else real_which(name))
    monkeypatch.setattr(corpus.subprocess, "run", run)
    monkeypatch.setattr(corpus, "_ocr_version", lambda: (17, 12))
    return calls, calls_pages


def _layer(monkeypatch, pages):
    """A PDF whose text layer is ``pages``: a page with text is typeset, one without is a scan."""
    monkeypatch.setattr(corpus, "_pdf_text_layer", lambda p: list(pages))
    monkeypatch.setattr(corpus, "_pdf_visuals",
                        lambda p, numbers: {n: {"cover": 0.0 if pages[n - 1].strip() else 1.0, "marked": True}
                                            for n in numbers})


def test_two_typeset_pages_before_the_scans_keep_their_text_and_their_numbers(tmp_path, monkeypatch, fake_ocrmypdf):
    """The 14-page book that was stored as 13."""
    calls, state = fake_ocrmypdf
    state["count"] = 14
    _layer(monkeypatch, [typeset(1), typeset(2)] + [""] * 12)
    meta = {}
    pages = corpus._pdf_pages(str(tmp_path / "book.pdf"), meta)
    assert len(pages) == 14
    assert pages[0] == typeset(1) and pages[1] == typeset(2)
    assert all(pages[n - 1] == SCANNED.format(n=n) for n in range(3, 15))
    argv = calls[0]
    assert "--force-ocr" in argv and "--skip-text" not in argv
    assert argv[argv.index("--pages") + 1] == "3-14"
    assert meta == {"ocr": True, "ocr_pages": list(range(3, 15))}


@pytest.mark.skipif(os.name == "nt", reason="a read-only folder is a POSIX permission")
def test_a_book_in_a_read_only_folder_is_still_read(tmp_path, monkeypatch, fake_ocrmypdf):
    """OCR worked beside the source; with one routed page enough to OCR, a read-only library
    folder refused books it used to index."""
    _calls, state = fake_ocrmypdf
    state["count"] = 3
    _layer(monkeypatch, [typeset(1), typeset(2), ""])
    library = tmp_path / "library"
    library.mkdir()
    library.chmod(0o555)
    try:
        pages = corpus._pdf_pages(str(library / "book.pdf"), {})
    finally:
        library.chmod(0o755)
    assert pages[2] == SCANNED.format(n=3)


def test_an_ocrmypdf_older_than_11_7_reads_every_page_and_only_the_chosen_are_kept(tmp_path, monkeypatch, fake_ocrmypdf):
    calls, state = fake_ocrmypdf
    state["count"] = 3
    monkeypatch.setattr(corpus, "_ocr_version", lambda: (11, 6))
    _layer(monkeypatch, [typeset(1), "", typeset(3)])
    pages = corpus._pdf_pages(str(tmp_path / "book.pdf"), {})
    assert "--pages" not in calls[0]
    assert pages == [typeset(1), SCANNED.format(n=2), typeset(3)]


def test_a_failed_ocr_leaves_the_pages_named_unread_with_why(tmp_path, monkeypatch, fake_ocrmypdf):
    _, state = fake_ocrmypdf
    state.update(count=3, exit=2, stderr="tesseract: language data for chi_sim is not installed")
    _layer(monkeypatch, [typeset(1), "", typeset(3)])
    meta = {}
    corpus._pdf_pages(str(tmp_path / "book.pdf"), meta)
    assert "chi_sim is not installed" in meta["unread_pages"]["2"]


def test_without_ocrmypdf_the_scanned_pages_are_named_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus.shutil, "which", lambda name: None)
    _layer(monkeypatch, [typeset(n) for n in (1, 2, 3)] + [""] * 2)
    meta = {}
    pages = corpus._pdf_pages(str(tmp_path / "book.pdf"), meta)
    assert pages[3:] == ["", ""]
    assert meta == {"unread_pages": {"4": f"{corpus.ROUTE_EMPTY}; {corpus.OCR_MISSING}",
                                     "5": f"{corpus.ROUTE_EMPTY}; {corpus.OCR_MISSING}"}}


# -- the corpus: new documents, and documents an older extractor indexed ----------------------------

def _ingest(tmp_path, monkeypatch, name="book.pdf"):
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    source = workspace / name
    if not source.exists():
        source.write_bytes(b"%PDF-1.4\n% stand-in: the text layer is faked\n")
    doc_id, count = corpus.ingest(str(source), workspace=str(workspace), with_tree=False)
    ddir = corpus.resolve_doc(doc_id, workspace=str(workspace))
    with open(f"{ddir}/meta.json", encoding="utf-8") as f:
        return doc_id, count, ddir, json.load(f)


def test_ingest_records_unread_pages_and_the_extractor_but_not_the_ocr_error(tmp_path, monkeypatch, fake_ocrmypdf):
    _, state = fake_ocrmypdf
    state.update(count=3, exit=2, stderr="tesseract failed")
    _layer(monkeypatch, [typeset(1), "", typeset(3)])
    _, count, _, meta = _ingest(tmp_path, monkeypatch)
    assert count == 3
    assert meta["extract_version"] == corpus.EXTRACT_VERSION
    assert "2" in meta["unread_pages"] and "ocr_error" not in meta


def _as_version_1(tmp_path, monkeypatch, stored, **learned):
    """Index ``stored`` as version 1 of the extractor left it, then restore the real extractor."""
    monkeypatch.setattr(corpus, "extract_pages", lambda p, meta=None: (meta.update(learned), list(stored))[1])
    doc_id, _, ddir, meta = _ingest(tmp_path, monkeypatch)
    meta.pop("extract_version")
    with open(f"{ddir}/meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f)
    monkeypatch.setattr(corpus, "extract_pages", _real_extract_pages)
    return doc_id


def test_a_document_stored_out_of_step_is_renumbered_when_ingested_again(tmp_path, monkeypatch, fake_ocrmypdf):
    """What version 1 stored for a book like the 14-page one, put right by `doc add`/`scan` again."""
    calls, state = fake_ocrmypdf
    doc_id = _as_version_1(tmp_path, monkeypatch,
                           ["[OCR skipped on page(s) 1-2]", SCANNED.format(n=3), SCANNED.format(n=4)], ocr=True)
    state["count"] = 4
    _layer(monkeypatch, [typeset(1), typeset(2), "", ""])
    _, count, _, meta = _ingest(tmp_path, monkeypatch)
    ws = str(tmp_path / "ws")
    assert count == 4
    assert corpus.read_page(doc_id, 1, workspace=ws) == typeset(1)
    assert corpus.read_page(doc_id, 4, workspace=ws) == SCANNED.format(n=4)
    assert meta["reread"]["pages"] == [1, 2, 3, 4] and meta["reread"]["pages_before"] == 3
    assert meta["ocr_pages"] == [3, 4] and meta["extract_version"] == corpus.EXTRACT_VERSION
    _ingest(tmp_path, monkeypatch)
    assert len(calls) == 1, "a document already current is not read again"


def test_a_table_version_2_sent_to_ocr_goes_back_to_its_text_layer(tmp_path, monkeypatch, fake_ocrmypdf):
    """Version 2 took a statistical table's rows for running lines and OCR'd its pages."""
    calls, state = fake_ocrmypdf
    table = "\n".join(f"{1850 + r}   {100 + r}   {200 + 2 * r}" for r in range(30))
    doc_id = _as_version_1(tmp_path, monkeypatch, [typeset(1), table + "\n1850 l00 2OO", typeset(3)],
                           ocr=True, ocr_pages=[2])
    ddir = corpus.resolve_doc(doc_id, workspace=str(tmp_path / "ws"))
    with open(f"{ddir}/meta.json", encoding="utf-8") as f:
        meta = json.load(f)
    meta["extract_version"] = 2
    with open(f"{ddir}/meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f)
    state["count"] = 3
    _layer(monkeypatch, [typeset(1), table, typeset(3)])
    _, _count, _, meta = _ingest(tmp_path, monkeypatch)
    assert corpus.read_page(doc_id, 2, workspace=str(tmp_path / "ws")) == table
    assert not meta.get("ocr") and meta["reread"]["pages"] == [2] and calls == []


def test_a_reread_rewrites_only_the_pages_it_newly_read(tmp_path, monkeypatch, fake_ocrmypdf):
    """Same page count: the typeset pages keep their stored bytes, so a located quotation stays."""
    _, state = fake_ocrmypdf
    stored = [typeset(1) + "  \n", "", typeset(3) + "  \n"]
    doc_id = _as_version_1(tmp_path, monkeypatch, stored)
    state["count"] = 3
    _layer(monkeypatch, [typeset(1), "", typeset(3)])    # pdftotext spaces it differently now
    _, count, _, meta = _ingest(tmp_path, monkeypatch)
    ws = str(tmp_path / "ws")
    assert count == 3
    assert corpus.read_page(doc_id, 1, workspace=ws) == stored[0], "untouched, byte for byte"
    assert corpus.read_page(doc_id, 2, workspace=ws) == SCANNED.format(n=2)
    assert meta["reread"]["pages"] == [2] and meta["ocr_pages"] == [2]


def test_without_ocrmypdf_a_reread_waits_for_it(tmp_path, monkeypatch):
    stored = [typeset(1), "", typeset(3)]
    _as_version_1(tmp_path, monkeypatch, stored)
    monkeypatch.setattr(corpus.shutil, "which", lambda name: None)
    _layer(monkeypatch, stored)
    _, _, _, meta = _ingest(tmp_path, monkeypatch)
    assert "extract_version" not in meta, "read again once ocrmypdf is installed"
    assert meta["unread_pages"] == {"2": f"{corpus.ROUTE_EMPTY}; {corpus.OCR_MISSING}"}


# -- what the doc_* tools say about it --------------------------------------------------------------

def test_the_listing_and_the_reader_name_unread_and_reread_pages():
    row = {"pages": 10, "ocr": True, "ocr_pages": [4, 5], "orig_path": "/w/book.pdf",
           "unread_pages": {"6": f"{corpus.ROUTE_EMPTY}; {corpus.OCR_MISSING}",
                            "7": f"{corpus.ROUTE_EMPTY}; {corpus.OCR_FOUND_NOTHING}"},
           "reread": {"at": 1790000000, "pages": [4, 5], "pages_before": 9}}
    assert tools._ocr_badge(row) == " (OCR 2/10) (1 unread)", "a blank or a photograph is not unread"
    note = tools._ocr_note(row)
    assert "Pages 6 needed OCR and did not get it" in note and "Pages 7 hold no text OCR could read" in note
    notes = tools._page_notes(row, 4, 7)
    assert "Page 6 has no text here (no text layer; ocrmypdf is not installed" in notes
    assert "Page 7 has no text here (no text layer; OCR found no text on it)" in notes
    assert "doc_page_image" in notes and "Pages 4-5 were re-read" in notes
    # A plate or a chart OCR read is left out of the figures, so the reader names it here.
    assert "Pages 4-5 were read by OCR from the page image" in notes
    assert tools._page_notes(row, 1, 3) == ""


def test_an_unread_djvu_page_is_seen_with_doc_page_image():
    row = {"pages": 5, "ocr": True, "ocr_pages": [2], "orig_path": "/w/book.djvu",
           "unread_pages": {"3": f"{corpus.ROUTE_EMPTY}; {corpus.OCR_MISSING}"}}
    assert "doc_page_image" in tools._page_notes(row, 3, 3)


# -- the real thing, when this machine has it -------------------------------------------------------

def _typeset(pdf, page, line, y):
    obj = raw.FPDFPageObj_NewTextObj(pdf.raw, b"Helvetica", 12.0)
    data = (line + "\0").encode("utf-16-le")
    assert raw.FPDFText_SetText(obj, (ctypes.c_ushort * (len(data) // 2)).from_buffer_copy(data))
    raw.FPDFPageObj_Transform(obj, 1, 0, 0, 1, 72, y)
    raw.FPDFPage_InsertObject(page.raw, obj)


def build_pdf(path, kinds, stamp=False):
    """``T`` a typeset page with a real text layer, ``S`` a scan: a picture of type, no layer."""
    pdf = pdfium.PdfDocument.new()
    for n, kind in enumerate(kinds, 1):
        page = pdf.new_page(595, 842)
        if kind == "T":
            _typeset(pdf, page, TYPESET.format(n=n), 742)
        else:
            image = Image.new("L", (1240, 1754), 255)
            draw, font = ImageDraw.Draw(image), ImageFont.load_default(size=40)
            draw.text((120, 200), f"SCANNED PAGE {n}", fill=0, font=font)
            draw.text((120, 270), "Report of the district magistrate on the harvest.", fill=0, font=font)
            obj = pdfium.PdfImage.new(pdf)
            obj.set_bitmap(pdfium.PdfBitmap.from_pil(image))
            obj.set_matrix(pdfium.PdfMatrix().scale(595, 842))
            page.insert_obj(obj)
            if stamp:
                _typeset(pdf, page, STAMP.format(n=n), 20)
        page.gen_content()
    pdf.save(str(path))
    return path


def _needs_ocr():
    if not (shutil.which("ocrmypdf") and shutil.which("pdftotext") and shutil.which("tesseract")):
        pytest.skip("ocrmypdf, poppler and tesseract are not all installed")
    langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", check=False).stdout.split()
    if not {"eng", "chi_sim", "jpn"} <= set(langs):
        pytest.skip("the default OCR languages are not all installed")


@pytest.mark.parametrize("kinds, stamp", [("TTSSS", False), ("SSSS", True), ("TTTTSSS", False)])
def test_real_ocr_reads_every_scanned_page_in_place(tmp_path, kinds, stamp):
    _needs_ocr()
    path = build_pdf(tmp_path / "book.pdf", kinds, stamp)
    meta = {}
    pages = corpus._pdf_pages(str(path), meta)
    assert len(pages) == len(kinds)
    for n, kind in enumerate(kinds, 1):
        if kind == "T":
            assert TYPESET.format(n=n) in pages[n - 1]
        else:
            assert f"SCANNED PAGE {n}" in pages[n - 1] and "magistrate" in pages[n - 1]
    assert "unread_pages" not in meta


def test_a_reread_without_pdfiums_answer_keeps_a_plates_ocr(tmp_path, monkeypatch):
    """Routed by text alone (pdfium's child timed out), a plate OCR read by its image cover went
    back to its one-line caption, stamped current for good."""
    ddir, source = tmp_path / "doc", tmp_path / "book.pdf"
    (ddir / "pages").mkdir(parents=True)
    source.write_bytes(b"%PDF-1.4\n")
    layer = [typeset(n) for n in range(1, 6)] + ["Plate three. Canton harbour, as painted"]
    stored = layer[:5] + [layer[5] + "\n\nCanton factories, junks, the Thirteen Hongs"]
    for number, text in enumerate(stored, 1):
        (ddir / "pages" / f"p{number:04d}.txt").write_text(text, encoding="utf-8")
    (ddir / "meta.json").write_text(json.dumps({"extract_version": 2, "pages": 6, "ocr": True, "ocr_pages": [6]}),
                                    encoding="utf-8")
    monkeypatch.setattr(corpus, "_pdf_text_layer", lambda p: list(layer))
    monkeypatch.setattr(corpus, "_pdf_visuals", lambda p, numbers: None)
    monkeypatch.setattr(corpus, "_ocr_pages", lambda *a, **k: None)
    assert corpus._reread(str(ddir), str(source), 6) == 6
    assert (ddir / "pages" / "p0006.txt").read_text(encoding="utf-8") == stored[5]
    assert json.loads((ddir / "meta.json").read_text(encoding="utf-8"))["extract_version"] == 2
    # A file pdfium never reads is not re-extracted, OCR and all, on every ingest for ever.
    for _ in range(corpus.REREAD_TRIES - 1):
        corpus._reread(str(ddir), str(source), 6)
    meta = json.loads((ddir / "meta.json").read_text(encoding="utf-8"))
    assert meta["extract_version"] == corpus.EXTRACT_VERSION and "reread_put_off" not in meta
    assert (ddir / "pages" / "p0006.txt").read_text(encoding="utf-8") == stored[5], "what was stored stands"


def _put_off_doc(tmp_path, monkeypatch, **meta):
    ddir, source = tmp_path / "doc", tmp_path / "book.pdf"
    (ddir / "pages").mkdir(parents=True)
    source.write_bytes(b"%PDF-1.4\n")
    layer = [typeset(n) for n in range(1, 6)] + [""]
    for number, text in enumerate(layer, 1):
        (ddir / "pages" / f"p{number:04d}.txt").write_text(text, encoding="utf-8")
    (ddir / "meta.json").write_text(json.dumps({"extract_version": 2, "pages": 6, **meta}), encoding="utf-8")
    monkeypatch.setattr(corpus, "_pdf_text_layer", lambda p: list(layer))
    monkeypatch.setattr(corpus, "_ocr_pages", lambda *a, **k: None)
    return ddir, source


def test_a_reread_put_off_and_then_answered_forgets_the_misses(tmp_path, monkeypatch):
    ddir, source = _put_off_doc(tmp_path, monkeypatch)
    # With ocrmypdf installed: on a machine without it the scanned pages wait for it, which keeps the put-off.
    real_which = shutil.which
    monkeypatch.setattr(corpus.shutil, "which", lambda name: f"/fake/{name}" if name == corpus.OCR_BINARY else real_which(name))
    monkeypatch.setattr(corpus, "_pdf_visuals", lambda p, numbers: None)
    corpus._reread(str(ddir), str(source), 6)
    monkeypatch.setattr(corpus, "_pdf_visuals", lambda p, numbers: {n: {"cover": 1.0, "marked": True} for n in numbers})
    corpus._reread(str(ddir), str(source), 6)
    meta = json.loads((ddir / "meta.json").read_text(encoding="utf-8"))
    assert "reread_put_off" not in meta, "the next version's re-read starts its count afresh"


def test_a_reread_is_not_given_up_while_its_pages_wait_for_ocr(tmp_path, monkeypatch):
    ddir, source = _put_off_doc(tmp_path, monkeypatch, unread_pages={"6": f"{corpus.ROUTE_EMPTY}; {corpus.OCR_MISSING}"})
    monkeypatch.setattr(corpus, "_pdf_visuals", lambda p, numbers: None)
    monkeypatch.setattr(corpus.shutil, "which", lambda name: None)
    for _ in range(corpus.REREAD_TRIES + 1):
        corpus._reread(str(ddir), str(source), 6)
    assert json.loads((ddir / "meta.json").read_text(encoding="utf-8"))["extract_version"] == 2, \
        "read again once ocrmypdf is installed"
