"""doc_read takes the path of an indexed workspace file, and with no node or pages reads from page 1."""
import os
from types import SimpleNamespace as NS

import pytest

from misaka.core.documents import index as corpus
from misaka.core.documents.wiring import documents
from misaka.core.wiring import ToolCollector

BODY = "The plain text of this document, long enough to count as a page of its own.\n"


@pytest.fixture
def read(tmp_path):
    collector = ToolCollector()
    documents.register(collector)
    tool = next(t for t in collector.tools if t.name == "doc_read")

    async def call(**params):
        result = await tool.execute("call", params, None, None, NS(cwd=str(tmp_path), model=None))
        return result["content"][0]["text"]

    return call


def _indexed(root, name="notes.md", body=BODY):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return corpus.ingest(str(path), workspace=str(root))[0], path


async def test_a_relative_path_reads_the_document(tmp_path, read):
    doc_id, _path = _indexed(tmp_path, "nodes/b_1/cards/t_1/report.md")
    text = await read(doc_id="nodes/b_1/cards/t_1/report.md")
    assert "The plain text of this document" in text and f"{doc_id} p1-" in text


async def test_an_absolute_path_reads_the_document(tmp_path, read):
    _doc_id, path = _indexed(tmp_path)
    assert "The plain text of this document" in await read(doc_id=str(path), pages="1")


async def test_a_file_not_indexed_says_what_to_do(tmp_path, read):
    (tmp_path / "plain.md").write_text(BODY, encoding="utf-8")
    text = await read(doc_id="plain.md")
    assert "plain.md is not indexed" in text and "read" in text and "doc_add" in text


async def test_a_file_changed_after_indexing_counts_as_not_indexed(tmp_path, read):
    _doc_id, path = _indexed(tmp_path)
    path.write_text(BODY + "A later line.\n", encoding="utf-8")
    assert "is not indexed" in await read(doc_id="notes.md")


async def test_a_path_outside_the_workspace_is_only_not_found(tmp_path, read):
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "secret.md"
    outside.write_text(BODY, encoding="utf-8")
    os.symlink(outside, workspace / "link.md")
    collector = ToolCollector()
    documents.register(collector)
    tool = next(t for t in collector.tools if t.name == "doc_read")
    for ref in (str(outside), "../secret.md", "link.md"):
        result = await tool.execute("call", {"doc_id": ref}, None, None, NS(cwd=str(workspace), model=None))
        assert result["content"][0]["text"].startswith("Document not found.")


async def test_a_missing_path_or_bad_id_is_not_found(tmp_path, read):
    assert (await read(doc_id="no/such/file.md")).startswith("Document not found.")
    assert (await read(doc_id="x")).startswith("Document not found.")


async def test_a_real_id_still_wins(tmp_path, read):
    doc_id, _path = _indexed(tmp_path)
    assert "The plain text of this document" in await read(doc_id=doc_id, pages="1")


async def test_no_node_or_pages_reads_from_page_one(tmp_path, read):
    doc_id, _path = _indexed(tmp_path)
    text = await read(doc_id=doc_id)
    assert "--- p1 ---" in text and "The plain text of this document" in text


async def test_empty_node_and_pages_read_the_same(tmp_path, read):
    doc_id, _path = _indexed(tmp_path)
    assert await read(doc_id=doc_id, node="", pages="", offset=0) == await read(doc_id=doc_id)


async def test_a_long_document_comes_in_windows_that_continue(tmp_path, read):
    doc_id, _path = _indexed(tmp_path, "long.md", "\n\n".join(f"Paragraph {n} " + "word " * 80 for n in range(200)))
    first = await read(doc_id=doc_id)
    assert "call again with offset=12000" in first and "Paragraph 0 " in first and "Paragraph 199 " not in first
    second = await read(doc_id=doc_id, offset=12000)
    assert "Paragraph 0 " not in second and "offset=24000" in second


async def test_a_page_range_and_a_node_error_are_unchanged(tmp_path, read):
    doc_id, _path = _indexed(tmp_path)
    assert "pages must be a single page" in await read(doc_id=doc_id, pages="75,155")
    assert "was not found" in await read(doc_id=doc_id, node="9999")
