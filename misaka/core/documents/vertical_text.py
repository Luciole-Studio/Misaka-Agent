"""Vertical text that ``pdftotext -layout`` reads across its columns, put back in reading order.

A page set top to bottom in columns, as Japanese and classical Chinese are printed, reaches
``-layout`` as rows cut across those columns: every character alone, in an order nobody reads.
Search and quotation checks then find nothing on it. One paper printed 顧問 on eight pages and
none of them was found (2026-11-19).

``pdftotext -raw`` gives the same page in the order the file states its text: one character a
line, down each column in turn. Joining those lines gives the page back, in the characters
pdftotext read for the rest of the document. PDFium also reads such a page in order, but on a
gazetteer it dropped 140 of a page's 410 characters without a sign, so it is not used.
"""
from __future__ import annotations

import unicodedata

# Of the 37,005 pages pdftotext reads in 238 real PDFs (2026-11-19), 163 have 0.3 or more of
# their characters standing alone between spaces. Those are read again in ``-raw``.
ALONE = 0.3

# In ``-raw``, the 131 vertical pages among those 163 have 0.87 or more of their lines one
# character long. The other 32, tables, indexes and spaced-out headings, have 0.39 or less.
COLUMN_LINES = 0.6


def cut_across(text, least):
    """Whether a page of ``-layout`` text looks cut across columns. A page of under ``least``
    characters is too short to say."""
    tokens = text.split()
    characters = sum(map(len, tokens))
    return characters >= least and sum(1 for token in tokens if len(token) == 1) >= ALONE * characters


def in_reading_order(raw):
    """The page from its ``-raw`` text with each column joined into a line, or None when that text
    is not columns of single characters.

    Most of the single characters must be letters: a table of one-digit cells written column by
    column also arrives one character a line, and joined it would be one long number.
    """
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    single = [line for line in lines if len(line) == 1]
    if len(single) < COLUMN_LINES * len(lines):
        return None
    if 2 * sum(unicodedata.category(character).startswith("L") for character in single) <= len(single):
        return None
    joined, column = [], []
    for line in lines:
        if len(line) == 1:
            column.append(line)
            continue
        if column:
            joined.append("".join(column))
            column = []
        joined.append(line)
    if column:
        joined.append("".join(column))
    return "\n".join(joined)
