"""The figures a text layer does not carry: named where the model reads, shown on request.

2026-10-07: doc_page_image could show a PDF page, but nothing told the model which pages held a
map, a chart or a plate, so it looked only when the prose happened to say "see figure 3". And a
model without vision got nothing at all: the image is dropped from its request. FrontierAgent's
reader names a page's figures as it reads ("figure not read: embedded image (covers 35% of
page)"); a model without vision is read the picture by the vision extension
(tests/test_vision_extension.py)."""
import base64
import ctypes
import io
import json
import os
from types import SimpleNamespace as NS

import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw
from pypdfium2 import raw

from misaka.core.documents import index as corpus
from misaka.core.documents.wiring import documents
from misaka.core.wiring import ToolCollector

BODY = ("The campaign of 1812 is told here from the letters of the officers who served in it, ",
        "the orders of the day that reached them, and the reports that went back to Paris; ",
        "each chapter follows one corps from the Niemen to the Berezina and back again, ",
        "and the maps show where it stood on the evening of each day of the march.")


def _page(images=(), strokes=0, ink=None, size=(600, 800)):
    return {"cover": 0.0, "marked": True, "images": [list(b) for b in images], "strokes": strokes,
            "ink": list(ink) if ink else None, "size": list(size), "rotation": 0}


def test_an_image_is_a_figure_and_a_logo_is_not():
    found = corpus._figure_inventory({1: _page([(100, 300, 400, 500)]), 2: _page([(10, 10, 60, 40)])}, 2)
    assert list(found) == [1]
    assert found[1]["images"] == 1 and found[1]["cover"] == pytest.approx(0.125)


def test_a_box_on_most_pages_is_a_background_not_a_figure():
    pages = {n: _page([(50, 50, 550, 300)]) for n in range(1, 7)}
    assert corpus._figure_inventory(pages, 6) == {}


def test_a_scanned_page_is_not_a_figure_of_itself_and_its_two_layers_count_once():
    scan = [(0, 0, 600, 800), (0, 0, 600, 800)]
    assert corpus._figure_inventory({1: _page(scan)}, 1) == {}
    assert corpus._figure_inventory({1: _page([(100, 300, 400, 500)] * 2)}, 1)[1]["images"] == 1


def test_a_drawing_is_a_figure_and_a_ruled_page_is_not():
    found = corpus._figure_inventory({1: _page(strokes=150, ink=(100, 100, 400, 300)), 2: _page(strokes=40, ink=(0, 0, 9, 9))}, 2)
    assert list(found) == [1] and found[1]["strokes"] == 150 and found[1]["boxes"] == [[100, 100, 400, 300]]


def test_a_scanned_page_with_little_text_is_named_as_a_possible_map_or_plate():
    scan = [(0, 0, 600, 800)]
    signals = {n: _page(scan) for n in range(1, 6)}
    texts = ["\n".join(f"{line} {word}" for line in BODY * 3) for word in ("Vilna", "Vitebsk", "Borodino", "Moscow")]
    texts.append("Map 3. Smolensk")
    found = corpus._figure_inventory(signals, 5, texts)
    assert list(found) == [5] and found[5]["sparse"] < corpus.SCAN_SPARSE


def test_the_reader_is_told_which_figure_is_which():
    record = {"cover": 0.35, "images": 2, "strokes": 230, "boxes": [[0, 0, 1, 1]] * 3, "size": [600, 800], "rotation": 0}
    line = documents._figure_line(12, record)
    assert "2 images covering 35% of the page (figure=1..2)" in line
    assert "a drawing of 230 strokes" in line and "(figure=3)" in line
    notes = documents._page_notes({}, 10, 14, {12: record, 30: record})
    assert "Page 12 carries" in notes and "Page 30" not in notes


def _typeset(pdf, page, line, y):
    obj = raw.FPDFPageObj_NewTextObj(pdf.raw, b"Helvetica", 10.0)
    data = (line + "\0").encode("utf-16-le")
    assert raw.FPDFText_SetText(obj, (ctypes.c_ushort * (len(data) // 2)).from_buffer_copy(data))
    raw.FPDFPageObj_Transform(obj, 1, 0, 0, 1, 50, y)
    raw.FPDFPage_InsertObject(page.raw, obj)


def figure_pdf(path):
    """Three typeset pages: the second with a map (an image), the third with a chart (lines)."""
    pdf = pdfium.PdfDocument.new()
    for n in (1, 2, 3):
        page = pdf.new_page(595, 842)
        for i, line in enumerate(BODY):
            _typeset(pdf, page, f"{line} [{n}]", 780 - 14 * i)
        if n == 2:
            picture = Image.new("L", (600, 400), 255)
            ImageDraw.Draw(picture).text((40, 180), "SMOLENSK", fill=0)
            image = pdfium.PdfImage.new(pdf)
            image.set_bitmap(pdfium.PdfBitmap.from_pil(picture))
            image.set_matrix(pdfium.PdfMatrix().scale(300, 200).translate(150, 300))
            page.insert_obj(image)
        if n == 3:
            line = raw.FPDFPageObj_CreateNewPath(100, 100)
            for i in range(150):
                raw.FPDFPath_LineTo(line, 100 + 2 * i, 100 + (i * 37) % 200)
            raw.FPDFPath_SetDrawMode(line, raw.FPDF_FILLMODE_NONE, True)
            raw.FPDFPage_InsertObject(page.raw, line)
        page.gen_content()
    pdf.save(str(path))
    return path


@pytest.fixture
def indexed(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    book = figure_pdf(workspace / "march.pdf")
    doc_id, _ = corpus.ingest(str(book), workspace=str(workspace), with_tree=False)
    return doc_id, str(workspace)


def test_figures_are_found_once_and_kept_beside_the_document(indexed, monkeypatch):
    doc_id, workspace = indexed
    found = corpus.figures(doc_id, workspace=workspace)
    assert sorted(found) == [2, 3]
    assert found[2]["images"] == 1 and found[2]["cover"] == pytest.approx(60000 / (595 * 842), abs=0.01)
    assert found[3]["strokes"] > corpus.FIGURE_STROKES
    assert os.path.isfile(os.path.join(corpus.resolve_doc(doc_id, workspace=workspace), "figures.json"))
    monkeypatch.setattr(corpus, "_pdf_visuals", lambda *a: pytest.fail("read again"))
    assert corpus.figures(doc_id, workspace=workspace) == found


def test_a_page_read_by_ocr_is_not_listed_as_a_figure(indexed):
    doc_id, workspace = indexed
    ddir = corpus.resolve_doc(doc_id, workspace=workspace)
    with open(os.path.join(ddir, "meta.json"), encoding="utf-8") as f:
        meta = json.load(f)
    meta.update(ocr=True, ocr_pages=[2])
    with open(os.path.join(ddir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    assert sorted(corpus.figures(doc_id, workspace=workspace)) == [3]


async def _call(tool_name, args, model=None):
    collector = ToolCollector()
    documents.register(collector)
    tool = next(t for t in collector.tools if t.name == tool_name)
    return await tool.execute("call", args, None, None, NS(cwd=args.pop("_cwd"), model=model,
                                                            modelRegistry=None, signal=None))


async def test_doc_read_names_the_figures_on_the_pages_it_returns(indexed):
    doc_id, workspace = indexed
    result = await _call("doc_read", {"doc_id": doc_id, "pages": "1-3", "_cwd": workspace})
    text = result["content"][0]["text"]
    assert "Page 2 carries what its text does not: an image covering 12% of the page (figure=1)" in text
    assert "Page 3 carries what its text does not: a drawing of" in text and "Page 1 carries" not in text


async def test_a_figure_is_rendered_alone_at_the_resolution_its_labels_need(indexed):
    doc_id, workspace = indexed
    page = await _call("doc_page_image", {"doc_id": doc_id, "page": 2, "_cwd": workspace})
    figure = await _call("doc_page_image", {"doc_id": doc_id, "page": 2, "figure": 1, "_cwd": workspace})
    assert figure["details"]["figure"] == 1 and "figure 1" in figure["content"][0]["text"]
    whole, alone = page["details"], figure["details"]
    assert alone["width"] > 2 * whole["width"] * 300 / 595
    with pytest.raises(ValueError, match="has 1 figure"):
        await _call("doc_page_image", {"doc_id": doc_id, "page": 2, "figure": 2, "_cwd": workspace})


async def test_a_model_without_vision_gets_the_image_for_the_vision_extension_to_read(indexed, monkeypatch):
    """The page tool reads nothing to a model itself: the vision extension does, for every image."""
    doc_id, workspace = indexed
    from misaka.core.web import vision
    monkeypatch.setattr(vision, "selected_model", lambda: ("openai/gpt-4o", "vision.model"))
    result = await _call("doc_page_image", {"doc_id": doc_id, "page": 2, "figure": 1, "_cwd": workspace}, NS(input=["text"]))
    assert any(getattr(part, "mimeType", None) for part in result["content"])
    assert "will be omitted" not in result["content"][0]["text"]


async def test_without_a_vision_model_a_text_model_is_told_how_to_get_one(indexed, monkeypatch):
    doc_id, workspace = indexed
    from misaka.core.web import vision
    monkeypatch.setattr(vision, "selected_model", lambda: (None, None))
    result = await _call("doc_page_image", {"doc_id": doc_id, "page": 2, "_cwd": workspace}, NS(input=["text"]))
    assert "vision.model" in result["content"][0]["text"]


async def test_a_djvu_page_is_rendered_too(tmp_path):
    from tests.test_djvu_documents import build_djvu
    workspace = tmp_path / "ws"
    workspace.mkdir()
    book = build_djvu(workspace, pages=2)
    doc_id, _ = corpus.ingest(str(book), workspace=str(workspace), with_tree=False)
    result = await _call("doc_page_image", {"doc_id": doc_id, "page": 2, "_cwd": str(workspace)})
    image = next(part for part in result["content"] if getattr(part, "mimeType", None))
    assert Image.open(io.BytesIO(base64.b64decode(image.data))).size[0] > 0
    assert "page 2 of 2" in result["content"][0]["text"]
