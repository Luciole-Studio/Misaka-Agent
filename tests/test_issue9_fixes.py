"""The confirmed defects of GitHub issue #9 (MISAKA 0.18.5, reported 2026-10-07).

A Sister created with a colon in her description dropped out of the roster without a word; a
range of pages with no text never got doc_read's "look at the page image" hint; every helper
script a card wrote was warned about as a corpus failure; and the reasoning tokens a provider
reports were never recorded."""
from __future__ import annotations

import logging
import os
from types import SimpleNamespace as NS

import pytest

from misaka.core.documents import index as corpus
from misaka.core.documents import workspace as ws_index
from misaka.core.documents.wiring import documents
from misaka.core.network import roster
from misaka.core.wiring import ToolCollector

SPECIALTY = "Economic history: late imperial China, the salt monopoly"


# -- a description with a colon in it -----------------------------------------------------------------

def test_a_colon_in_the_description_no_longer_breaks_the_profile(tmp_path):
    ok, message = roster.create_sister("10077", root=str(tmp_path), specialty=SPECIALTY)
    assert ok, message
    assert roster.describe("10077", root=str(tmp_path))[0] == SPECIALTY


def test_a_profile_written_unquoted_before_the_fix_still_reads(tmp_path):
    (tmp_path / "10032").mkdir()
    (tmp_path / "10032" / "DESCRIBE.md").write_text(f"---\ndescription: {SPECIALTY}\n---\n# body\n", encoding="utf-8")
    assert roster.describe("10032", root=str(tmp_path)) == (SPECIALTY, "# body"), "her profile comes with it"


def test_a_profile_that_does_not_read_back_is_not_reported_created(tmp_path, monkeypatch):
    monkeypatch.setattr(roster, "describe", lambda sid, root=None: ("DESCRIBE.md could not be read (x)", None))
    ok, message = roster.create_sister("10078", root=str(tmp_path), specialty=SPECIALTY)
    assert not ok and "did not read back" in message
    assert not (tmp_path / "10078").exists(), "no half-made Sister is left in the roster"


# -- pages with no text ---------------------------------------------------------------------------------

def _indexed(tmp_path, monkeypatch, pages):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "scan.pdf").write_bytes(b"%PDF-1.4\n% stand-in\n")
    monkeypatch.setattr(corpus, "extract_pages", lambda p, meta=None: list(pages))
    doc_id, _ = corpus.ingest(str(workspace / "scan.pdf"), workspace=str(workspace), with_tree=False)
    return doc_id, str(workspace)


def test_a_page_with_no_text_says_so(tmp_path, monkeypatch):
    prose = "A page of the book with enough words on it to count as text. " * 3
    doc_id, workspace = _indexed(tmp_path, monkeypatch, [prose, "   ", prose])
    window = corpus.read_pages(doc_id, 1, 3, workspace=workspace)
    assert "--- p2 --- (no text on this page)" in window
    assert corpus.pages_have_text(doc_id, 2, 2, workspace=workspace) is False
    assert corpus.pages_have_text(doc_id, 1, 3, workspace=workspace) is True


async def test_doc_read_points_a_range_with_no_text_at_the_page_image(tmp_path, monkeypatch):
    prose = "A page of the book with enough words on it to count as text. " * 3
    doc_id, workspace = _indexed(tmp_path, monkeypatch, [prose, "", "", prose])
    collector = ToolCollector()
    documents.register(collector)
    tool = next(t for t in collector.tools if t.name == "doc_read")
    result = await tool.execute("call", {"doc_id": doc_id, "pages": "2-3"}, None, None,
                                NS(cwd=workspace, model=None, modelRegistry=None, signal=None))
    assert "No text was extracted from p2-3" in result["content"][0]["text"]


# -- deliverables the corpus never reads ------------------------------------------------------------------

def test_a_helper_script_is_not_warned_about_but_a_refused_document_is(tmp_path, caplog):
    root = tmp_path / "card"
    root.mkdir()
    (root / "patch_summary.py").write_text("print('helper')\n", encoding="utf-8")
    (root / "empty.pdf").write_bytes(b"%PDF-1.4\n")
    task = {"id": "t_1", "workspace": str(root)}
    with caplog.at_level(logging.WARNING, logger=ws_index.logger.name):
        got = ws_index.ingest_artifacts(None, task, ["patch_summary.py", "empty.pdf"])
    assert got == []
    warned = " ".join(record.getMessage() for record in caplog.records)
    assert "patch_summary.py" not in warned
    assert "empty.pdf" in warned, "a document the corpus reads and refused is still named"


# -- reasoning tokens -------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, reasoning, cache_read", [
    ({"prompt_tokens": 100, "completion_tokens": 80, "completion_tokens_details": {"reasoning_tokens": 60},
      "prompt_cache_hit_tokens": 40}, 60, 40),                               # DeepSeek
    ({"prompt_tokens": 100, "completion_tokens": 80, "cached_tokens": 25}, 0, 25),  # Kimi
])
def test_reasoning_tokens_are_recorded_as_part_of_the_output(raw, reasoning, cache_read):
    from misaka.ai.api.openai_completions import _parseChunkUsage as parse_chunk_usage
    from misaka.ai.compat import getModel
    usage = parse_chunk_usage(raw, getModel("openai", "gpt-4o"))
    assert usage.output == 80, "completion_tokens already include the reasoning: not added again"
    assert usage.reasoning == reasoning and usage.cacheRead == cache_read


def test_the_roster_comment_names_a_tool_that_exists():
    import inspect
    assert "misaka_sisters" not in inspect.getsource(roster)
    assert os.path.exists(roster.__file__)


# -- PDF outlines in every install ------------------------------------------------------------------------

def test_every_install_has_what_pdf_outlines_need():
    """The outline packages were an extra, and a release archive cannot add one: no archive install
    ever had an outline. They are MISAKA's own dependencies now."""
    import tomllib
    from pathlib import Path

    from misaka.core.documents import pageindex
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    named = {requirement.split("=")[0].split(">")[0].strip().lower() for requirement in project["dependencies"]}
    assert {name.lower() for name in pageindex.REQUIREMENTS} - named <= {"regex"}, "regex comes with tiktoken"
    assert "tiktoken" in named
    assert project["optional-dependencies"]["pageindex"] == [], "the old extra still installs"
