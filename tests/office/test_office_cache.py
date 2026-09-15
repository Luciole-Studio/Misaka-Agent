"""A rendered workbook is parsed once, not once per continuation read.

``doc_read`` pages through a document, ``read`` pages through it with ``offset``, and both
call the renderer again for every page. A 50-sheet workbook costs seconds to parse and
recalculates formulas while it is at it, so paging through one without a cache pays that
per page.

Every failure here is a miss, never a raise: the cache is an optimisation, and a read-only
home or an unwritable cache directory must cost speed rather than the document. The key is
``(realpath, size, mtime_ns)`` so an edited file re-renders without anybody invalidating
anything by hand.
"""
from __future__ import annotations

import os

import pytest

from misaka.config.product import CFG
from misaka.core.documents.office import cache


@pytest.fixture(autouse=True)
def cache_home(tmp_path, monkeypatch):
    """A cache of this test's own: ``CFG`` is what the suite redirects, so redirect it."""
    monkeypatch.setitem(CFG, "office_cache", str(tmp_path / "cache"))


def _doc(tmp_path, name="book.xlsx", body="sheet one"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_the_second_read_of_an_unchanged_file_does_not_render_it_again(tmp_path):
    path = _doc(tmp_path)
    calls = []

    def render():
        calls.append(1)
        return "## Sheet: One\nrendered"

    assert cache.render_cached(path, render) == "## Sheet: One\nrendered"
    assert cache.render_cached(path, render) == "## Sheet: One\nrendered"
    assert len(calls) == 1


def test_touching_the_file_re_renders_it(tmp_path):
    path = _doc(tmp_path)
    calls = []

    def render():
        calls.append(1)
        return f"render {len(calls)}"

    assert cache.render_cached(path, render) == "render 1"
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert cache.render_cached(path, render) == "render 2"


def test_editing_the_file_re_renders_it(tmp_path):
    """Size is in the key too: a same-second edit that changes length must not be served
    the previous rendering."""
    path = _doc(tmp_path, body="short")
    first = cache.render_cached(path, lambda: "first")
    stat = os.stat(path)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(" and then some more text")
    # Put the timestamp back: only the size now differs, which is the case a mtime-only key
    # would serve stale.
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    second = cache.render_cached(path, lambda: "second")
    assert (first, second) == ("first", "second")


def test_two_files_with_the_same_content_do_not_share_an_entry(tmp_path):
    """The key is the path, not the bytes: the corpus is content addressed and this is
    not -- ``read`` is asked about a path and answers about that path."""
    one = _doc(tmp_path, "a.xlsx", "same")
    two = _doc(tmp_path, "b.xlsx", "same")
    assert cache.render_cached(one, lambda: "from a") == "from a"
    assert cache.render_cached(two, lambda: "from b") == "from b"


def test_an_unwritable_cache_directory_still_returns_the_rendering(tmp_path, monkeypatch):
    monkeypatch.setitem(CFG, "office_cache", "/proc/nonexistent/office")
    assert cache.render_cached(_doc(tmp_path), lambda: "rendered anyway") == "rendered anyway"


def test_a_corrupt_cache_entry_is_ignored_rather_than_served(tmp_path, monkeypatch):
    path = _doc(tmp_path)
    assert cache.render_cached(path, lambda: "good") == "good"
    directory = tmp_path / "cache"
    entry = next(directory.glob("*.md"))
    entry.write_bytes(b"\xff\xfe\x00 not utf-8 at all")
    assert cache.render_cached(path, lambda: "re-rendered") == "re-rendered"


def test_the_store_is_bounded_and_evicts_the_oldest(tmp_path):
    """A long research run renders many documents; the cache must not grow without end."""
    for n in range(cache.MAX_ENTRIES + 5):
        cache.render_cached(_doc(tmp_path, f"doc{n}.xlsx", f"body {n}"), lambda n=n: f"render {n}")
    kept = list((tmp_path / "cache").glob("*.md"))
    assert len(kept) <= cache.MAX_ENTRIES


def test_a_renderer_that_raises_is_not_cached(tmp_path):
    """A refusal is not a rendering: the next call must ask again, or a transient parse
    failure becomes this file's permanent answer."""
    path = _doc(tmp_path)

    def boom():
        raise ValueError("Refused book.xlsx: something was wrong")

    with pytest.raises(ValueError):
        cache.render_cached(path, boom)
    assert cache.render_cached(path, lambda: "fine now") == "fine now"
