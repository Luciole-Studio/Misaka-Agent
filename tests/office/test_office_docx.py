"""A Word document reads in body order, keeps its footnotes, and stays quotable.

The last of those is the one that shapes everything else. A quotation is checked against
the page after ``normalize_for_quote_match`` folds every whitespace character away, so any
character the parser wedges between two words of a sentence -- a ``**``, a ``[^1]``, the
``](`` of a markdown link, the ``|`` of a pipe table -- is a character the page has and the
model's quotation does not, and the sentence around it can never be locked to its page.
pandoc writes all four inline and FrontierAgent inherits that; here they go after the line.

The tests below are therefore split in two: what the rendering *says* (structure, order,
footnotes) and what it *can still verify* (the folding tests). The second set is the one
that must not regress.
"""
from __future__ import annotations

import importlib
import zipfile

import pytest

from misaka.core.documents.index import normalize_for_quote_match as fold
from misaka.core.documents.office import docx as reader

docx = importlib.import_module("docx")

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def _paper(tmp_path, name="paper.docx"):
    """A document with headings, mixed-format runs, both list kinds and a table between two
    paragraphs -- the shape python-docx's own ``doc.paragraphs`` walk cannot reproduce."""
    document = docx.Document()
    document.add_heading("Quarterly Review", 1)
    paragraph = document.add_paragraph()
    paragraph.add_run("Total ")
    for part in ("reven", "ue"):          # Word splits a phrase at spell-check boundaries
        run = paragraph.add_run(part)
        run.bold = True
    paragraph.add_run(" rose ")
    italic = paragraph.add_run("sharply")
    italic.italic = True
    paragraph.add_run(" in the third quarter.")
    document.add_paragraph("first", style="List Bullet")
    document.add_paragraph("second", style="List Bullet")
    document.add_paragraph("step one", style="List Number")
    document.add_paragraph("step two", style="List Number")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "region"
    table.cell(0, 1).text = "units"
    table.cell(1, 0).text = "North"
    table.cell(1, 1).text = "800"
    document.add_paragraph("after the table")
    path = tmp_path / name
    document.save(path)
    return path


def _hand_built(tmp_path, body, extra=None, name="notes.docx"):
    """A .docx assembled from XML.

    python-docx cannot write a footnote at all, which is exactly why FrontierAgent's
    python-docx fallback loses them; the package has to be built by hand to test that this
    one does not. Same technique the EPUB tests use, and no binary fixture in the repo.
    """
    content_types = (
        '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/'
        '2006/content-types"><Default Extension="rels" ContentType="application/vnd.'
        'openxmlformats-package.relationships+xml"/><Default Extension="xml" '
        'ContentType="application/xml"/><Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.'
        'document.main+xml"/></Types>'
    )
    root_rels = (
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
        'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
        'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    styles = (f'<?xml version="1.0"?><w:styles {W}><w:style w:styleId="Heading1" '
              'w:type="paragraph"><w:name w:val="heading 1"/></w:style></w:styles>')
    members = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": root_rels,
        "word/styles.xml": styles,
        "word/document.xml":
            f'<?xml version="1.0"?><w:document {W} {R}><w:body>{body}</w:body></w:document>',
        **(extra or {}),
    }
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        for member, text in members.items():
            archive.writestr(member, text)
    return path


# ---- structure -------------------------------------------------------------------------

def test_a_table_sits_where_the_document_puts_it(tmp_path):
    """``doc.paragraphs`` skips tables and ``doc.tables`` collects them at the end, so the
    easy python-docx walk moves every table to the bottom of the document. The walk here is
    over ``w:body``, where they arrive interleaved."""
    out = reader.render(str(_paper(tmp_path)))
    body = out.index("Total revenue rose")
    table = out.index("`table 2×2`")
    after = out.index("after the table")
    assert body < table < after


def test_heading_levels_become_hashes(tmp_path):
    assert "# Quarterly Review" in reader.render(str(_paper(tmp_path)))


def test_a_localised_heading_style_is_still_a_heading(tmp_path):
    """Word ships localised style names: a document authored in Chinese Word carries
    "标题 1" for the same style an English one calls "Heading 1"."""
    styles = (f'<?xml version="1.0"?><w:styles {W}><w:style w:styleId="a3" '
              'w:type="paragraph"><w:name w:val="标题 1"/></w:style></w:styles>')
    path = _hand_built(
        tmp_path,
        '<w:p><w:pPr><w:pStyle w:val="a3"/></w:pPr><w:r><w:t>第一章</w:t></w:r></w:p>',
        extra={"word/styles.xml": styles})
    assert "# 第一章" in reader.render(str(path))


def test_both_list_kinds_render_with_their_own_markers(tmp_path):
    out = reader.render(str(_paper(tmp_path)))
    assert "- first\n- second" in out
    assert "1. step one\n2. step two" in out


def test_a_table_row_is_tab_separated(tmp_path):
    out = reader.render(str(_paper(tmp_path)))
    assert "region\tunits" in out
    assert "|" not in out


def test_a_vertically_merged_cell_keeps_its_value_once(tmp_path):
    """A continued vertical merge repeats the origin's text in the file. Emitting it again
    would make one value read as two."""
    body = (
        '<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid>'
        '<w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr>'
        '<w:p><w:r><w:t>North</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>800</w:t></w:r></w:p></w:tc></w:tr>'
        '<w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr>'
        '<w:p><w:r><w:t>North</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>440</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
    out = reader.render(str(_hand_built(tmp_path, body)))
    assert out.count("North") == 1
    assert "\t440" in out


def test_a_horizontally_merged_cell_still_occupies_its_columns(tmp_path):
    body = (
        '<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/><w:gridCol/></w:tblGrid>'
        '<w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>'
        '<w:p><w:r><w:t>span</w:t></w:r></w:p></w:tc>'
        '<w:tc><w:p><w:r><w:t>last</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
    out = reader.render(str(_hand_built(tmp_path, body)))
    assert "`table 1×3`" in out
    assert "span\t\tlast" in out


def test_a_tracked_deletion_is_not_part_of_the_document(tmp_path):
    body = ('<w:p><w:r><w:t xml:space="preserve">kept </w:t></w:r>'
            '<w:del><w:r><w:delText>removed</w:delText></w:r></w:del>'
            '<w:ins><w:r><w:t>added</w:t></w:r></w:ins></w:p>')
    out = reader.render(str(_hand_built(tmp_path, body)))
    assert "kept added" in out
    assert "removed" not in out


def test_an_image_alt_is_rendered_and_an_unnamed_one_is_numbered(tmp_path):
    document = docx.Document()
    document.add_paragraph("before")
    document.add_picture(str(_png(tmp_path)))
    path = tmp_path / "pic.docx"
    document.save(path)
    assert "![image 1]" in reader.render(str(path))


def _png(tmp_path):
    path = tmp_path / "dot.png"
    path.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
        "de0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e"
        "44ae426082"))
    return path


# ---- footnotes -------------------------------------------------------------------------

_FOOTNOTES = {
    "word/footnotes.xml": (
        f'<?xml version="1.0"?><w:footnotes {W}>'
        '<w:footnote w:type="separator" w:id="0"><w:p><w:r><w:separator/></w:r></w:p>'
        '</w:footnote>'
        '<w:footnote w:id="7"><w:p><w:r><w:t>Excludes the divested unit.</w:t></w:r></w:p>'
        '</w:footnote></w:footnotes>'),
    "word/endnotes.xml": (
        f'<?xml version="1.0"?><w:endnotes {W}>'
        '<w:endnote w:type="separator" w:id="0"><w:p><w:r><w:separator/></w:r></w:p>'
        '</w:endnote>'
        '<w:endnote w:id="7"><w:p><w:r><w:t>See appendix B.</w:t></w:r></w:p>'
        '</w:endnote></w:endnotes>'),
}

_NOTE_BODY = (
    '<w:p><w:r><w:t xml:space="preserve">Revenue grew </w:t></w:r>'
    '<w:r><w:footnoteReference w:id="7"/></w:r>'
    '<w:r><w:t xml:space="preserve"> in the third quarter.</w:t></w:r></w:p>'
    '<w:p><w:r><w:t>A second claim</w:t></w:r>'
    '<w:r><w:endnoteReference w:id="7"/></w:r></w:p>')


def test_footnote_bodies_are_in_the_same_page_flow_as_the_body(tmp_path):
    """python-docx has no footnote API, which is why FA's fallback loses them entirely.
    They go in the page flow rather than a sidecar so ``verify_quote`` can lock a quotation
    taken from a footnote the same way it locks one from a paragraph."""
    out = reader.render(str(_hand_built(tmp_path, _NOTE_BODY, extra=_FOOTNOTES)))
    assert "## Footnotes" in out
    assert "[^1]: Excludes the divested unit." in out
    assert "## Endnotes" in out
    assert "[^e1]: See appendix B." in out


def test_the_marker_counts_references_not_word_ids(tmp_path):
    """Word's ids start at 2 and survive deletions, so the raw id is not a reader-facing
    number. This document's only footnote has id 7 and prints as [^1]."""
    out = reader.render(str(_hand_built(tmp_path, _NOTE_BODY, extra=_FOOTNOTES)))
    assert "[^7]" not in out
    assert "[^1]" in out


def test_an_endnote_marker_cannot_be_confused_with_a_footnote_one(tmp_path):
    """Both number from 1 in Word, so both would print [^1] and a reader could not tell
    which section to look it up in."""
    out = reader.render(str(_hand_built(tmp_path, _NOTE_BODY, extra=_FOOTNOTES)))
    assert "A second claim [^e1]" in out


def test_the_separator_rules_are_not_read_as_notes(tmp_path):
    """Ids 0 and 1 are the rules Word draws above the notes; they carry ``w:type``."""
    out = reader.render(str(_hand_built(tmp_path, _NOTE_BODY, extra=_FOOTNOTES)))
    assert out.count("[^1]:") == 1


# ---- what stays quotable ----------------------------------------------------------------

def test_a_sentence_with_a_bold_phrase_in_it_still_verifies(tmp_path):
    """The whole reason emphasis does not go inline. Under ``**revenue**`` the page folds
    to "Total**revenue**rose" and the quotation to "Totalrevenuerose", which is not a
    substring of it -- so the sentence could never be cited."""
    out = reader.render(str(_paper(tmp_path)))
    assert fold("Total revenue rose sharply in the third quarter.") in fold(out)


def test_the_bold_phrase_is_still_named(tmp_path):
    """Moved off the sentence, not dropped: which words carried the emphasis survives."""
    out = reader.render(str(_paper(tmp_path)))
    assert 'bold "revenue"' in out
    assert 'italic "sharply"' in out


def test_a_line_that_is_bold_end_to_end_keeps_its_markers(tmp_path):
    """Markers at the two ends cost a quotation of anything inside the line nothing, so
    there is no reason to drop them."""
    document = docx.Document()
    paragraph = document.add_paragraph()
    run = paragraph.add_run("Everything here is emphasised")
    run.bold = True
    path = tmp_path / "bold.docx"
    document.save(path)
    out = reader.render(str(path))
    assert "**Everything here is emphasised**" in out
    assert fold("here is emphasised") in fold(out)


def test_a_sentence_carrying_a_footnote_still_verifies(tmp_path):
    """pandoc writes ``grew[^1] in``; that page folds to "grew[^1]in" and the sentence
    cannot be found in it."""
    out = reader.render(str(_hand_built(tmp_path, _NOTE_BODY, extra=_FOOTNOTES)))
    assert fold("Revenue grew in the third quarter.") in fold(out)


def test_two_cells_of_one_row_verify_as_one_quotation(tmp_path):
    """Under a pipe table the row folds to "|North|800|" and "North 800" is not in it."""
    out = reader.render(str(_paper(tmp_path)))
    assert fold("North 800") in fold(out)


def test_a_hyperlink_target_is_kept_without_splitting_the_sentence(tmp_path):
    """``[Q3](http://x)`` puts "](" inside the sentence. The autolink goes after it."""
    rels = (
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
        'package/2006/relationships"><Relationship Id="rId9" Type="http://schemas.'
        'openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
        'Target="https://example.org/q3" TargetMode="External"/></Relationships>')
    body = ('<w:p><w:r><w:t xml:space="preserve">See the </w:t></w:r>'
            '<w:hyperlink r:id="rId9"><w:r><w:t>third quarter</w:t></w:r></w:hyperlink>'
            '<w:r><w:t xml:space="preserve"> figures.</w:t></w:r></w:p>')
    path = _hand_built(tmp_path, body, extra={"word/_rels/document.xml.rels": rels})
    out = reader.render(str(path))
    assert "<https://example.org/q3>" in out
    assert fold("See the third quarter figures.") in fold(out)


# ---- edges -------------------------------------------------------------------------------

def test_the_document_title_comes_from_its_own_properties(tmp_path):
    document = docx.Document()
    document.core_properties.title = "A Study of Something"
    document.add_paragraph("body")
    path = tmp_path / "titled.docx"
    document.save(path)
    assert reader.title(str(path)) == "A Study of Something"


def test_a_document_with_no_title_property_reports_none(tmp_path):
    assert reader.title(str(_paper(tmp_path))) == ""


def test_something_that_is_not_a_word_document_is_refused_by_name(tmp_path):
    path = tmp_path / "fake.docx"
    path.write_bytes(b"not a zip at all")
    with pytest.raises(ValueError) as caught:
        reader.render(str(path))
    assert "fake.docx" in str(caught.value)


def test_an_empty_document_renders_to_its_header_alone(tmp_path):
    document = docx.Document()
    path = tmp_path / "empty.docx"
    document.save(path)
    out = reader.render(str(path))
    assert out.startswith("<!-- docx readout")
    assert out.strip().count("\n") == 0


def test_a_footnote_verifies_out_of_the_corpus_like_any_other_sentence(tmp_path, monkeypatch):
    """The reason the notes go in the page flow. A paper puts the qualification on its own
    claim in a footnote, so a footnote that cannot be cited is a claim that cannot be
    qualified."""
    monkeypatch.setenv("MISAKA_PAGEINDEX", str(tmp_path / "corpus"))
    from misaka.core.documents import index as corpus

    path = _hand_built(tmp_path, _NOTE_BODY, extra=_FOOTNOTES)
    doc_id, _pages = corpus.ingest(str(path))
    assert corpus.verify_quote(doc_id, "Excludes the divested unit.") is not None
    assert corpus.verify_quote(doc_id, "Revenue grew in the third quarter.") is not None


def test_a_document_pages_at_its_top_level_headings(tmp_path, monkeypatch):
    monkeypatch.setenv("MISAKA_PAGEINDEX", str(tmp_path / "corpus"))
    from misaka.core.documents import index as corpus

    document = docx.Document()
    for chapter in ("One", "Two", "Three"):
        document.add_heading(chapter, 1)
        document.add_paragraph(f"body of {chapter}")
    path = tmp_path / "chapters.docx"
    document.save(path)
    doc_id, pages = corpus.ingest(str(path))
    assert pages == 3
    assert corpus.verify_quote(doc_id, "body of Three")["page"] == 3


def test_the_word_title_reaches_the_corpus_metadata(tmp_path):
    from misaka.core.documents import index as corpus

    document = docx.Document()
    document.core_properties.title = "A Study of Something"
    document.add_paragraph("body")
    path = tmp_path / "titled.docx"
    document.save(path)
    assert corpus.source_title(str(path)) == "A Study of Something"
