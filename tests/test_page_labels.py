"""The number printed on a page, shown beside the file's page.

2026-10-07 (GitHub issue #9): a page here is a page of the file, and a delivered card cited a
scanned book's file pages as the book's own -- thirteen pages out. 32 of the 98 PDFs in one real
corpus label their pages (Roman front matter, journal articles starting at 1361); the rest print
their numbers in running heads and footers."""
from __future__ import annotations

from types import SimpleNamespace as NS

from misaka.core.documents import index as corpus
from misaka.core.documents.wiring import documents
from misaka.core.wiring import ToolCollector

PROSE = "The peasants for their part by and large looked on the members of this order with suspicion"


def _book(first_printed, pages=30, head="The Mind in the Machine", unnumbered=()):
    """A scan's pages: a running head carrying the printed number, and a footnote at the foot."""
    out = []
    for n in range(1, pages + 1):
        number = first_printed + n - 1
        top = head if n in unnumbered else f"{head}                    {number}"
        out.append(f"{top}\n{PROSE} ({n}).\nMore text on the page.\n{n % 7 + 80} Zhuravsky, Stat. obozreniye, p. 313.\n")
    return out


def test_the_number_alone_or_at_the_edge_of_a_head_is_a_folio():
    assert corpus._folios("The Mind in the Machine    219\nbody") == {219}
    assert corpus._folios("body\n- 338 -") == {338}
    assert corpus._folios("第338页\nbody") == {338}
    assert corpus._folios("The campaign of 1812 began in June\nbody text\nmore") == set()


def test_a_constant_distance_over_many_pages_is_believed():
    labels = corpus._inferred_labels(_book(first_printed=-1, pages=30))
    assert labels[3] == "1" and labels[30] == "28"


def test_a_footnote_number_or_a_year_does_not_pass_for_a_folio():
    pages = [f"1812: The Russian Campaign\n{PROSE}\n{n * 3} See note.\n" for n in range(1, 31)]
    assert corpus._inferred_labels(pages) == {}


def test_the_digits_of_a_number_printed_apart_are_not_folios():
    """A law journal prints both its page numbers with the digits apart, "(83-3-) 1 8 8 4 5 4" on
    one page and "(83-3-) 4 5 5 1 8 9" on the next. The last digit of each foot passed for a folio
    and 25 of the paper's 32 pages were given printed numbers such as 1, 3 and 5 (2026-11-19)."""
    def foot(n):
        issue, volume = " ".join(str(185 + n)), " ".join(str(451 + n))
        return f"(Law Review 83-3-) {issue} {volume}" if n % 2 else f"(83-3-) {volume} {issue}"

    pages = [f"{PROSE} ({n}).\nMore text on the page.\n{foot(n)}\n" for n in range(1, 33)]

    assert corpus._folios(pages[2]) == set()
    assert corpus._inferred_labels(pages) == {}


def test_an_unnumbered_chapter_opening_between_two_numbered_pages_takes_its_number():
    labels = corpus._inferred_labels(_book(first_printed=100, pages=30, unnumbered={15}))
    assert labels[15] == "114"


def test_a_short_run_of_numbers_is_not_enough():
    assert corpus._inferred_labels(_book(first_printed=50, pages=6)) == {}


def _labelled_pdf(path):
    """Four pages labelled i, ii, 1, 2 -- written by hand, no PDF writer needed."""
    objects = ["<< /Type /Catalog /Pages 2 0 R /PageLabels << /Nums [0 << /S /r >> 2 << /S /D >>] >> >>",
               "<< /Type /Pages /Kids [3 0 R 4 0 R 5 0 R 6 0 R] /Count 4 >>"]
    objects += ["<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>"] * 4
    body, offsets = b"%PDF-1.4\n", []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(body))
        body += f"{number} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(body)
    body += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    body += "".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode()
    body += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(body)
    return path


def test_a_pdf_says_what_its_pages_are_labelled(tmp_path):
    assert corpus._pdf_page_labels(str(_labelled_pdf(tmp_path / "labelled.pdf"))) == ["i", "ii", "1", "2"]


def _indexed(tmp_path, monkeypatch, pages, name="scan.pdf"):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / name).write_bytes(b"%PDF-1.4\n% stand-in\n")
    monkeypatch.setattr(corpus, "extract_pages", lambda p, meta=None: list(pages))
    monkeypatch.setattr(corpus, "_pdf_page_labels", lambda p: None)
    doc_id, _ = corpus.ingest(str(workspace / name), workspace=str(workspace), with_tree=False)
    return doc_id, str(workspace)


async def test_the_printed_number_is_shown_beside_the_file_page(tmp_path, monkeypatch):
    doc_id, workspace = _indexed(tmp_path, monkeypatch, _book(first_printed=338, pages=30))
    labels, where = corpus.page_labels(doc_id, workspace=workspace)
    assert labels[1] == "338" and where == "the page headers and footers"
    assert "--- p14 (printed p. 351) ---" in corpus.read_pages(doc_id, 14, 14, workspace=workspace)
    hit = corpus.search_literal(f"{PROSE} (14)", doc_id=doc_id, workspace=workspace)[0]
    assert (hit["page"], hit["printed"]) == (14, "351")
    collector = ToolCollector()
    documents.register(collector)
    verify = next(t for t in collector.tools if t.name == "doc_verify")
    result = await verify.execute("call", {"doc_id": doc_id, "quote": f"{PROSE} (14)"}, None, None,
                                  NS(cwd=workspace, model=None, modelRegistry=None, signal=None))
    text = result["content"][0]["text"]
    assert "printed page 351 (from the page headers and footers)" in text
    assert f"Cite as: [{doc_id} p14; printed p. 351]" in text


def test_a_book_whose_numbers_match_its_file_pages_shows_nothing_extra(tmp_path, monkeypatch):
    doc_id, workspace = _indexed(tmp_path, monkeypatch, _book(first_printed=1, pages=30))
    assert corpus.page_labels(doc_id, workspace=workspace) == ({}, "the page headers and footers")
    assert "printed" not in corpus.read_pages(doc_id, 5, 5, workspace=workspace)
