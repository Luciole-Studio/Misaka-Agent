"""Reading Office documents: one renderer, two entry points.

``core/tools/read.py`` shows a model a file in its working directory; ``documents/index.py``
puts a document in the corpus, where a quotation can be locked to a page. FrontierAgent has
one ``read_file`` for both and misaka does not, so the rendering lives here and both callers
use it. That is the point of it being one function: the text a model reads in the working
directory is the same text it later cites out of the corpus, byte for byte, so a quotation
copied out of a ``read`` verifies against the indexed document instead of missing by a
space.

What is dispatched here, and what is not:

* the formats in ``_FORMATS``, each to its own module. The table grows with its renderers
  and never ahead of them -- a suffix listed before its module exists is a file routed here
  and then crashed on, which is worse than the refusal that names what this reads;
* legacy ``.doc`` / ``.xls`` / ``.ppt`` are NOT here. They are OLE, not zip, and reading
  them needs LibreOffice; ``index.py`` routes them through ``office.soffice`` first (the
  plan's W22-I) and what arrives here is the converted package;
* ``.pdf`` stays with pageindex and images stay with the image stack -- neither is this
  package's, and both are owner-excluded.

Everything that is a zip is prechecked before its parser is called; see ``_zip``.
"""
from __future__ import annotations

import os

from misaka.core.documents.office import _zip
from misaka.core.documents.office import docx as _docx
from misaka.core.documents.office import pptx as _pptx
from misaka.core.documents.office import xlsx as _xlsx
from misaka.core.documents.office.cache import render_cached
from misaka.core.documents.office.paging import blocks, resume_context

# Suffix to renderer key. ``.xlsm`` is a workbook that carries macros: the macros are not
# text and the sheets read identically, so it is an ``xlsx`` (borrowed from FrontierAgent
# plugins/tools/_reader_core.py:572 ``_FMT_NORM``). ``.csv``/``.tsv`` are rendered by the
# workbook module because they share its relational-table representation, but they are
# their own format for paging: a csv has no sheets to cut at. ``.docm``/``.pptm`` are the
# macro-bearing variants and read identically for the same reason ``.xlsm`` does.
# Only what this package can actually render is listed; ``read`` routes on membership here
# and ``index._EXTRACTORS`` mirrors it.
_FORMATS = {
    ".xlsx": "xlsx", ".xlsm": "xlsx",
    ".csv": "csv", ".tsv": "csv",
    ".docx": "docx", ".docm": "docx",
    ".pptx": "pptx", ".pptm": "pptx",
}

# The formats that are OOXML packages, and so go through the zip precheck. A csv is a text
# file: prechecking it as a zip would refuse every csv there is.
_PACKAGED = frozenset({"xlsx", "docx", "pptx"})

# The renderer per format. ``cell_range`` reaches only the workbook: it names a sheet and a
# range, which the other three do not have.
_RENDER = {
    "xlsx": lambda path, cell_range, meta: _xlsx.render(path, cell_range=cell_range,
                                                        meta=meta),
    "csv": lambda path, cell_range, meta: _xlsx.render_csv(path),
    "docx": lambda path, cell_range, meta: _docx.render(path),
    "pptx": lambda path, cell_range, meta: _pptx.render(path),
}

SUFFIXES = frozenset(_FORMATS)

__all__ = [
    "SUFFIXES",
    "TITLE_OF",
    "blocks",
    "format_of",
    "precheck",
    "render",
    "render_cached",
    "resume_context",
]

precheck = _zip.precheck

# The title a document carries inside itself, for the formats that carry one.
# ``index.source_title`` prefers it over the file name, which for a downloaded deck is
# whatever the URL ended in.
TITLE_OF = {"docx": _docx.title, "pptx": _pptx.title}


def format_of(path):
    """The renderer key for a path this package reads (``"xlsx"``, ``"csv"``, ...), else
    ``None``. Pure -- it never touches the filesystem, so a caller can route on it before
    deciding whether the file is worth opening."""
    return _FORMATS.get(os.path.splitext(str(path))[1].lower())


def render(path, *, cell_range=None, meta=None):
    """The whole document as markdown.

    Deterministic: the same file renders to the same text, which is what lets the corpus
    store pages of it and lets ``read`` hand a model something it can quote. Raises
    ``ValueError`` naming the file for anything unreadable -- an unknown suffix, a package
    that fails the precheck, a damaged archive -- because that string is the answer
    ``doc_add`` gives the model.

    ``cell_range`` ("Sheet1!A3:D15") renders just that region of a workbook. It is refused
    rather than ignored for the other formats: a silently dropped argument reads to the
    model as a range it asked for and did not get.

    ``meta`` collects what the renderer learned on the way -- why a recalculation did not
    happen, which conversion produced the file -- so a caller's refusal can quote it.
    """
    fmt = format_of(path)
    if fmt is None:
        name = os.path.basename(str(path))
        suffix = os.path.splitext(name)[1].lower() or "a file with no suffix"
        raise ValueError(
            f"Cannot read {name}: this reads {' '.join(sorted(SUFFIXES))}, not {suffix}. "
            "Convert the file first, or use read for plain text and doc_add for a PDF."
        )
    if cell_range and fmt != "xlsx":
        raise ValueError(
            f"cell_range names a sheet and a range, and this is a {fmt} file; "
            "call again without it."
        )
    if fmt in _PACKAGED:
        _zip.precheck(path)
    return _RENDER[fmt](path, cell_range, meta)
