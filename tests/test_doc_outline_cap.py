"""``doc_outline`` returns a document's whole heading tree, which has no natural bound. It now
gets the same kind of cap as the workspace index and find/grep: a huge tree is cut, the cut says
how to keep navigating (sections by node or pages, doc_find beyond the cut); an ordinary
outline passes through untouched with its usual reading hint. A document with no tree is outlined
by its pages, every one of them, under the same cap."""
from types import SimpleNamespace as NS

import pytest

from misaka.core.documents.wiring import documents
from misaka.core.wiring import ToolCollector


@pytest.fixture
def outline(tmp_path, monkeypatch):
    monkeypatch.setattr(documents, "_owning_root", lambda doc_id, ctx: str(tmp_path))
    monkeypatch.setattr(documents, "_row", lambda doc_id, root: {})
    monkeypatch.setattr(documents, "_ocr_note", lambda row: "")
    collector = ToolCollector()
    documents.register(collector)
    tool = next(t for t in collector.tools if t.name == "doc_outline")

    async def call():
        result = await tool.execute("call", {"doc_id": "d"}, None, None, NS(cwd=str(tmp_path), model=None))
        return result["content"][0]["text"]

    return call


async def test_a_huge_outline_is_cut_with_navigation_advice(outline, monkeypatch):
    monkeypatch.setattr(documents.corpus, "tree_outline",
                        lambda doc_id, workspace: "\n".join(f"{i:05d}  Heading {i} " + "x" * 60 for i in range(20000)))
    text = await outline()
    assert len(text.encode("utf-8")) < documents.OUTLINE_MAX_BYTES + 2048
    assert "00000  Heading 0 " in text and "19999  Heading 19999 " not in text
    assert "limit reached" in text and "doc_find" in text and "pages=<range>" in text


async def test_an_ordinary_outline_passes_through(outline, monkeypatch):
    monkeypatch.setattr(documents.corpus, "tree_outline", lambda doc_id, workspace: "0000  Book I\n0001  Chapter 1\n")
    text = await outline()
    assert "0000  Book I\n0001  Chapter 1" in text
    assert "Use doc_read(doc_id, node=<node-id>) to read a section." in text and "limit reached" not in text


def _pages(count):
    return {"mode": "pages only, no structure tree", "title": "Deck",
            "pages": [{"page": n, "head": f"Slide {n} title"} for n in range(1, count + 1)]}


@pytest.fixture
def by_page(outline, monkeypatch):
    monkeypatch.setattr(documents.corpus, "tree_outline", lambda doc_id, workspace: "")
    monkeypatch.setattr(documents.corpus, "figures", lambda doc_id, workspace: [])
    monkeypatch.setattr(documents.corpus, "pageindex_available", lambda: True)

    def serve(count):
        monkeypatch.setattr(documents.corpus, "structure", lambda doc_id, workspace: _pages(count))
        return outline()

    return serve


async def test_a_document_without_a_tree_lists_every_page(by_page):
    # It listed the first eighty and said nothing: a 120-slide deck read as an eighty-slide one.
    text = await by_page(120)
    assert [f"p{n} " in text for n in (1, 80, 81, 120)] == [True] * 4
    assert text.count("\n  p") == 120 and "limit reached" not in text


async def test_a_page_outline_past_the_cap_says_how_much_it_shows(by_page, monkeypatch):
    monkeypatch.setattr(documents, "OUTLINE_MAX_BYTES", 600)
    text = await by_page(120)
    shown = text.count("\n  p")
    assert 0 < shown < 120
    assert f"limit reached: {shown} of 120 pages shown" in text and "pages=<range>" in text
    assert text.index("END-UNTRUSTED-DATA") < text.index("limit reached")   # the notice is ours
