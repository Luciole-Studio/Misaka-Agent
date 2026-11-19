"""Where an Office rendering is cut, and what a continuation page says about itself.

The corpus addresses a citation as (page, offset), so what a page *is* decides whether a
page number locates anything. ``index.py:_paginate`` cuts running text at paragraph breaks,
which is right for a book and wrong for a workbook: it puts the tail of one sheet and the
head of the next on one page, and "p7" then names nothing a reader can turn to. So a
rendering is cut at the format's own boundaries first -- sheet, slide, top-level heading --
and only a block that is still too large goes on to ``_paginate``.

This module deliberately does not call ``_paginate`` itself. ``index.py`` imports this
package; importing ``index`` back would be a cycle, and the composition is two lines at the
one call site that has both.

The boundary is the rendering's own title line rather than an inserted marker: a marker
would be text in the page, quotable and therefore verifiable, and the title line has to be
there anyway.
"""
from __future__ import annotations

import itertools
import re

_BOUNDARY = {
    "xlsx": re.compile(r"^## Sheet: (?!.*\(continued\)$).*$", re.MULTILINE),
    "csv": re.compile(r"^## Sheet: (?!.*\(continued\)$).*$", re.MULTILINE),
    "pptx": re.compile(r"^## Slide \d+(?::|\b)(?!.*\(continued\)$).*$", re.MULTILINE),
    "docx": re.compile(r"^# (?!.*\(continued\)$).*$", re.MULTILINE),
}

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)", re.MULTILINE)

_COLUMN_LETTERS = re.compile(r"^\t(?:[A-Z]{1,3}\t)*[A-Z]{1,3}$")
_FENCE_CLOSE = "```"

CONTINUED = " (continued)"


def _fenced_spans(text):
    """``[(start, end)]`` of every fenced region, so a boundary inside one can be skipped.

    An unclosed fence runs to the end of the text: the safe direction is to treat the tail
    as content rather than to find boundaries inside what a writer meant as one block.
    """
    spans, opening = [], None
    for match in _FENCE.finditer(text):
        fence, rest = match.groups()
        if opening is None:
            if fence[0] != "`" or "`" not in rest:
                opening = (match.start(), fence)
        elif fence[0] == opening[1][0] and len(fence) >= len(opening[1]) and not rest.strip():
            spans.append((opening[0], match.end()))
            opening = None
    if opening is not None:
        spans.append((opening[0], len(text)))
    return spans


def blocks(text, fmt):
    """Cut ``text`` at ``fmt``'s own boundaries; ``[]`` for a rendering with no content.

    Blocks concatenate back to ``text`` exactly. Anything before the first boundary belongs
    to the first block -- a document's front matter is not a section of its own.
    """
    if not text or not text.strip():
        return []
    pattern = _BOUNDARY.get(fmt)
    if pattern is None:
        return [text]
    spans = _fenced_spans(text)
    found = [m.start() for m in pattern.finditer(text)
             if not any(lo <= m.start() < hi for lo, hi in spans)]
    cuts = found[1:]
    if not cuts:
        return [text]
    bounds = [0, *cuts, len(text)]
    return [text[a:b] for a, b in itertools.pairwise(bounds) if text[a:b]]


def resume_context(block, fmt):
    """The header a continuation page of ``block`` opens with, or ``""``.

    Two things, both of which a page after the first would otherwise be missing: the
    block's own title line, marked as a continuation so re-splitting stays idempotent, and
    -- for a spreadsheet -- the column-letter row, without which the page is a wall of
    values in unnamed columns.
    """
    pattern = _BOUNDARY.get(fmt)
    if pattern is None:
        return ""
    spans = _fenced_spans(block)
    title = next((match for match in pattern.finditer(block)
                  if not any(lo <= match.start() < hi for lo, hi in spans)), None)
    if title is None:
        return ""
    lines = block[title.start():].splitlines()
    out = lines[0].rstrip() + CONTINUED + "\n"
    header = _header_row(lines[1:], fmt)
    if header is not None:
        out += header + "\n"
    return out


def _header_row(lines, fmt):
    """The tabular header to repeat on a continuation page, or ``None``."""
    if fmt == "xlsx":
        return next((line for line in lines if _COLUMN_LETTERS.match(line)), None)
    if fmt == "csv":
        for index, line in enumerate(lines):
            if line.strip() == _FENCE_CLOSE and index + 1 < len(lines):
                return lines[index + 1]
    return None
