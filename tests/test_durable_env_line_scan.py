"""pi durable's ``test/env-line-scan.test.ts`` (v1.1.0)."""

from __future__ import annotations

import pytest

from misaka.durable.env.decode import StreamDecoder
from misaka.durable.env.line_scan import LineScanner
from misaka.utils.js import RangeError, Utf8Decoder


def _imul(a: int, b: int) -> int:
    """``Math.imul``: the low 32 bits of the product, as an unsigned 32-bit integer here."""
    return (a * b) & 0xFFFFFFFF


def random(seed: int):
    state = seed & 0xFFFFFFFF

    def next_() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & 0xFFFFFFFF
        t = state
        t = _imul(t ^ (t >> 15), t | 1)
        t ^= (t + _imul(t ^ (t >> 7), t | 61)) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296

    return next_


# Newlines, ASCII, a byte-order mark, valid multi-byte sequences, and bytes that form invalid or truncated sequences.
PIECES = [
    [0x0A],
    [0x0A],
    [0x61],
    [0x62, 0x63],
    [0xEF, 0xBB, 0xBF],
    [0xC3, 0xA9],
    [0xE2, 0x82, 0xAC],
    [0xF0, 0x9F, 0x98, 0x80],
    [0xE2, 0x82],
    [0xFF],
    [0x80],
    [0xF0, 0x9F],
    [0x0D, 0x0A],
]


def randomFile(next_) -> bytes:
    out: list[int] = []
    pieces = int(next_() * 60)
    for _index in range(pieces):
        out.extend(PIECES[int(next_() * len(PIECES))])
    return bytes(out)


def utf8(text: str) -> int:
    return len(text.encode("utf-8"))


def wholeDecode(data: bytes, ignoreBOM: bool = False) -> str:
    """``new TextDecoder("utf-8", {ignoreBOM}).decode(data)``."""
    return Utf8Decoder(ignoreBOM=ignoreBOM).decode(data, final=True)


class TestStreamDecoder:
    def test_keeps_a_u_feff_that_does_not_start_the_stream(self):
        # Node's streaming TextDecoder with BOM handling dropped this U+FEFF, which follows an invalid sequence.
        data = bytes([0xE2, 0x82, 0xEF, 0xBB, 0xBF, 0x61])
        decoder = StreamDecoder()
        text = "".join(decoder.decode(bytes([byte])) for byte in data) + decoder.decode()
        assert text == wholeDecode(data)
        assert text == "�﻿a"

    def test_decodes_any_chunking_like_decoding_the_whole_stream(self):
        for seed in range(1, 5001):
            next_ = random(seed)
            data = randomFile(next_)
            decoder = StreamDecoder()
            text = ""
            offset = 0
            while offset < len(data):
                size = 1 + int(next_() * 7)
                text += decoder.decode(data[offset : offset + size])
                offset += size
            assert text + decoder.decode() == wholeDecode(data), f"seed {seed}"


class TestLineScanner:
    def test_agrees_with_decoding_and_splitting_the_whole_file(self):
        for seed in range(1, 5001):
            next_ = random(seed)
            file = randomFile(next_)
            lines = wholeDecode(file).split("\n")
            startLine = int(next_() * (len(lines) + 2))
            endLine = None if next_() < 0.3 else startLine + 1 + int(next_() * (len(lines) + 1))
            scanner = LineScanner(startLine) if endLine is None else LineScanner(startLine, endLine)
            offset = 0
            while offset < len(file):
                size = 1 + int(next_() * 7)
                scanner.push(file[offset : offset + size])
                offset += size
            scan = scanner.finish()
            message = f"seed {seed}"
            assert scan["newlines"] == len(lines) - 1, message
            selected = lines[startLine:endLine]

            def decode(from_: int, to: int, file: bytes = file) -> str:
                return wholeDecode(file[from_:to], ignoreBOM=from_ > 0)

            assert decode(scan["start"], scan["end"]) == "\n".join(selected), message
            assert scan["selectedBytes"] == utf8("\n".join(selected)), message
            if startLine < len(lines):
                assert decode(scan["start"], scan["firstLineEnd"]) == lines[startLine], message
                assert scan["firstLineBytes"] == utf8(lines[startLine]), message
                lastLine = min(len(lines) if endLine is None else endLine, len(lines)) - 1
                lastEnd = file.find(0x0A, scan["lastLineStart"]) if lastLine + 1 < len(lines) else len(file)
                assert decode(scan["lastLineStart"], lastEnd) == lines[lastLine], message
            else:
                assert (scan["start"], scan["end"], scan["selectedBytes"]) == (len(file), len(file), 0), message

    def test_rejects_empty_or_invalid_ranges(self):
        with pytest.raises(RangeError):
            LineScanner(2, 2)
        with pytest.raises(RangeError):
            LineScanner(-1)
        with pytest.raises(RangeError):
            LineScanner(1.5)
