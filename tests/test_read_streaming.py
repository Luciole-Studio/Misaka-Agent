"""``read`` streams text and refuses what is not a regular file.

It used to open a FIFO (blocking its worker thread forever: the abort could not reach it), read
``/dev/zero`` until memory ran out, and load any text file whole before paging it. The paged
output must stay byte-for-byte what the whole-file algorithm produced; that algorithm is kept
below as the oracle.
"""
import asyncio
import os
import random
import threading

import pytest

from misaka.core.tools import read as read_module
from misaka.core.tools.read import createReadToolDefinition
from misaka.core.tools.truncate import DEFAULT_MAX_BYTES, formatSize, truncateHead


def _oracle(text, offset, limit, path):
    """The whole-file algorithm ``read`` used before streaming, verbatim."""
    all_lines = text.split("\n")
    total_file_lines = len(all_lines)
    start_line = max(0, offset - 1) if offset else 0
    start_line_display = start_line + 1
    if start_line >= len(all_lines):
        return f"Offset {offset} is beyond end of file ({len(all_lines)} lines total)", None
    user_limited_lines = None
    if limit is not None:
        end_line = min(start_line + limit, len(all_lines))
        selected_content = "\n".join(all_lines[start_line:end_line])
        user_limited_lines = end_line - start_line
    else:
        selected_content = "\n".join(all_lines[start_line:])
    truncation = truncateHead(selected_content)
    if truncation.firstLineExceedsLimit:
        first_line_size = formatSize(len(all_lines[start_line].encode("utf-8")))
        return (f"[Line {start_line_display} is {first_line_size}, exceeds {formatSize(DEFAULT_MAX_BYTES)} limit. "
                f"Use bash: sed -n '{start_line_display}p' {path} | head -c {DEFAULT_MAX_BYTES}]"), truncation
    if truncation.truncated:
        end_line_display = start_line_display + truncation.outputLines - 1
        next_offset = end_line_display + 1
        output_text = truncation.content
        if truncation.truncatedBy == "lines":
            output_text += (f"\n\n[Showing lines {start_line_display}-{end_line_display} of {total_file_lines}. "
                            f"Use offset={next_offset} to continue.]")
        else:
            output_text += (f"\n\n[Showing lines {start_line_display}-{end_line_display} of {total_file_lines} "
                            f"({formatSize(DEFAULT_MAX_BYTES)} limit). Use offset={next_offset} to continue.]")
        return output_text, truncation
    if user_limited_lines is not None and start_line + user_limited_lines < len(all_lines):
        remaining = len(all_lines) - (start_line + user_limited_lines)
        next_offset = start_line + user_limited_lines + 1
        return f"{truncation.content}\n\n[{remaining} more lines in file. Use offset={next_offset} to continue.]", None
    return truncation.content, None


def _documents():
    rng = random.Random(7)
    words = ["alpha", "数据", "Ω", "", "x" * 300, "\tindent", "emoji 🎉"]
    yield "empty", b""
    yield "one line", b"hello"
    yield "trailing newline", b"a\nb\n"
    yield "only newlines", b"\n\n\n"
    yield "crlf and bom", "﻿one\r\ntwo\r\n三\r\n".encode()
    yield "invalid utf-8", b"ok\n\xff\xfe broken \xe6\x95\nend \xe6"
    yield "huge first line", b"y" * (DEFAULT_MAX_BYTES * 3) + b"\nsecond\n"
    yield "line at the byte limit", b"z" * DEFAULT_MAX_BYTES + b"\n" + b"w" * 10
    yield "exactly the byte limit plus newline", b"q" * (DEFAULT_MAX_BYTES - 1) + b"\n" + b"r\n"
    yield "many short lines", "\n".join(str(n) for n in range(5000)).encode()
    yield "mixed", "\n".join(rng.choice(words) for _ in range(4000)).encode()
    yield "multibyte across the scan chunk", ("é" * (read_module._SCAN_CHUNK_CHARS + 3) + "\nend").encode()


@pytest.mark.parametrize("chunk", [7, read_module._SCAN_CHUNK_CHARS])
# Named by the document alone: an id spelled out of the bytes outgrows Windows' 32767-character
# limit on the environment variable pytest keeps the current test in.
@pytest.mark.parametrize("name,data", list(_documents()), ids=[name for name, _ in _documents()])
async def test_paged_output_is_what_the_whole_file_algorithm_produced(tmp_path, monkeypatch, name, data, chunk):
    monkeypatch.setattr(read_module, "_SCAN_CHUNK_CHARS", chunk)     # 7: every boundary lands mid-chunk
    path = tmp_path / "doc.txt"
    path.write_bytes(data)
    tool = createReadToolDefinition(str(tmp_path))
    text = data.decode("utf-8", errors="replace")
    total = len(text.split("\n"))
    for offset, limit in [(None, None), (1, 1), (2, 3), (None, 10), (total, None), (total, 5),
                          (total + 1, None), (3, 0), (1, 2001), (1500, 1000)]:
        expected, expected_truncation = _oracle(text, offset, limit, "doc.txt")
        params = {"path": "doc.txt", **({"offset": offset} if offset is not None else {}),
                  **({"limit": limit} if limit is not None else {})}
        try:
            result = await tool.execute("call", params)
        except RuntimeError as error:
            assert str(error) == expected, (name, offset, limit)
            continue
        assert result.content[0].text == expected, (name, offset, limit)
        truncation = result.details.truncation if result.details else None
        assert truncation == expected_truncation, (name, offset, limit)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
async def test_a_fifo_is_refused_at_once_and_leaves_no_thread_behind(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    before = threading.active_count()
    with pytest.raises(RuntimeError, match="not a regular file"):
        await asyncio.wait_for(createReadToolDefinition(str(tmp_path)).execute("call", {"path": "pipe"}), 5)
    await asyncio.sleep(0.05)
    assert threading.active_count() <= before + 1       # the default executor may keep one idle worker


@pytest.mark.skipif(not os.path.exists("/dev/zero"), reason="needs /dev/zero")
async def test_a_device_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="not a regular file"):
        await asyncio.wait_for(createReadToolDefinition(str(tmp_path)).execute("call", {"path": "/dev/zero"}), 5)


async def test_a_directory_is_still_reported_as_one(tmp_path):
    (tmp_path / "folder").mkdir()
    with pytest.raises(IsADirectoryError):
        await createReadToolDefinition(str(tmp_path)).execute("call", {"path": "folder"})


async def test_a_large_file_pages_without_being_held_whole(tmp_path, monkeypatch):
    (tmp_path / "big.log").write_text("".join(f"line {n}\n" for n in range(200_000)), newline="\n", encoding="utf-8")
    requested = []
    real = read_module._scan_text

    class Watched:
        def __init__(self, stream):
            self.stream = stream

        def read(self, size=-1):
            requested.append(size)
            return self.stream.read(size)

    def watched(stream, *args):
        return real(Watched(stream), *args)

    monkeypatch.setattr(read_module, "_scan_text", watched)
    result = await createReadToolDefinition(str(tmp_path)).execute("call", {"path": "big.log", "offset": 150_000, "limit": 2})
    assert result.content[0].text.startswith("line 149999\nline 150000\n\n[50000 more lines")
    assert requested and all(0 < size <= read_module._SCAN_CHUNK_CHARS for size in requested)
