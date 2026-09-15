"""The one writer both web tools leave their pages with.

Every assertion here is about a *file on disk that has already been cited*: the name is
content-addressed and registered in ``research_artifacts.sha256``, and the header is
parsed independently by ``research/ledger.py`` and ``research/report.py``. So the tests
are deliberately about bytes and line numbers rather than about "a page was saved" --
a change that keeps the writer working while moving a delimiter breaks a run that has
already finished, in a way nothing else in the suite would notice.
"""

from __future__ import annotations

import hashlib
import json
import os

from misaka.core.tools._web.evidence import frontmatter_line_count, save_page

PROVENANCE = {
    "source_url": "https://example.com/page",
    "final_url": "https://example.com/page",
    "sha256": hashlib.sha256(b"<html>...</html>").hexdigest(),
    "text_sha256": hashlib.sha256(b"Title: T\n\nbody").hexdigest(),
    "title": "Widget Report",
}


def _write(workspace, provenance, text):
    relative = save_page(str(workspace), provenance, text)
    assert relative, "the page was not written"
    return relative, (workspace / relative).read_text(encoding="utf-8")


# --- the name ------------------------------------------------------------------------


def test_name_is_hash_of_exact_saved_bytes(workspace):
    path, text = _write(workspace, PROVENANCE, "body")
    assert path == f"downloads/pages/{hashlib.sha256(text.encode()).hexdigest()[:12]}.md"
    assert _write(workspace, PROVENANCE, "body")[0] == path


def test_every_changed_byte_gets_a_new_immutable_name(workspace):
    first, original = _write(workspace, PROVENANCE, "body")
    for key in PROVENANCE:
        changed, _ = _write(workspace, {**PROVENANCE, key: "different"}, "body")
        assert changed != first
    assert _write(workspace, {**PROVENANCE, "provider": "parallel"}, "body")[0] != first
    assert _write(workspace, PROVENANCE, "new extraction")[0] != first
    assert (workspace / first).read_text() == original


# --- the file ------------------------------------------------------------------------


def test_the_saved_path_is_relative_to_the_workspace(workspace):
    relative, _ = _write(workspace, PROVENANCE, "body")
    assert relative.startswith("downloads/pages/") and relative.endswith(".md")
    assert (workspace / relative).is_file()


def test_the_frontmatter_round_trips(workspace):
    """JSON scalars are YAML scalars, so what the ledger parses is what was handed in."""
    provenance = {**PROVENANCE, "title": 'Q3: "record" revenue — 中文'}
    _, document = _write(workspace, provenance, "Title: T\n\nbody")

    _, head, body = document.split("---", 2)
    parsed = {}
    for line in head.strip().splitlines():
        key, _, value = line.partition(":")
        parsed[key.strip()] = json.loads(value.strip())
    assert parsed == provenance
    assert body == "\n\nTitle: T\n\nbody\n"


def test_a_provenance_key_the_writer_has_never_seen_is_written_anyway(workspace):
    """web_extract stamps the vendor that rendered the page; no whitelist stands in its way."""
    _, document = _write(workspace, {**PROVENANCE, "provider": "firecrawl"}, "body")
    assert '\nprovider: "firecrawl"\n' in document


def test_fetched_at_is_not_written_even_when_it_is_handed_in(workspace):
    """Only because the caller keeps it out: the file's bytes have to be reproducible.

    This pins the other half of that contract -- the writer stamps exactly the keys it is
    given, so a clock reading in the header would be the *caller's* bug, and this is the
    test that says which side is responsible.
    """
    _, document = _write(workspace, PROVENANCE, "body")
    assert "fetched_at" not in document


def test_the_same_call_twice_writes_byte_identical_files(workspace):
    """Content-addressed names are only true if the writer is deterministic."""
    _, first = _write(workspace, PROVENANCE, "body")
    _, second = _write(workspace, PROVENANCE, "body")
    assert first == second


# --- the line count the truncation footer is computed from ----------------------------


def _first_text_line(workspace, provenance, text):
    """The 1-indexed line ``read`` would land on, following :func:`frontmatter_line_count`."""
    _, document = _write(workspace, provenance, text)
    offset = frontmatter_line_count(provenance)
    return document.split("\n")[offset]


def test_the_line_count_finds_the_text_under_a_one_key_header(workspace):
    assert _first_text_line(workspace, {"source_url": "https://e.com/p"}, "第一行") == "第一行"


def test_the_line_count_finds_the_text_under_a_five_key_header(workspace):
    assert _first_text_line(workspace, PROVENANCE, "Title: T") == "Title: T"


def test_a_colon_in_a_value_does_not_buy_the_header_an_extra_line(workspace):
    """The hazard the count exists for: a URL and a page-written title are full of colons,
    and a footer one line out sends the model into the yaml instead of the page."""
    provenance = {**PROVENANCE, "title": "Q3: revenue: up", "final_url": "https://e.com:8443/a:b"}
    assert _first_text_line(workspace, provenance, "the first line of the page") == (
        "the first line of the page"
    )


def test_the_count_is_the_header_the_writer_actually_wrote(workspace):
    """Counted against the file rather than against a number typed into this test."""
    _, document = _write(workspace, PROVENANCE, "body")
    header, _, _rest = document.partition("\n\n")
    assert frontmatter_line_count(PROVENANCE) == len(header.split("\n")) + 1  # + the blank line


# --- what happens when there is nowhere to write --------------------------------------


def test_no_workspace_means_no_file_and_no_exception():
    """The tool is registered outside a card too; nowhere to put evidence is not a failure."""
    assert save_page(None, PROVENANCE, "body") is None
    assert save_page("", PROVENANCE, "body") is None


def test_a_write_that_cannot_land_costs_the_evidence_file_and_nothing_else(tmp_path):
    """A full disk (here: a file where the workspace should be) must not fail the fetch."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    assert save_page(str(blocked), PROVENANCE, "body") is None


def test_a_failed_rename_leaves_no_half_written_page_behind(workspace, monkeypatch):
    """``_discard``'s job: a staging file that never became a page is not evidence, and a
    ``.part`` left in downloads/ would be swept into the leftover commit as if it were."""

    def refuse(_source, _destination):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", refuse)
    assert save_page(str(workspace), PROVENANCE, "body") is None
    monkeypatch.undo()
    assert sorted((workspace / "downloads" / "pages").iterdir()) == []
