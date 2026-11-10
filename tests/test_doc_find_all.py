"""A search lists every page that matches, for the model and at the command line alike, and the
tool says so when its byte guard cuts the list."""
import hashlib
import json
import re
from types import SimpleNamespace as NS

import pytest

from misaka.cli import app
from misaka.core.documents import index as corpus
from misaka.core.documents.wiring import documents
from misaka.core.wiring import ToolCollector


def _doc(root, name, added_at, pages):
    sha = hashlib.sha256(name.encode()).hexdigest()
    doc_id = sha[:12]
    folder = root / ".pageindex" / doc_id
    (folder / "pages").mkdir(parents=True)
    source = root / f"{name}.txt"
    source.write_text("source", encoding="utf-8")
    (folder / "meta.json").write_text(json.dumps({
        "doc_id": doc_id, "sha256": sha, "title": name, "orig_path": str(source),
        "pages": len(pages), "added_at": added_at}), encoding="utf-8")
    for number, text in enumerate(pages, 1):
        (folder / "pages" / f"p{number:04d}.txt").write_text(text, encoding="utf-8")
    return doc_id


@pytest.fixture
def find(tmp_path):
    collector = ToolCollector()
    documents.register(collector)
    tool = next(t for t in collector.tools if t.name == "doc_find")

    async def call(**params):
        result = await tool.execute("call", params, None, None, NS(cwd=str(tmp_path), model=None))
        return result["content"][0]["text"]

    return call


def _lines(text):
    return [line for line in text.splitlines() if re.match(r"[0-9a-f]{12} p\d+", line)]


async def test_every_match_is_listed_across_documents(tmp_path, find):
    ids = [_doc(tmp_path, f"doc{n}", n, [f"page {page} needle here" for page in range(6)]) for n in range(4)]
    text = await find(query="needle")
    assert len(_lines(text)) == 24
    assert f"{ids[-1]} p6" in text        # the newest document is reached
    assert "limit reached" not in text


async def test_every_match_within_one_document_is_listed(tmp_path, find):
    one = _doc(tmp_path, "one", 1, [f"page {page} needle here" for page in range(30)])
    other = _doc(tmp_path, "other", 2, ["needle elsewhere"])
    text = await find(query="needle", doc_id=one)
    assert len(_lines(text)) == 30 and other not in text


async def test_the_byte_guard_says_how_much_it_shows(tmp_path, find, monkeypatch):
    _doc(tmp_path, "one", 1, [f"page {page} needle here" for page in range(60)])
    monkeypatch.setattr(documents, "FIND_MAX_BYTES", 600)
    text = await find(query="needle")
    shown = len(_lines(text))
    assert 0 < shown < 60
    assert f"limit reached: {shown} of 60 matching pages shown" in text and "doc_id" in text
    assert text.index("END-UNTRUSTED-DATA") < text.index("limit reached")   # the notice is ours


async def test_no_match_still_says_so(tmp_path, find):
    _doc(tmp_path, "one", 1, ["nothing here"])
    assert await find(query="needle") == "No matches."


def test_the_search_itself_has_no_limit(tmp_path):
    _doc(tmp_path, "one", 1, [f"page {page} needle here" for page in range(30)])
    assert len(corpus.search_literal("needle", workspace=str(tmp_path))) == 30


def _doc_command(tmp_path, monkeypatch, capsys, *argv):
    monkeypatch.chdir(tmp_path)
    app._cmd_doc(app._parser().parse_args(["doc", *argv]))
    return capsys.readouterr().out


def test_the_command_line_lists_every_page_too(tmp_path, monkeypatch, capsys):
    # It stopped at ten and printed "10 match(es)": a book with the word on 27 pages read as ten.
    one = _doc(tmp_path, "one", 1, [f"page {page} needle here, and needle again" for page in range(27)])
    _doc(tmp_path, "other", 2, ["needle elsewhere"])
    out = _doc_command(tmp_path, monkeypatch, capsys, "find", "needle")
    assert len([line for line in out.splitlines() if re.match(r"  [0-9a-f]{12} p\d+", line)]) == 28
    assert "28 page(s) hold a match." in out and "match(es)" not in out
    out = _doc_command(tmp_path, monkeypatch, capsys, "find", "needle", "--doc", one)
    assert f"{one} p27" in out and "27 page(s) hold a match." in out


def test_the_command_line_tree_lists_every_page(tmp_path, monkeypatch, capsys):
    # A deck has no heading tree, so its outline is one line a slide: forty of 120 read as forty.
    deck = _doc(tmp_path, "deck", 1, [f"Slide {page} title\nbody" for page in range(1, 121)])
    out = _doc_command(tmp_path, monkeypatch, capsys, "tree", deck)
    heads = [line for line in out.splitlines() if re.match(r"  p\d+ +Slide \d+ title$", line)]
    assert len(heads) == 120 and heads[-1].split() == ["p120", "Slide", "120", "title"]
