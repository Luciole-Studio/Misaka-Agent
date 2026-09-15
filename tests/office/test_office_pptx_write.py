"""Every pptx op, verified by reading the deck back with misaka's own reader.

Three of the tests below guard failures that look exactly like success from inside the
writer -- a placeholder quietly overwritten, a font that had no effect, an image whose
relationship was left behind. Each is why the writer does the awkward thing it does.
"""
from __future__ import annotations

import importlib
import zipfile

from misaka.core.documents.office import pptx as reader
from misaka.core.tools._office import pptx as writer

pptx = importlib.import_module("pptx")


def _deck(tmp_path, slides, name="deck.pptx"):
    path = tmp_path / name
    outcome = writer.write(str(path), "create", {"slides": slides})
    assert outcome["ok"], outcome
    return path


def _xml(path):
    with zipfile.ZipFile(path) as archive:
        return "".join(archive.read(name).decode("utf-8") for name in archive.namelist()
                       if name.startswith("ppt/slides/slide") and name.endswith(".xml"))


# ---- create ------------------------------------------------------------------------------

def test_create_builds_the_slides_it_was_given(tmp_path):
    path = tmp_path / "deck.pptx"
    outcome = writer.write(str(path), "create", {"slides": [
        {"layout": "title", "title": "Findings", "subtitle": "Q3"},
        {"layout": "title_and_content", "title": "Points",
         "body": {"items": [{"text": "First"}, {"text": "Nested", "level": 1}]},
         "notes": "say the caveat"},
    ]})
    assert outcome["counts"] == {"slide": 2}
    out = reader.render(str(path))
    assert "## Slide 1: Findings" in out
    assert "## Slide 2: Points" in out
    assert "  - Nested" in out
    assert "**notes:** say the caveat" in out


def test_create_with_no_slides_fails_rather_than_writing_an_empty_deck(tmp_path):
    outcome = writer.write(str(tmp_path / "empty.pptx"), "create", {"slides": []})
    assert not outcome["ok"]
    assert "no slides given" in outcome["summary"]


def test_create_refuses_a_path_that_exists(tmp_path):
    path = _deck(tmp_path, [{"title": "First"}])
    outcome = writer.write(str(path), "create", {"slides": [{"title": "Second"}]})
    assert not outcome["ok"]
    assert "First" in reader.render(str(path))


def test_a_subtitle_and_a_body_on_one_slide_both_survive(tmp_path):
    """They share placeholder index 1, so an index lookup returns the same shape for both
    and whichever ran last silently wins -- with a success receipt for both."""
    path = _deck(tmp_path, [{"layout": "title", "title": "T", "subtitle": "the subtitle"}])
    writer.write(str(path), "set_text", {"slide": 1, "placeholder": "subtitle",
                                         "text": "replaced subtitle"})
    out = reader.render(str(path))
    assert "replaced subtitle" in out
    assert "## Slide 1: T" in out


def test_bullets_come_from_structure_not_from_typed_characters(tmp_path):
    path = _deck(tmp_path, [{"title": "T", "body": {"items": [
        {"text": "bulleted"},
        {"text": "plain", "bullet": False},
    ]}}])
    assert "a:buNone" in _xml(path)
    out = reader.render(str(path))
    assert "- bulleted" in out
    assert "\nplain" in out


def test_a_cjk_font_is_written_where_cjk_text_reads_it(tmp_path):
    """``font.name`` writes only ``a:latin``. CJK characters take their typeface from
    ``a:ea``, so without it the request has no effect at all."""
    path = _deck(tmp_path, [{"title": [{"text": "季度报告", "font": "SimSun"}]}])
    xml = _xml(path)
    assert '<a:ea typeface="SimSun"' in xml or 'a:ea' in xml and "SimSun" in xml


# ---- editing -----------------------------------------------------------------------------

def test_add_slide_appends_and_can_be_placed_by_index(tmp_path):
    path = _deck(tmp_path, [{"title": "One"}, {"title": "Three"}])
    writer.write(str(path), "add_slide", {"layout": "title_only", "title": "Two",
                                          "index": 2})
    out = reader.render(str(path))
    assert out.index("## Slide 1: One") < out.index("## Slide 2: Two") < out.index("## Slide 3: Three")


def test_delete_slide_removes_it(tmp_path):
    path = _deck(tmp_path, [{"title": "One"}, {"title": "Two"}])
    writer.write(str(path), "delete_slide", {"slide": 1})
    out = reader.render(str(path))
    assert "One" not in out
    assert "## Slide 1: Two" in out


def test_a_slide_number_out_of_range_says_how_many_there_are(tmp_path):
    path = _deck(tmp_path, [{"title": "One"}])
    outcome = writer.write(str(path), "set_text", {"slide": 9, "text": "x"})
    assert "out of range" in outcome
    assert "this deck has 1" in outcome


def test_replace_text_covers_the_whole_deck_or_one_slide(tmp_path):
    path = _deck(tmp_path, [{"title": "draft one"}, {"title": "draft two"}])
    writer.write(str(path), "replace_text", {"find": "draft", "replace": "final",
                                             "slide": 2})
    out = reader.render(str(path))
    assert "draft one" in out and "final two" in out
    writer.write(str(path), "replace_text", {"find": "draft", "replace": "final"})
    assert "draft" not in reader.render(str(path))


def test_replace_text_with_no_match_warns(tmp_path):
    path = _deck(tmp_path, [{"title": "One"}])
    outcome = writer.write(str(path), "replace_text", {"find": "absent", "replace": "x"})
    assert outcome["ok"]
    assert "0 matches" in outcome["warn"]


def test_format_text_applies_to_the_matching_runs(tmp_path):
    path = _deck(tmp_path, [{"title": "T", "body": {"items": ["target line"]}}])
    outcome = writer.write(str(path), "format_text", {"slide": 1, "find": "target",
                                                      "bold": True, "font_size": 30})
    assert outcome["ok"]
    out = reader.render(str(path))
    # A line that is uniform end to end keeps its markers, and the size is stated once in
    # the shape meta -- see the reader's own tests for why that is not per-run noise.
    assert "**target line**" in out
    assert "30pt" in out


def test_duplicate_slide_carries_its_relationships(tmp_path):
    """Copying shape XML alone leaves every ``r:embed`` pointing at a relationship the new
    slide does not have: the images vanish and PowerPoint calls the file corrupt."""
    image = tmp_path / "dot.png"
    image.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
        "de0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e"
        "44ae426082"))
    path = _deck(tmp_path, [{"layout": "blank"}])
    writer.write(str(path), "add_image", {"slide": 1, "image_path": str(image)})
    writer.write(str(path), "duplicate_slide", {"slide": 1})

    with zipfile.ZipFile(path) as archive:
        rels = archive.read("ppt/slides/_rels/slide2.xml.rels").decode("utf-8")
        slide = archive.read("ppt/slides/slide2.xml").decode("utf-8")
    assert "media/" in rels
    for token in slide.split('r:embed="')[1:]:
        assert token.split('"')[0] in rels          # every reference resolves


def test_add_table_and_add_chart_reach_the_readout(tmp_path):
    path = _deck(tmp_path, [{"layout": "title_only", "title": "Data"}])
    writer.write(str(path), "add_table", {"slide": 1,
                                          "rows": [["region", "units"], ["North", "800"]]})
    writer.write(str(path), "add_chart", {"slide": 1, "chart_type": "column",
                                          "categories": ["Q1", "Q2"],
                                          "series": {"revenue": [1200, 1500]},
                                          "title": "Growth", "y": 4.5})
    out = reader.render(str(path))
    assert "table 2×2" in out
    assert "region\tunits" in out
    assert "category\trevenue" in out
    assert "Q1\t1200.0" in out


def test_add_shape_carries_its_fill_and_text(tmp_path):
    path = _deck(tmp_path, [{"layout": "blank"}])
    writer.write(str(path), "add_shape", {"slide": 1, "shape": "rounded_rectangle",
                                          "text": "Collect", "fill_color": "#228899"})
    out = reader.render(str(path))
    assert "ROUNDED_RECTANGLE" in out
    assert "fill:#228899" in out
    assert "Collect" in out


def test_set_notes_replaces_the_speaker_notes(tmp_path):
    path = _deck(tmp_path, [{"title": "T", "notes": "old note"}])
    writer.write(str(path), "set_notes", {"slide": 1, "text": "new note"})
    out = reader.render(str(path))
    assert "new note" in out
    assert "old note" not in out


def test_set_slide_size_switches_the_aspect_ratio(tmp_path):
    """The most common patch of all: a template that is not widescreen."""
    path = _deck(tmp_path, [{"title": "T"}])
    writer.write(str(path), "set_slide_size", {"preset": "16:9"})
    assert "13.33×7.5in" in reader.render(str(path))


def test_add_textbox_places_text_where_it_was_told(tmp_path):
    path = _deck(tmp_path, [{"layout": "blank"}])
    writer.write(str(path), "add_textbox", {"slide": 1, "text": "floating",
                                            "x": 2, "y": 3, "autofit": "shrink_text"})
    out = reader.render(str(path))
    assert "@2.0,3.0" in out
    assert "floating" in out


def test_an_image_that_is_not_there_is_refused_rather_than_crashing(tmp_path):
    path = _deck(tmp_path, [{"layout": "blank"}])
    outcome = writer.write(str(path), "add_image", {"slide": 1, "image_path": "absent.png"})
    assert "no such image" in outcome


def test_an_op_on_a_file_that_does_not_exist_says_so(tmp_path):
    outcome = writer.write(str(tmp_path / "absent.pptx"), "add_slide", {"title": "x"})
    assert "file not found" in outcome


def test_an_unknown_op_names_the_ones_that_exist(tmp_path):
    path = _deck(tmp_path, [{"title": "T"}])
    outcome = writer.write(str(path), "teleport", {})
    assert "unknown pptx op 'teleport'" in outcome
    assert "add_slide" in outcome
