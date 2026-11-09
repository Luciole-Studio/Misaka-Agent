"""``edit`` and its preview refuse what is not a regular file, and files too large to rewrite.

A FIFO used to hang ``edit`` for good: the open blocked on a worker thread, and the abort path
waits for that worker while holding the file's mutation lock, so Esc could not bring it back.
"""
import asyncio
import os

import pytest
from PIL import Image

from misaka.core.tools import _common, read
from misaka.core.tools.edit import createEditToolDefinition
from misaka.core.tools.edit_diff import EditDiffError, computeEditsDiff

EDIT = {"edits": [{"oldText": "a", "newText": "b"}]}

pytestmark = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")


async def test_edit_on_a_fifo_fails_at_once(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(RuntimeError, match="not a regular file"):
        await asyncio.wait_for(createEditToolDefinition(str(tmp_path)).execute("call", {"path": "pipe", **EDIT}), 5)


async def test_the_edit_preview_on_a_fifo_fails_at_once(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    result = await asyncio.wait_for(computeEditsDiff("pipe", EDIT["edits"], str(tmp_path)), 5)
    assert isinstance(result, EditDiffError) and "not a regular file" in result.error


async def test_edit_refuses_a_file_past_the_whole_read_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(_common, "MAX_WHOLE_READ_BYTES", 1024)
    (tmp_path / "big.txt").write_text("a" * 2048, encoding="utf-8")
    with pytest.raises(RuntimeError, match="edit loads at most"):
        await createEditToolDefinition(str(tmp_path)).execute("call", {"path": "big.txt", **EDIT})
    assert (tmp_path / "big.txt").read_text(encoding="utf-8") == "a" * 2048


async def test_edit_still_edits_a_regular_file(tmp_path):
    (tmp_path / "notes.txt").write_text("a line\n", encoding="utf-8")
    await createEditToolDefinition(str(tmp_path)).execute("call", {"path": "notes.txt", **EDIT})
    assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "b line\n"


async def test_read_refuses_an_office_file_past_its_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(read, "MAX_OFFICE_READ_BYTES", 10)
    (tmp_path / "report.docx").write_bytes(b"PK" + b"\0" * 100)
    with pytest.raises(RuntimeError, match="renders Office files up to"):
        await read.createReadToolDefinition(str(tmp_path)).execute("call", {"path": "report.docx"})


async def test_read_refuses_an_image_past_the_whole_read_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(_common, "MAX_WHOLE_READ_BYTES", 16)
    Image.new("RGB", (8, 8)).save(tmp_path / "shot.png")
    with pytest.raises(RuntimeError, match="read loads at most"):
        await read.createReadToolDefinition(str(tmp_path)).execute("call", {"path": "shot.png"})


async def test_a_file_that_grows_past_the_limit_is_refused_not_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(_common, "MAX_WHOLE_READ_BYTES", 1024)
    path = tmp_path / "growing.txt"
    path.write_text("a" * 100, encoding="utf-8")
    real_regular_file = _common.regular_file

    def stat_then_grow(absolute_path, **kwargs):
        status = real_regular_file(absolute_path, **kwargs)
        path.write_text("a" * 4096, encoding="utf-8")                  # another writer, between the stat and the read
        return status

    monkeypatch.setattr(_common, "regular_file", stat_then_grow)
    with pytest.raises(RuntimeError, match="edit loads at most"):
        _common.whole_file_bytes(str(path), "edit")
