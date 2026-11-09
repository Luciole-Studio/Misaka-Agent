"""Bounded output streams and adaptive progress commits: ``OutputBuffer`` keeps a head or tail window of a stream at a
cost proportional to each chunk, and ``Progress`` paces commits of a changing value by their size.

Translated from pi's ``packages/chord/src/output.ts`` (v1.3.0; durable's ``harness/output.ts`` until 1.2.0). Text is
bounded as UTF-8 bytes. Slices decode exactly: a U+FEFF at a slice's start is text, not a byte-order mark. ``Progress``
reads the time and sets its timer through a clock object (``now()``, ``setTimeout(callback, ms)``,
``clearTimeout(handle)``), the system clock by default, so a test can drive it with a fake one, as the upstream test does
with fake timers. Parameters pi names ``bytes`` are ``data`` here: the annotations need the ``bytes`` type.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypedDict

from misaka.chord._promise import Deferred, awaited, background, isRejection
from misaka.utils.js import Utf8Decoder, startTask, toWellFormed


class OutputLimits(TypedDict):
    """Retention limits of one output stream."""

    maxBytes: int
    maxLines: int
    retain: Literal["head", "tail"]


class BoundedOutput(TypedDict):
    """Retained output and what the limits dropped."""

    text: str
    droppedBytes: int
    droppedLines: int


class OutputSlice(TypedDict):
    """An exact slice of the input within the limits, and what it left out."""

    text: str
    bytes: int
    droppedBytes: int
    droppedLines: int


class OutputSkip(TypedDict):
    """Output a producer omitted before a chunk, measured on the decoded text: every U+FFFD counts as three bytes, and no
    sanitizing is applied."""

    bytes: int  # UTF-8 byte length of the omitted text.
    newlines: int  # Newlines (U+000A) in the omitted text.
    endsWithNewline: bool  # Whether the omitted text ends with a newline.


NEWLINE = 0x0A
INVALID_OUTPUT = re.compile("[\x00-\x08\x0b-\x1f\ufff9-\ufffb]")


def sanitizeOutput(text: str) -> str:
    """Remove control characters that break display and transcripts; tabs and newlines stay."""
    return INVALID_OUTPUT.sub("", text)


def encode(text: str) -> bytes:
    # A text encoder writes U+FFFD for a lone surrogate.
    return toWellFormed(text).encode("utf-8")


def decode(data: bytes) -> str:
    """``new TextDecoder("utf-8", {ignoreBOM: true}).decode(data)``: slices decode exactly."""
    return Utf8Decoder(ignoreBOM=True).decode(data, True)


def boundOutput(text: str, limits: OutputLimits) -> OutputSlice:
    """Bound ``text`` to whole lines within the limits: the first lines for ``head``, the last lines for ``tail``. The
    result is an exact slice, trailing newline included. A single line longer than ``maxBytes`` is cut at the byte limit
    on a character boundary."""
    data = encode(text)
    start, end = headRange(data, limits) if limits["retain"] == "head" else tailRange(data, limits)
    kept = data[start:end]
    return {
        "text": text if len(kept) == len(data) else decode(kept),
        "bytes": len(kept),
        "droppedBytes": len(data) - len(kept),
        "droppedLines": lineCount(data) - lineCount(kept),
    }


def lastNewlineAtOrBefore(data: bytes, index: int) -> int:
    """``bytes.lastIndexOf(NEWLINE, index)`` for an ``index`` of at least ``-1``: ``-1`` when none."""
    if index < 0:
        return -1
    return data.rfind(b"\n", 0, index + 1)


def headRange(data: bytes, limits: OutputLimits) -> tuple[int, int]:
    if limits["maxLines"] == 0 or limits["maxBytes"] == 0:
        return (0, 0)
    end = len(data)
    lines = 0
    index = data.find(b"\n")
    while index != -1:
        lines += 1
        if lines == limits["maxLines"]:
            end = index + 1
            break
        index = data.find(b"\n", index + 1)
    if end > limits["maxBytes"]:
        newline = lastNewlineAtOrBefore(data, limits["maxBytes"] - 1)
        end = characterEnd(data, limits["maxBytes"]) if newline == -1 else newline + 1
    return (0, end)


def tailRange(data: bytes, limits: OutputLimits) -> tuple[int, int]:
    if limits["maxLines"] == 0 or limits["maxBytes"] == 0:
        return (len(data), len(data))
    # A trailing newline ends the last line rather than starting another.
    last = len(data) - 2 if len(data) > 0 and data[len(data) - 1] == NEWLINE else len(data) - 1
    start = 0
    lines = 1
    index = -1 if last < 0 else lastNewlineAtOrBefore(data, last)
    while index != -1:
        if lines == limits["maxLines"]:
            start = index + 1
            break
        lines += 1
        index = -1 if index == 0 else lastNewlineAtOrBefore(data, index - 1)
    if len(data) - start > limits["maxBytes"]:
        origin = len(data) - limits["maxBytes"]
        newline = data.find(b"\n", origin - 1)
        # The first line starting inside the byte window, or a cut of the last line when it alone is too long.
        start = newline + 1 if newline != -1 and newline + 1 < len(data) else characterStart(data, origin)
    return (start, len(data))


def characterEnd(data: bytes, index: int) -> int:
    """The last character boundary at or before ``index``."""
    end = index
    while end > 0 and ((data[end] if end < len(data) else 0) & 0xC0) == 0x80:
        end -= 1
    return end


def characterStart(data: bytes, index: int) -> int:
    """The first character boundary at or after ``index``."""
    start = index
    while start < len(data) and (data[start] & 0xC0) == 0x80:
        start += 1
    return start


def lineCount(data: bytes) -> int:
    if len(data) == 0:
        return 0
    return data.count(b"\n") + (0 if data[len(data) - 1] == NEWLINE else 1)


class OutputBuffer:
    """Bounded running output of one stream, such as a tool call's or a process's. Accepting a chunk costs time
    proportional to the chunk: head retention stops storing once the window is full, and tail retention drops stored text the window no longer needs when it
    snapshots. Counts of the whole stream are kept so the dropped totals stay exact."""

    def __init__(self, limits: OutputLimits) -> None:
        self._limits = limits
        # Decoder of byte chunks. A string chunk or a skip ends an incomplete character of earlier bytes (it becomes
        # U+FFFD); the next byte chunk then starts a new character. As in `StreamDecoder`, only a byte-order mark at the
        # very start of the output is dropped, never a U+FEFF later in it.
        self._decoder = Utf8Decoder(ignoreBOM=True)
        self._started = False
        # Stored chunks: for head the start of the stream, for tail a suffix that still contains the next window.
        self._chunks: list[dict[str, Any]] = []
        self._storedBytes = 0
        self._storedNewlines = 0
        self._full = False
        self._totalBytes = 0
        self._totalNewlines = 0
        self._endsWithNewline = True

    @property
    def storedBytes(self) -> int:
        """Bytes currently held; bounded by the limits plus one chunk."""
        return self._storedBytes

    def push(self, chunk: str | bytes | bytearray | memoryview, skipped: OutputSkip | None = None) -> bool:
        """Accept a chunk; returns whether anything was accepted. ``skipped`` is output omitted right before the chunk,
        which must be more than the tail window by at least one byte or line; only tail retention accepts it."""
        # Bytes of an incomplete character from an earlier byte chunk come first.
        pending = self._flush() if isinstance(chunk, str) or skipped is not None else ""
        text = chunk if isinstance(chunk, str) else self._decoder.decode(bytes(chunk), False)
        first = not self._started and pending == "" and skipped is None
        if pending != "" or text != "" or skipped is not None:
            self._started = True
        if first and not isinstance(chunk, str) and text.startswith("\ufeff"):
            text = text[1:]
        if skipped is None:
            return self._accept(pending + text)
        if self._limits["retain"] != "tail":
            raise RuntimeError("Skipped output requires tail retention")
        self._accept(pending)
        self._skip(skipped)
        self._accept(text)
        return True

    def _skip(self, skipped: OutputSkip) -> None:
        """Count omitted output; nothing stored before it can be in the window once the text after it arrives."""
        if skipped["bytes"] == 0:
            return
        self._totalBytes += skipped["bytes"]
        self._totalNewlines += skipped["newlines"]
        self._endsWithNewline = skipped["endsWithNewline"]
        self._chunks = []
        self._storedBytes = 0
        self._storedNewlines = 0

    def end(self) -> None:
        """Flush an incomplete trailing character as a replacement character; call when the stream ends."""
        self._accept(self._flush())

    def _flush(self) -> str:
        return self._decoder.decode(b"", True)

    def _accept(self, text: str) -> bool:
        if len(text) == 0:
            return False
        size = utf8ByteLength(text)
        newlines = countNewlines(text)
        self._totalBytes += size
        self._totalNewlines += newlines
        self._endsWithNewline = text.endswith("\n")
        if self._full:
            return True
        self._chunks.append({"text": text, "bytes": size, "newlines": newlines})
        self._storedBytes += size
        self._storedNewlines += newlines
        if self._limits["retain"] == "head":
            # Nothing past a full window is ever needed.
            self._full = (
                self._storedBytes > self._limits["maxBytes"] or self._storedNewlines >= self._limits["maxLines"]
            )
            return True
        # Drop leading chunks while the rest still holds more than a window: more than ``maxBytes`` bytes or
        # ``maxLines`` newlines, plus one, so the window's line start can still be found. Each chunk is dropped once.
        while len(self._chunks) > 1:
            first = self._chunks[0]
            bytesAfter = self._storedBytes - first["bytes"]
            newlinesAfter = self._storedNewlines - first["newlines"]
            if bytesAfter <= self._limits["maxBytes"] + 1 and newlinesAfter <= self._limits["maxLines"] + 1:
                break
            self._chunks.pop(0)
            self._storedBytes = bytesAfter
            self._storedNewlines = newlinesAfter
        return True

    def snapshot(self) -> BoundedOutput:
        """Retained, sanitized output and what the limits dropped from the whole stream."""
        stored = self._chunks[0]["text"] if len(self._chunks) == 1 else "".join(chunk["text"] for chunk in self._chunks)
        kept = boundOutput(stored, self._limits)
        storedLines = lines(self._storedNewlines, stored == "" or stored.endswith("\n"))
        keptLines = storedLines - kept["droppedLines"]
        # Tail windows never reach back before this one, but finding a later window's first line needs what precedes
        # it: keep the shortest suffix longer than the window by a byte or a line, as `_accept` does.
        if self._limits["retain"] == "tail" or len(self._chunks) > 1:
            text = tailMargin(stored, self._limits) if self._limits["retain"] == "tail" else stored
            size = utf8ByteLength(text) if self._limits["retain"] == "tail" else self._storedBytes
            self._chunks = [] if text == "" else [{"text": text, "bytes": size, "newlines": countNewlines(text)}]
            self._storedBytes = size
            self._storedNewlines = self._chunks[0]["newlines"] if self._chunks else 0
        return {
            "text": sanitizeOutput(kept["text"]),
            "droppedBytes": self._totalBytes - kept["bytes"],
            "droppedLines": lines(self._totalNewlines, self._endsWithNewline) - keptLines,
        }


def tailMargin(text: str, limits: OutputLimits) -> str:
    """The shortest suffix of ``text`` with more than ``maxBytes`` bytes or more than ``maxLines`` newlines, or all of
    it. The tail window of any text that ends with this suffix, followed by anything, is the same as of ``text``
    followed by it."""
    data = encode(text)
    byteStart = characterEnd(data, len(data) - limits["maxBytes"] - 1) if len(data) > limits["maxBytes"] else 0
    lineStart = 0
    newlines = 0
    index = data.rfind(NEWLINE)
    while index != -1:
        newlines += 1
        if newlines > limits["maxLines"]:
            lineStart = index
            break
        if index == 0:
            break
        index = data.rfind(NEWLINE, 0, index)
    start = max(byteStart, lineStart)
    return text if start == 0 else decode(data[start:])


def lines(newlines: int, terminated: bool) -> int:
    """Lines of text with ``newlines`` newlines; a final unterminated line counts."""
    return newlines + (0 if terminated else 1)


def utf8ByteLength(content: str) -> int:
    """UTF-8 byte length of ``content``; a lone surrogate counts as U+FFFD, three bytes.

    MISAKA fork: upstream asks ``Buffer.byteLength`` when the runtime has it and otherwise walks UTF-16 code units. A
    Python string holds code points, where an astral character is one (four bytes, as its surrogate pair is upstream);
    the encoder answers for well-formed text, and the walk upstream does without ``Buffer`` handles a string that holds
    surrogates, pairing a high surrogate with the low one after it as UTF-16 would."""
    if content.isascii():
        return len(content)
    try:
        return len(content.encode("utf-8"))
    except UnicodeEncodeError:
        pass
    data = 0
    index = 0
    length = len(content)
    while index < length:
        code = ord(content[index])
        if code <= 0x7F:
            data += 1
        elif code <= 0x7FF:
            data += 2
        elif 0xD800 <= code <= 0xDBFF and index + 1 < length:
            following = ord(content[index + 1])
            if 0xDC00 <= following <= 0xDFFF:
                data += 4
                index += 1
            else:
                data += 3
        elif code > 0xFFFF:
            data += 4
        else:
            data += 3
        index += 1
    return data


def countNewlines(text: str) -> int:
    return text.count("\n")


PROGRESS_BYTES_PER_SECOND = 100 * 1024
"""Each progress commit also buys a pause proportional to what it wrote."""


class SystemClock:
    def now(self) -> float:
        return time.time() * 1000

    def setTimeout(self, callback: Callable[[], None], delayMs: float) -> Any:
        return asyncio.get_running_loop().call_later(delayMs / 1000, callback)

    def clearTimeout(self, handle: Any) -> None:
        handle.cancel()


class Progress:
    """Adaptive progress commits: the first change after an idle period commits at once; each commit then delays the
    next by at least ``minIntervalMs`` and by its written size at 100 KiB/s. At most one commit is in flight; changes
    made meanwhile coalesce into the next one.

    A waiter is a ``Deferred`` (``Promise.withResolvers()``): ``markAndWait`` returns its ``promise``, ``stop`` hands
    back the waiters themselves, which the final commit settles with ``resolve()`` / ``reject(error)``."""

    def __init__(
        self,
        write: Callable[[], Awaitable[int]],
        onError: Callable[[BaseException], None],
        minIntervalMs: float,
        *,
        clock: Any = None,  # MISAKA fork: a test clock in place of vitest's fake timers.
    ) -> None:
        self._write = write
        self._onError = onError
        self._minIntervalMs = minIntervalMs
        self._clock = clock if clock is not None else SystemClock()
        self._waiters: list[Deferred[None]] = []
        self._timer: Any = None
        self._inFlight: asyncio.Future[None] | None = None
        self._nextAt = 0.0
        self._dirty = False
        self._stopped = False

    def mark(self) -> None:
        """Schedule a commit."""
        self._dirty = True
        self._schedule()

    def markAndWait(self) -> asyncio.Future[None]:
        """Schedule a commit; the future settles with the commit that includes this change."""
        waiter: Deferred[None] = Deferred(quiet=False)
        self._waiters.append(waiter)
        self.mark()
        return waiter.promise

    async def stop(self) -> list[Deferred[None]]:
        """Stop committing and wait for the commit in flight; returns the waiters the final commit must settle."""
        self._stopped = True
        if self._timer is not None:
            self._clock.clearTimeout(self._timer)
        self._timer = None
        if self._inFlight is not None:
            # `await this.#inFlight`: a caller that gives up does not cancel the commit in flight.
            await awaited(self._inFlight)
        waiters, self._waiters = self._waiters, []
        return waiters

    def _schedule(self) -> None:
        if self._stopped or self._timer is not None or self._inFlight is not None:
            return
        wait = self._nextAt - self._clock.now()
        if wait <= 0:
            self._flush()
        else:

            def fire() -> None:
                self._timer = None
                self._flush()

            self._timer = self._clock.setTimeout(fire, wait)

    def _flush(self) -> None:
        if self._stopped or not self._dirty:
            return
        self._dirty = False
        waiters, self._waiters = self._waiters, []
        started = self._clock.now()
        # Called here, as upstream calls it: the body runs up to its first await now, and a synchronous throw leaves
        # `mark()`.
        written = startTask(self._write())
        # `write().then(…, …).finally(…)`: the commit in flight; it rejects when `onError` throws.
        inFlight: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._inFlight = inFlight

        async def run() -> None:
            failure: BaseException | None = None
            closing = False
            try:
                try:
                    # `.then` handlers run after the code that flushed, also for a write that already settled.
                    await asyncio.sleep(0)
                    count = await written
                except GeneratorExit:
                    raise
                except BaseException as error:  # the failure goes to the waiters and the error callback
                    if not isRejection(error):
                        raise
                    self._nextAt = started + self._minIntervalMs
                    for waiter in waiters:
                        waiter.reject(error)
                    self._onError(error)
                else:
                    self._nextAt = started + max(self._minIntervalMs, (count * 1000) / PROGRESS_BYTES_PER_SECOND)
                    for waiter in waiters:
                        waiter.resolve()
            except GeneratorExit:
                closing = True
                raise
            except BaseException as error:  # `onError` threw: the commit in flight rejects (`stop()` throws it)
                if not isRejection(error):
                    raise
                failure = error
            finally:
                self._inFlight = None
                if inFlight.done() or closing:
                    pass
                elif failure is not None:
                    inFlight.set_exception(failure)
                else:
                    inFlight.set_result(None)
                if self._dirty and not closing:
                    self._schedule()

        background(run())


__all__ = [
    "PROGRESS_BYTES_PER_SECOND",
    "BoundedOutput",
    "OutputBuffer",
    "OutputLimits",
    "OutputSkip",
    "OutputSlice",
    "Progress",
    "boundOutput",
    "characterEnd",
    "sanitizeOutput",
    "utf8ByteLength",
]
