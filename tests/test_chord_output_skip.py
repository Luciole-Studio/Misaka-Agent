"""pi chord's ``test/output-skip.test.ts`` (durable's ``test/harness-output-skip.test.ts`` until 1.2.0)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from misaka.chord.output import OutputBuffer, OutputLimits, OutputSkip

MASK = 0xFFFFFFFF


def imul(a: int, b: int) -> int:
    """``Math.imul`` on 32-bit patterns."""
    return (a * b) & MASK


def random(seed: int) -> Callable[[], float]:
    """Deterministic PRNG (mulberry32) so failures reproduce from the printed seed."""
    state = seed & MASK

    def next_() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & MASK
        t = state
        t = imul(t ^ (t >> 15), t | 1)
        t ^= (t + imul(t ^ (t >> 7), t | 61)) & MASK
        return ((t ^ (t >> 14)) & MASK) / 4294967296

    return next_


# Decoded shell output: ASCII, multi-byte and astral characters, replacement characters, CR/LF, tabs, and control
# characters that sanitizing removes after bounding.
ALPHABET = ["a", "b", "z", " ", "\n", "\n", "\n", "\r\n", "\t", "é", "€", "😀", "�", "\x01", "\x1b"]


def randomChunk(next_: Callable[[], float]) -> str:
    length = int(next_() * 12)
    text = ""
    for _ in range(length):
        text += ALPHABET[int(next_() * len(ALPHABET))]
    return text


def measure(text: str) -> OutputSkip:
    return {"bytes": len(text.encode()), "newlines": text.count("\n"), "endsWithNewline": text.endswith("\n")}


def exceedsWindow(text: str, limits: OutputLimits) -> bool:
    """Whether ``text`` proves that everything before it is outside the tail window."""
    size = measure(text)
    return size["bytes"] > limits["maxBytes"] or size["newlines"] > limits["maxLines"]


def boundaries(text: str) -> list[int]:
    """Code-point boundaries of ``text``, so a cut never splits a character (a Python string indexes code points)."""
    return list(range(len(text) + 1))


def checkSeed(seed: int) -> None:
    """Feed random output to one buffer in full and to another through a reference skipper that follows the contract of
    ``ShellOutputInfo.skipped``: it holds undelivered text, and when it flushes, it may replace a prefix of it by counts
    if the rest exceeds the window. Snapshots must agree whenever both buffers have seen the same output."""
    next_ = random(seed)
    limits: OutputLimits = {"maxBytes": 1 + int(next_() * 40), "maxLines": 1 + int(next_() * 5), "retain": "tail"}
    full = OutputBuffer(limits)
    skipping = OutputBuffer(limits)
    pending = ""
    skips = 0

    def flush() -> None:
        nonlocal pending, skips
        if pending == "":
            return
        cuts = [cut for cut in boundaries(pending) if cut > 0 and exceedsWindow(pending[cut:], limits)]
        if len(cuts) > 0 and next_() < 0.7:
            cut = cuts[int(next_() * len(cuts))]
            skipping.push(pending[cut:], measure(pending[:cut]))
            skips += 1
        else:
            skipping.push(pending)
        pending = ""
        # Progress snapshots happen between deliveries and compact stored chunks.
        if next_() < 0.5:
            skipping.snapshot()
        assert skipping.snapshot() == full.snapshot(), f"seed {seed}"

    chunks = int(next_() * 40)
    for _ in range(chunks):
        chunk = randomChunk(next_)
        full.push(chunk)
        if next_() < 0.3:
            full.snapshot()
        pending += chunk
        if next_() < 0.3:
            flush()
    flush()
    full.end()
    skipping.end()
    assert skipping.snapshot() == full.snapshot(), f"seed {seed}"
    assert skips >= 0


# Progress commits snapshot at arbitrary moments; snapshots compact what is stored and must never change the window. A
# compaction to exactly the kept window lost the character that decides where a later window's first line starts.
def test_keeps_the_same_tail_whenever_progress_snapshots_happen():
    for seed in range(1, 3001):
        next_ = random(seed)
        limits: OutputLimits = {"maxBytes": 1 + int(next_() * 40), "maxLines": 1 + int(next_() * 5), "retain": "tail"}
        plain = OutputBuffer(limits)
        sampled = OutputBuffer(limits)
        chunks = int(next_() * 30)
        for _ in range(chunks):
            chunk = randomChunk(next_)
            plain.push(chunk)
            sampled.push(chunk)
            if next_() < 0.4:
                sampled.snapshot()
        assert sampled.snapshot() == plain.snapshot(), f"seed {seed}"


def test_matches_the_full_stream_for_every_legal_skip_pattern():
    for seed in range(1, 3001):
        checkSeed(seed)


def test_counts_skipped_bytes_lines_and_the_final_newline_exactly():
    limits: OutputLimits = {"maxBytes": 1000, "maxLines": 2, "retain": "tail"}
    full = OutputBuffer(limits)
    skipping = OutputBuffer(limits)
    # The skipped text ends without a newline, so its last line continues in the delivered text.
    omitted = "one\ntwo\nthr"
    kept = "ee\nfour\nfive\nsix"
    full.push(omitted + kept)
    skipping.push(kept, measure(omitted))
    assert skipping.snapshot() == full.snapshot()
    assert skipping.snapshot() == {"text": "five\nsix", "droppedBytes": 19, "droppedLines": 4}


def test_refuses_skips_for_head_retention():
    buffer = OutputBuffer({"maxBytes": 10, "maxLines": 2, "retain": "head"})
    with pytest.raises(Exception, match="tail retention"):
        buffer.push("x\ny\nz\n", {"bytes": 3, "newlines": 1, "endsWithNewline": True})
