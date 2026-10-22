"""pi chord's ``test/output.test.ts`` (durable's ``test/harness-output.test.ts`` until 1.2.0).

A JavaScript string is UTF-16, so the surrogate cases build their input as UTF-16 units and convert it with
``fromUtf16Units`` (a high and a low surrogate next to each other are one character, a lone one stays lone).
``Progress`` runs on a fake clock, as the upstream test runs it on fake timers.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from misaka.chord.output import (
    OutputBuffer,
    Progress,
    boundOutput,
    sanitizeOutput,
)
from misaka.utils.js import fromUtf16Units, toUtf16Units, toWellFormed


def head(maxLines: int, maxBytes: int = 1000) -> dict[str, Any]:
    return {"maxBytes": maxBytes, "maxLines": maxLines, "retain": "head"}


def tail(maxLines: int, maxBytes: int = 1000) -> dict[str, Any]:
    return {"maxBytes": maxBytes, "maxLines": maxLines, "retain": "tail"}


def encode(text: str) -> bytes:
    return toWellFormed(text).encode("utf-8")


def bufferTail(content: str, maxBytes: int) -> str:
    data = encode(content)
    if len(data) <= maxBytes:
        return content
    start = len(data) - maxBytes
    while start < len(data) and (data[start] & 0xC0) == 0x80:
        start += 1
    return data[start:].decode("utf-8", "replace")


def assertMatchesBufferTail(text: str, maxByteValues: list[int] | None = None) -> None:
    """A single line longer than the byte limit is cut like Buffer's tail slice on a character boundary."""
    totalBytes = len(encode(text))
    values = maxByteValues if maxByteValues is not None else list(range(totalBytes + 5))
    for maxBytes in values:
        kept = boundOutput(text, {"maxBytes": maxBytes, "maxLines": 10, "retain": "tail"})["text"]
        expected = bufferTail(text, maxBytes)
        if kept != expected:
            raise AssertionError(f"tail mismatch input={text!r} maxBytes={maxBytes} expected={expected!r} actual={kept!r}")
        if len(encode(kept)) > maxBytes:
            raise AssertionError(f"tail output exceeded {maxBytes} bytes")


def sampledByteLimits(text: str) -> list[int]:
    totalBytes = len(encode(text))
    candidates = [0, 1, 2, 3, 4, 5, 8, totalBytes // 2, totalBytes - 4, totalBytes - 1, totalBytes, totalBytes + 1]
    return sorted({value for value in candidates if value >= 0})


def bound(text: str, limits: dict[str, Any]) -> dict[str, Any]:
    result = boundOutput(text, limits)
    return {"kept": result["text"], "droppedBytes": result["droppedBytes"], "droppedLines": result["droppedLines"]}


def test_removes_control_characters_but_keeps_tabs_newlines_and_other_text():
    assert sanitizeOutput(f"a\0b\tc\nd\re\x07f{chr(0xFFF9)}g{chr(0xFFFB)}h\U0001f600") == "ab\tc\ndefgh\U0001f600"


def test_keeps_output_within_the_limits_unchanged():
    assert bound("a\nb\n", head(2)) == {"kept": "a\nb\n", "droppedBytes": 0, "droppedLines": 0}
    assert bound("a\nb", tail(2)) == {"kept": "a\nb", "droppedBytes": 0, "droppedLines": 0}
    assert bound("", tail(2)) == {"kept": "", "droppedBytes": 0, "droppedLines": 0}


def test_keeps_nothing_with_a_zero_limit():
    assert bound("ab\ncd\n", head(10, 0)) == {"kept": "", "droppedBytes": 6, "droppedLines": 2}
    assert bound("ab\ncd\n", tail(0)) == {"kept": "", "droppedBytes": 6, "droppedLines": 2}


def test_keeps_exact_slices_of_whole_lines_trailing_newline_included():
    assert bound("a\nb\nc\n", head(2)) == {"kept": "a\nb\n", "droppedBytes": 2, "droppedLines": 1}
    assert bound("a\nb\nc\n", tail(2)) == {"kept": "b\nc\n", "droppedBytes": 2, "droppedLines": 1}
    assert bound("a\nb\nc", tail(2)) == {"kept": "b\nc", "droppedBytes": 2, "droppedLines": 1}
    # Blank lines are lines.
    assert bound("a\nb\nc\n\n", tail(3)) == {"kept": "b\nc\n\n", "droppedBytes": 2, "droppedLines": 1}


def test_cuts_at_the_byte_limit_on_whole_lines_when_possible():
    assert bound("aa\nbb\ncc\n", head(10, 7)) == {"kept": "aa\nbb\n", "droppedBytes": 3, "droppedLines": 1}
    assert bound("aa\nbb\ncc\n", tail(10, 7)) == {"kept": "bb\ncc\n", "droppedBytes": 3, "droppedLines": 1}


def test_cuts_a_single_line_longer_than_the_byte_limit_on_a_character_boundary():
    # An accented e is two bytes; five bytes hold two whole characters.
    assert bound("ééé\n", head(10, 5)) == {"kept": "éé", "droppedBytes": 3, "droppedLines": 0}
    assert bound("x\néééé", tail(10, 5)) == {"kept": "éé", "droppedBytes": 6, "droppedLines": 1}


def test_keeps_a_u_feff_at_the_start_of_a_kept_slice():
    assert bound("x\ufeffa", tail(10, 4)) == {"kept": "\ufeffa", "droppedBytes": 1, "droppedLines": 0}


def test_cuts_tails_of_surrogate_edge_cases_exactly_like_buffer():
    inputs = [
        "a\ud83d",
        "\ude42b",
        "a\ude42b",
        "\ud83d🙂",
        "🙂\ude42",
        "\U0001f469‍\U0001f4bb",
    ]
    for units in inputs:
        assertMatchesBufferTail(fromUtf16Units(units))


def test_cuts_tails_exactly_like_buffer_across_deterministic_fuzz_cases():
    alphabet = [
        "a",
        "\x7f",
        "\x80",
        chr(0xE9),
        chr(0x07FF),
        chr(0x0800),
        chr(0x4E2D),
        chr(0xD7FF),
        "\ud800",
        "\ud83d",
        "\udc00",
        "\ude42",
        toUtf16Units("\U0001f642"),
        chr(0xE000),
        chr(0xFFFF),
    ]

    def checkExhaustive(prefix: str, depth: int) -> None:
        text = fromUtf16Units(prefix)
        assertMatchesBufferTail(text, sampledByteLimits(text))
        if depth == 0:
            return
        for character in alphabet:
            checkExhaustive(prefix + character, depth - 1)

    checkExhaustive("", 3)
    seed = 0x12345678

    def random() -> float:
        nonlocal seed
        seed = (seed * 1664525 + 1013904223) & 0xFFFFFFFF
        return seed / 0x100000000

    for _ in range(1000):
        units = ""
        length = int(random() * 80)
        for _ in range(length):
            units += alphabet[int(random() * len(alphabet))]
        text = fromUtf16Units(units)
        assertMatchesBufferTail(text, sampledByteLimits(text))


def test_buffer_keeps_exact_totals_across_chunks_and_decodes_utf_8_split_across_byte_chunks():
    buffer = OutputBuffer(tail(2))
    data = "\U0001f600\n".encode()
    buffer.push("a\nb\n")
    buffer.push(data[:2])
    buffer.push(data[2:])
    assert buffer.snapshot() == {"text": "b\n\U0001f600\n", "droppedBytes": 2, "droppedLines": 1}


def test_buffer_sanitizes_the_retained_text_but_counts_the_raw_stream():
    buffer = OutputBuffer(tail(1))
    buffer.push("a\x07\n")
    assert buffer.snapshot() == {"text": "a\n", "droppedBytes": 0, "droppedLines": 0}
    buffer.push("b\x1b\n")
    assert buffer.snapshot() == {"text": "b\n", "droppedBytes": 3, "droppedLines": 1}


def test_buffer_drops_a_byte_order_mark_only_at_the_start_of_the_output():
    bom = bytes([0xEF, 0xBB, 0xBF])
    buffer = OutputBuffer(tail(10))
    buffer.push(bom[:1])
    buffer.push(bytes([0xBB, 0xBF, 0x61]))
    buffer.push("b")
    # A U+FEFF after a string chunk is text.
    buffer.push(bytes([*bom, 0x63]))
    buffer.end()
    assert buffer.snapshot()["text"] == "ab\ufeffc"


def test_buffer_flushes_an_incomplete_character_before_a_string_chunk_and_at_the_end():
    buffer = OutputBuffer(tail(10))
    euro = "€".encode()
    buffer.push(euro[:1])
    buffer.push("x")
    buffer.push(euro[:2])
    buffer.end()
    assert buffer.snapshot()["text"] == "�x�"


def test_buffer_matches_bounding_the_whole_stream_when_several_chunks_arrive_between_snapshots():
    for limits in [head(3, 40), tail(3, 40), head(50, 25), tail(50, 25)]:
        buffer = OutputBuffer(limits)
        stream = ""
        for index in range(300):
            chunk = f"{'é' * (index % 30)}\n" if index % 7 == 0 else f"line {index}\n"
            stream += chunk
            buffer.push(chunk)
            if index % 5 != 4:
                continue
            expected = boundOutput(stream, limits)
            assert buffer.snapshot() == {
                "text": expected["text"],
                "droppedBytes": expected["droppedBytes"],
                "droppedLines": expected["droppedLines"],
            }


def test_buffer_stops_storing_head_output_once_the_window_is_full():
    buffer = OutputBuffer(head(2))
    for index in range(1000):
        buffer.push(f"line {index}\n")
    assert buffer.storedBytes < 20
    assert buffer.snapshot() == {"text": "line 0\nline 1\n", "droppedBytes": 8876, "droppedLines": 998}


def test_buffer_stores_only_the_tail_window_after_each_snapshot():
    buffer = OutputBuffer(tail(3, 100))
    stream = ""
    for index in range(2000):
        chunk = f"line {index}\n\n"
        stream += chunk
        buffer.push(chunk)
        snapshot = buffer.snapshot()
        assert buffer.storedBytes <= 100
        assert snapshot["text"] == boundOutput(stream, tail(3, 100))["text"]


class FakeClock:
    """The clock ``Progress`` reads: time moves only when the test advances it."""

    def __init__(self) -> None:
        self.time = 0.0
        self.timers: list[dict[str, Any]] = []
        self.sequence = 0

    def now(self) -> float:
        return self.time

    def setTimeout(self, callback: Any, delayMs: float) -> dict[str, Any]:
        self.sequence += 1
        timer = {"due": self.time + delayMs, "sequence": self.sequence, "callback": callback, "cancelled": False}
        self.timers.append(timer)
        return timer

    def clearTimeout(self, handle: dict[str, Any]) -> None:
        handle["cancelled"] = True

    async def advance(self, ms: float) -> None:
        target = self.time + ms
        await settle()
        while True:
            due = [timer for timer in self.timers if not timer["cancelled"] and timer["due"] <= target]
            if not due:
                break
            timer = min(due, key=lambda item: (item["due"], item["sequence"]))
            self.timers.remove(timer)
            self.time = max(self.time, timer["due"])
            timer["callback"]()
            await settle()
        self.time = target
        await settle()


async def settle() -> None:
    for _ in range(8):
        await asyncio.sleep(0)


async def test_progress_commits_the_first_change_at_once_then_waits_the_minimum_and_the_written_size():
    clock = FakeClock()
    commits: list[float] = []
    size = 50 * 1024

    async def write() -> int:
        commits.append(clock.now())
        return size

    progress = Progress(write, lambda error: None, 100, clock=clock)
    progress.mark()
    await clock.advance(0)
    assert commits == [0]
    # 50 KiB buys 500 ms; changes meanwhile coalesce into one commit.
    size = 10
    progress.mark()
    progress.mark()
    await clock.advance(499)
    assert commits == [0]
    await clock.advance(1)
    assert commits == [0, 500]
    # A small commit still waits the minimum 100 ms.
    progress.mark()
    await clock.advance(99)
    assert commits == [0, 500]
    await clock.advance(1)
    assert commits == [0, 500, 600]


async def test_progress_waits_the_configured_minimum_interval_between_small_commits():
    clock = FakeClock()
    commits: list[float] = []

    async def write() -> int:
        commits.append(clock.now())
        return 10

    progress = Progress(write, lambda error: None, 500, clock=clock)
    progress.mark()
    await clock.advance(0)
    progress.mark()
    await clock.advance(499)
    assert commits == [0]
    await clock.advance(1)
    assert commits == [0, 500]


async def test_progress_rejects_the_waiters_of_a_failed_commit_and_reports_its_error():
    errors: list[BaseException] = []
    failure = RuntimeError("commit failed")

    async def write() -> int:
        raise failure

    progress = Progress(write, errors.append, 100)
    with pytest.raises(RuntimeError) as raised:
        await progress.markAndWait()
    assert raised.value is failure
    assert errors == [failure]


async def test_progress_stops_waits_for_the_commit_in_flight_and_hands_back_waiters_no_commit_covered_yet():
    inFlight: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def write() -> int:
        await inFlight
        return 0

    progress = Progress(write, lambda error: None, 100, clock=FakeClock())
    first = progress.markAndWait()
    second = progress.markAndWait()
    stopped = asyncio.ensure_future(progress.stop())
    inFlight.set_result(None)
    pending = await stopped
    await first
    assert len(pending) == 1
    pending[0].resolve()
    await second


def test_utf8ByteLength_counts_like_nodes_buffer():
    """MISAKA fork: a Python string holds code points; a lone surrogate counts as U+FFFD (three bytes), a high surrogate
    followed by a low one as the pair UTF-16 would make (four), an astral character as four."""
    from misaka.chord.output import utf8ByteLength

    assert utf8ByteLength("") == 0
    assert utf8ByteLength("ascii") == 5
    assert utf8ByteLength("é€") == 5
    assert utf8ByteLength("\U0001f600") == 4
    assert utf8ByteLength("\ud83d") == 3
    assert utf8ByteLength("a\udc00b") == 5
    assert utf8ByteLength(fromUtf16Units("😀")) == 4
    assert utf8ByteLength("\ud83d" + "\ude00") == 4
