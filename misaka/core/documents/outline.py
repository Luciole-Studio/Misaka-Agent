"""A document's outline: which reader made it and from what, or why there is none.

Three readers write one tree shape -- nested ``{title, node_id, start_index, end_index, nodes}``
over the corpus' own page numbers: PageIndex over a PDF's bookmarks and layout, ``text_outline``
over the headings a text file states, and the headings MinerU marks on a PDF PageIndex found
nothing in. ``derive`` picks among them and ``index`` stores what it returns: the tree in
``tree.json``, the attempt in ``meta.json`` under ``outline``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from functools import partial

from misaka.core.documents import mineru, pdf_outline, text_outline

# Bump when a reader would find something it did not before. A document without an outline is
# asked again once the attempt on record is older. 1 was ``tree_attempted_version``, which kept
# only "none found" and so re-ran every failure on every re-index: ten real PDFs, 90 s a round.
VERSION = 2
MIN_PAGES = 20
WORKERS = 2
REASON_CHARS = 200

BUILT, NONE, FAILED, UNAVAILABLE = "built", "none", "failed", "unavailable"

SOURCES = {
    "bookmarks": "the bookmarks the file carries",
    "hybrid": "the file's bookmarks, with sections detected from the page layout hung under them",
    "layout": "headings detected from the page layout (the file states no outline, so an entry can be wrong)",
    "text_headings": "the headings the text states on lines of their own",
    "ocr_headings": "the headings MinerU marked on the pages (sparse, and a running head can pass for one)",
}
_PAGEINDEX_SOURCES = {"bookmarks": "bookmarks", "hybrid": "hybrid", "detected": "layout"}


@dataclass(frozen=True)
class Outline:
    """One attempt at a document's outline. ``nodes`` and ``source`` come with ``BUILT``,
    ``reason`` with ``FAILED``."""
    status: str
    nodes: list = field(default_factory=list)
    source: str = ""
    reason: str = ""

    @property
    def record(self):
        """What ``meta.json`` keeps of the attempt. None for ``UNAVAILABLE``: an install that
        cannot read outlines learned nothing about the document, and must ask once repaired."""
        if self.status == UNAVAILABLE:
            return None
        kept = {"status": self.status, "version": VERSION}
        if self.source:
            kept["source"] = self.source
        if self.reason:
            kept["reason"] = self.reason
        return kept


def _stated(_path, pages, _printed_pages, *, markdown):
    nodes = text_outline.build_text_tree(pages(), markdown=markdown)
    return Outline(BUILT, nodes, "text_headings") if nodes else Outline(NONE)


def _pdf(path, _pages, printed_pages):
    try:
        result = pdf_outline.read(path, workers=WORKERS)
    except pdf_outline.PageIndexUnavailable:
        _warn_unavailable(path)
        return Outline(UNAVAILABLE)
    except pdf_outline.PageIndexFailed as error:
        return Outline(FAILED, reason=str(error)[:REASON_CHARS])
    nodes = result.get("structure") or []
    if nodes:
        return Outline(BUILT, nodes, _PAGEINDEX_SOURCES.get(result.get("toc_source"), "layout"))
    return (_printed(path, printed_pages) if printed_pages else None) or Outline(NONE)


def _printed(path, count):
    """The outline the headings printed on a PDF's pages make, or None.

    Sparser than a publisher's, and not always right: against the bookmarks of three scanned books
    it found 2, 7 and 7 of 23, 25 and 48, and of what it offered on one of them about half were a
    title page's lines or a running head rather than sections. On another all twelve were sections.
    A document that would otherwise have no outline at all is what this is weighed against.
    """
    try:
        found = mineru.headings(path, count)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    marked = ["\n\n".join(f"{'#' * level} {title}" for level, title in found.get(number, []))
              for number in range(1, count + 1)]
    nodes = text_outline.build_text_tree(marked, markdown=True)
    return Outline(BUILT, nodes, "ocr_headings") if nodes else None


_READERS = {".pdf": _pdf, ".txt": partial(_stated, markdown=False),
            ".md": partial(_stated, markdown=True), ".markdown": partial(_stated, markdown=True)}


def reads(ext):
    """Whether any reader takes this suffix. Other formats have no outline to ask for."""
    return ext in _READERS


def derive(path, pages, *, printed_pages=0):
    """The outline of the document at ``path``, whose suffix ``reads`` accepts.

    ``pages`` returns the corpus' page texts and is called for a text file only. ``printed_pages``
    is how many pages MinerU may be asked for headings on when PageIndex finds nothing in a PDF;
    0 does not ask.
    """
    return _READERS[os.path.splitext(path)[1].lower()](path, pages, printed_pages)


def describe(record, *, has_tree, ext, pages):
    """Where a document's outline came from, or why it has none, as a clause.

    Empty for an outline the corpus kept no word about: one built before attempts were recorded.
    """
    record = record if isinstance(record, dict) else {}
    if has_tree:
        told = SOURCES.get(record.get("source"))
        return f"outline from {told}" if told else ""
    if not reads(ext):
        return "no outline: this format has none"
    if record.get("status") == FAILED:
        error = str(record.get("reason") or "").partition(":")[0] or "error"
        return (f"no outline: the outline reader failed on this file ({error}), which says nothing "
                "about how the document is organised")
    if record.get("status") == NONE:
        return "no outline: the file states none and none could be read from its pages"
    if pages < MIN_PAGES:
        return f"no outline: documents under {MIN_PAGES} pages are navigated by page"
    if ext == ".pdf" and not pdf_outline.available():
        return (f"no outline: this install cannot read PDF outlines ({pdf_outline.INSTALL_HINT}), which "
                "says nothing about how the document is organised")
    return "no outline: none has been built for it; indexing the file again builds one"


_warned_unavailable = False


def _warn_unavailable(path):
    """Say once, out loud, that every PDF outline in this run was skipped for the same reason.

    Without this a broken install is invisible: ingestion succeeds, the corpus gets pages and no
    ``tree.json``. Once per process, because a scan hits it for every PDF in the folder.
    """
    global _warned_unavailable
    if _warned_unavailable:
        return
    _warned_unavailable = True
    print(f"Warning: no outline for {os.path.basename(path)} (and any other PDF in this run): "
          f"PageIndex's packages are missing from this install, so PDFs are split into pages only.\n"
          f"         To fix it: {pdf_outline.INSTALL_HINT}\n"
          f"         Then re-run the same `misaka doc add`/`doc scan` to build the outlines.",
          file=sys.stderr)
