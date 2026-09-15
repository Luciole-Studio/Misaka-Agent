"""An Office document is cut at its own boundaries, and every page says what it is.

The corpus pins a citation to a page (``verify_quote`` returns page + offset), and an
Office file has no pages -- so one has to be invented, and where it is invented decides
whether a page number means anything. Cutting a workbook at 3000 characters puts half of
one sheet and the top of the next on the same page, and "p7" then names nothing a reader
can find. Cutting at the sheet and the slide instead makes the page number a location.

A block that is itself larger than a page still has to be split, and the pages after the
first would otherwise arrive with no idea what they are: 400 lines of numbers, no sheet
name, no column letters. FrontierAgent rebuilds that context on every continuation page
(``_reader_core.py:430 _resume_ctx``) and so does this -- the block's own title line, plus
the column-letter header for a spreadsheet.
"""
from __future__ import annotations

from misaka.core.documents.office import paging


def test_a_workbook_is_cut_at_its_sheets():
    md = "## Sheet: One\nrow one\n\n## Sheet: Two\nrow two\n\n## Sheet: Three\nrow three\n"
    blocks = paging.blocks(md, "xlsx")
    assert len(blocks) == 3
    assert blocks[0].startswith("## Sheet: One")
    assert blocks[2].startswith("## Sheet: Three")


def test_a_deck_is_cut_at_its_slides():
    md = "## Slide 1: Title\nbody\n\n## Slide 2: Next\nbody\n"
    blocks = paging.blocks(md, "pptx")
    assert [b.splitlines()[0] for b in blocks] == ["## Slide 1: Title", "## Slide 2: Next"]


def test_a_document_is_cut_at_its_top_level_headings():
    md = "# Chapter One\ntext\n\n## A section, not a boundary\nmore\n\n# Chapter Two\ntext\n"
    blocks = paging.blocks(md, "docx")
    assert len(blocks) == 2
    assert "A section, not a boundary" in blocks[0]


def test_text_before_the_first_boundary_belongs_to_the_first_block():
    """A document's front matter is not a block of its own -- FA's ``_split_blocks`` says
    the same: legend and lead-in belong to the first block."""
    md = "some front matter\n\n## Sheet: One\nrow\n"
    blocks = paging.blocks(md, "xlsx")
    assert len(blocks) == 1
    assert blocks[0].startswith("some front matter")


def test_a_document_with_no_boundary_is_one_block():
    assert paging.blocks("just text, no headings\n", "docx") == ["just text, no headings\n"]


def test_an_empty_rendering_has_no_blocks():
    assert paging.blocks("", "xlsx") == []
    assert paging.blocks("   \n\n ", "xlsx") == []


def test_blocks_concatenate_back_to_the_rendering():
    md = "## Sheet: One\nrow one\n\n## Sheet: Two\nrow two\n"
    assert "".join(paging.blocks(md, "xlsx")) == md


def test_a_heading_inside_a_fenced_block_is_not_a_boundary():
    """A cell holding ``## Sheet: fake`` is data, and a code fence in a docx is content."""
    md = "## Sheet: One\n```\n## Sheet: fake\n```\nafter\n\n## Sheet: Two\nrow\n"
    blocks = paging.blocks(md, "xlsx")
    assert len(blocks) == 2
    assert "## Sheet: fake" in blocks[0]


def test_a_continuation_page_repeats_the_sheet_title_and_the_column_header():
    block = "## Sheet: Revenue\n\tA\tB\tC\n1\tone\ttwo\tthree\n2\tfour\tfive\tsix\n"
    assert paging.resume_context(block, "xlsx") == "## Sheet: Revenue (continued)\n\tA\tB\tC\n"


def test_a_continuation_page_of_a_slide_repeats_only_its_title():
    block = "## Slide 3: Results\nbullet one\nbullet two\n"
    assert paging.resume_context(block, "pptx") == "## Slide 3: Results (continued)\n"


def test_a_block_with_no_title_line_has_no_continuation_context():
    assert paging.resume_context("just rows\nand more rows\n", "xlsx") == ""


def test_a_sheet_with_no_column_header_still_repeats_its_title():
    block = "## Sheet: Notes\nfree text with no grid\n"
    assert paging.resume_context(block, "xlsx") == "## Sheet: Notes (continued)\n"


def test_the_continuation_line_is_not_mistaken_for_a_new_boundary():
    """Re-splitting a paged rendering must not multiply blocks: the continuation title
    carries ``(continued)``, and the splitter treats it as body."""
    md = "## Sheet: One\nrow\n\n## Sheet: One (continued)\nmore rows\n"
    assert len(paging.blocks(md, "xlsx")) == 1


def test_a_continuation_page_of_a_csv_repeats_its_own_column_names():
    """A workbook's header is the column-letter row and is recognisable on sight; a csv's
    is whatever the file called its columns, so it is found by position -- the line after
    the meta fence closes."""
    block = ("## Sheet: rows.csv\n```meta\n▸ table (2 data rows)\n```\n"
             "name\tamount\nalpha\t1\nbeta\t2\n")
    assert paging.resume_context(block, "csv") == "## Sheet: rows.csv (continued)\nname\tamount\n"


def test_a_csv_with_no_fence_still_repeats_its_title():
    assert paging.resume_context("## Sheet: x.csv\nrows\n", "csv") == "## Sheet: x.csv (continued)\n"
