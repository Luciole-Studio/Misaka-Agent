"""Translated from pi-tui's ``packages/tui/src/stdin-buffer.ts`` (v1.3.0).

StdinBuffer buffers input and emits complete sequences.

This is necessary because stdin data events can arrive in partial chunks,
especially for escape sequences like mouse events. Without buffering,
partial sequences can be misinterpreted as regular keypresses.

For example, the mouse SGR sequence `\\x1b[<35;20;5m` might arrive as:
- Event 1: `\\x1b`
- Event 2: `[<35`
- Event 3: `;20;5m`

The buffer accumulates these until a complete sequence is detected.
Call the `process()` method to feed input data.

Based on code from OpenTUI (https://github.com/anomalyco/opentui)
MIT License - Copyright (c) 2025 opentui

MISAKA forks, each marked where it happens:

- Node's ``EventEmitter`` is the ``on``/``emit`` pair of the class itself.
- Raw bytes are decoded here (Node decodes them in the stdin stream).
- ``setTimeout`` is a timer on the asyncio event loop the input arrives on.
- Strings are Python strings: a character outside the Basic Multilingual Plane is one sequence, where JavaScript
  splits it into two surrogate halves.
"""

from __future__ import annotations

import asyncio
import codecs
from collections.abc import Callable
from typing import Any, Literal, TypedDict

from misaka.utils.js_regexp import RegExp

ESC = "\x1b"
DEFAULT_SEQUENCE_TIMEOUT_MS = 50
DEFAULT_ESCAPE_TIMEOUT_MS = 10
BRACKETED_PASTE_START = "\x1b[200~"
BRACKETED_PASTE_END = "\x1b[201~"
# Color and program-status OSC replies, XTVersion, and Kitty graphics replies.
# Until their header is complete, the input may be batched legacy Alt keystrokes.
TERMINAL_REPLY_HEADER_PATTERN = RegExp(r"^\x1b(?:\](?:1[01];|4;\d{1,3};|7501;\?)|P>\||_G(?:[a-z]=\d+,)*[a-z]=\d+;)")


def isControlString(data: str) -> bool:
    return data.startswith(f"{ESC}]") or data.startswith(f"{ESC}P") or data.startswith(f"{ESC}_")  # noqa: PIE810


def isCompleteSequence(data: str) -> Literal["complete", "incomplete", "not-escape"]:
    """Check if a string is a complete escape sequence or needs more data"""
    if not data.startswith(ESC):
        return "not-escape"

    if len(data) == 1:
        return "incomplete"

    afterEsc = data[1:]

    # CSI sequences: ESC [
    if afterEsc.startswith("["):
        # Check for old-style mouse sequence: ESC[M + 3 bytes
        if afterEsc.startswith("[M"):
            # Old-style mouse needs ESC[M + 3 bytes = 6 total
            return "complete" if len(data) >= 6 else "incomplete"
        return isCompleteCsiSequence(data)

    # OSC sequences: ESC ]
    if afterEsc.startswith("]"):
        return isCompleteOscSequence(data)

    # DCS sequences: ESC P ... ESC \ (includes XTVersion responses)
    if afterEsc.startswith("P"):
        return isCompleteDcsSequence(data)

    # APC sequences: ESC _ ... ESC \ (includes Kitty graphics responses)
    if afterEsc.startswith("_"):
        return isCompleteApcSequence(data)

    # SS3 sequences: ESC O
    if afterEsc.startswith("O"):
        # ESC O followed by a single character
        return "complete" if len(afterEsc) >= 2 else "incomplete"

    # Meta key sequences: ESC followed by a single character
    if len(afterEsc) == 1:
        return "complete"

    # Unknown escape sequence - treat as complete
    return "complete"


def isCompleteCsiSequence(data: str) -> Literal["complete", "incomplete"]:
    """Check if CSI sequence is complete
    CSI sequences: ESC [ ... followed by a final byte (0x40-0x7E)
    """
    if not data.startswith(f"{ESC}["):
        return "complete"

    # Need at least ESC [ and one more character
    if len(data) < 3:
        return "incomplete"

    payload = data[2:]

    # CSI sequences end with a byte in the range 0x40-0x7E (@-~)
    # This includes all letters and several special characters
    lastChar = payload[len(payload) - 1]
    lastCharCode = ord(lastChar)

    if lastCharCode >= 0x40 and lastCharCode <= 0x7E:
        # Special handling for SGR mouse sequences
        # Format: ESC[<B;X;Ym or ESC[<B;X;YM
        if payload.startswith("<"):
            # Must have format: <digits;digits;digits[Mm]
            mouseMatch = RegExp(r"^<\d+;\d+;\d+[Mm]$").test(payload)
            if mouseMatch:
                return "complete"
            # If it ends with M or m but doesn't match the pattern, still incomplete
            if lastChar == "M" or lastChar == "m":
                # Check if we have the right structure
                parts = payload[1:-1].split(";")
                if len(parts) == 3 and all(RegExp(r"^\d+$").test(p) for p in parts):
                    return "complete"

            return "incomplete"

        return "complete"

    return "incomplete"


def isCompleteOscSequence(data: str) -> Literal["complete", "incomplete"]:
    """Check if OSC sequence is complete
    OSC sequences: ESC ] ... ST (where ST is ESC \\ or BEL)
    """
    if not data.startswith(f"{ESC}]"):
        return "complete"

    # OSC sequences end with ST (ESC \) or BEL (\x07)
    if data.endswith(f"{ESC}\\") or data.endswith("\x07"):  # noqa: PIE810 - upstream checks the two endings apart
        return "complete"

    return "incomplete"


def isCompleteDcsSequence(data: str) -> Literal["complete", "incomplete"]:
    """Check if DCS (Device Control String) sequence is complete
    DCS sequences: ESC P ... ST (where ST is ESC \\)
    Used for XTVersion responses like ESC P >| ... ESC \\
    """
    if not data.startswith(f"{ESC}P"):
        return "complete"

    # DCS sequences end with ST (ESC \)
    if data.endswith(f"{ESC}\\"):
        return "complete"

    return "incomplete"


def isCompleteApcSequence(data: str) -> Literal["complete", "incomplete"]:
    """Check if APC (Application Program Command) sequence is complete
    APC sequences: ESC _ ... ST (where ST is ESC \\)
    Used for Kitty graphics responses like ESC _ G ... ESC \\
    """
    if not data.startswith(f"{ESC}_"):
        return "complete"

    # APC sequences end with ST (ESC \)
    if data.endswith(f"{ESC}\\"):
        return "complete"

    return "incomplete"


# Split accumulated buffer into complete sequences
def parseUnmodifiedKittyPrintableCodepoint(sequence: str) -> int | None:
    match = RegExp(r"^\x1b\[(\d+)(?::\d*)?(?::\d+)?u$").exec(sequence)
    if not match:
        return None

    codepoint = int(match[1], 10)
    return codepoint if codepoint >= 32 else None


class ExtractedSequences(TypedDict):
    sequences: list[str]
    remainder: str


def extractCompleteSequences(buffer: str) -> ExtractedSequences:
    sequences: list[str] = []
    pos = 0

    while pos < len(buffer):
        remaining = buffer[pos:]

        # Try to extract a sequence starting at this position
        if remaining.startswith(ESC):
            terminalReply = isControlString(remaining) and TERMINAL_REPLY_HEADER_PATTERN.test(remaining)
            # Find the end of this escape sequence
            seqEnd = 1
            while seqEnd <= len(remaining):
                if terminalReply and seqEnd > 2:
                    char = remaining[seqEnd - 1]
                    if char == "\x18" or char == "\x1a":
                        # CAN and SUB cancel the string, not subsequent keyboard input.
                        pos += seqEnd
                        break
                    if char == ESC and seqEnd < len(remaining) and remaining[seqEnd] != "\\":
                        # A new escape sequence aborts an unterminated reply. Keep a
                        # trailing ESC buffered until we can distinguish it from ST.
                        pos += seqEnd - 1
                        break
                candidate = remaining[:seqEnd]
                status = isCompleteSequence(candidate)

                if status == "complete":
                    # WezTerm with enable_kitty_keyboard sends the Escape key press as a
                    # raw '\x1b' byte (simple text path in encode_kitty, ignoring
                    # DISAMBIGUATE_ESCAPE_CODES) and the release as a full Kitty CSI-u
                    # sequence. These arrive concatenated as '\x1b\x1b[27;...u'.
                    # The buffer would normally treat '\x1b\x1b' as a complete meta-key
                    # sequence (ESC + single char), leaving '[27;...u' to be typed as
                    # plain text. If the character immediately following '\x1b\x1b'
                    # would begin a new escape sequence, emit only the first ESC and
                    # restart from the second.
                    if candidate == "\x1b\x1b":
                        nextChar = remaining[seqEnd] if seqEnd < len(remaining) else None
                        if (
                            nextChar == "["  # CSI
                            or nextChar == "]"  # OSC
                            or nextChar == "O"  # SS3
                            or nextChar == "P"  # DCS
                            or nextChar == "_"  # APC
                        ):
                            sequences.append(ESC)
                            pos += 1
                            break
                    sequences.append(candidate)
                    pos += seqEnd
                    break
                elif status == "incomplete":
                    seqEnd += 1
                else:
                    # Should not happen when starting with ESC
                    sequences.append(candidate)
                    pos += seqEnd
                    break

            if seqEnd > len(remaining):
                return {"sequences": sequences, "remainder": remaining}
        else:
            # Not an escape sequence - take a single character
            sequences.append(remaining[0])
            pos += 1

    return {"sequences": sequences, "remainder": ""}


class StdinBufferOptions(TypedDict, total=False):
    # Maximum time to wait for an incomplete keyboard or mouse sequence
    # (default: 50ms). Strings with a recognized terminal reply header wait for
    # their terminator instead, so delayed replies cannot become keypresses.
    # Ambiguous legacy Alt-key input still uses this timeout.
    timeout: float
    # Maximum time to wait after a lone ESC before treating it as Escape
    # (default: 10ms). Increase for high-latency Alt+key input (SSH).
    escapeTimeout: float


StdinBufferEventMap = TypedDict(
    "StdinBufferEventMap",
    {
        "data": tuple[str],
        "paste": tuple[str],
    },
)


class StdinBuffer:
    """Buffers stdin input and emits complete sequences via the 'data' event.
    Handles partial escape sequences that arrive across multiple chunks.
    """

    def __init__(self, options: StdinBufferOptions | None = None, loop: asyncio.AbstractEventLoop | None = None) -> None:
        if options is None:
            options = {}
        self.buffer: str = ""
        self.timeout: asyncio.TimerHandle | None = None
        self.pasteMode: bool = False
        self.pasteBuffer: str = ""
        self.pendingKittyPrintableCodepoint: int | None = None
        # MISAKA fork: EventEmitter's listener table.
        self.listeners: dict[str, list[Callable[[str], Any]]] = {}
        # MISAKA fork: the loop `setTimeout` schedules on (`None`: the running loop when a timer is armed).
        self.loop = loop
        # MISAKA fork: upstream's terminal sets the stdin encoding to UTF-8, so Node's stream decodes each chunk
        # and holds a partial character back for the next one. MISAKA's POSIX reader hands over raw `os.read`
        # bytes; this decoder does what that stream decoder does.
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        timeout = options.get("timeout")
        self.timeoutMs: float = timeout if timeout is not None else DEFAULT_SEQUENCE_TIMEOUT_MS
        escapeTimeout = options.get("escapeTimeout")
        self.escapeTimeoutMs: float = escapeTimeout if escapeTimeout is not None else DEFAULT_ESCAPE_TIMEOUT_MS

    def on(self, event: Literal["data", "paste"], listener: Callable[[str], Any]) -> StdinBuffer:
        """MISAKA fork: ``EventEmitter.on``."""
        self.listeners.setdefault(event, []).append(listener)
        return self

    def emit(self, event: Literal["data", "paste"], *args: str) -> bool:
        """MISAKA fork: ``EventEmitter.emit``."""
        listeners = self.listeners.get(event)
        if not listeners:
            return False
        for listener in list(listeners):
            listener(*args)
        return True

    def process(self, data: str | bytes | bytearray) -> None:
        # MISAKA fork: bytes go through the stream decoder first (see `decoder`). A chunk that decodes to nothing
        # (a character split across reads) is no data event in Node, so it neither emits nor touches the timer.
        # Upstream's Buffer branch (a single byte above 127 becomes ESC + byte - 128, for 8-bit meta keys) is not
        # ported: production input never takes it there, and here a lone byte of a split UTF-8 character would
        # turn into an Alt+key.
        if isinstance(data, (bytes, bytearray)):
            data = self.decoder.decode(bytes(data))
            if len(data) == 0:
                return

        # Clear any pending timeout
        if self.timeout:
            self.timeout.cancel()
            self.timeout = None

        str_: str = data

        if len(str_) == 0 and len(self.buffer) == 0:
            self.emitDataSequence("")
            return

        self.buffer += str_

        if self.pasteMode:
            self.pasteBuffer += self.buffer
            self.buffer = ""

            endIndex = self.pasteBuffer.find(BRACKETED_PASTE_END)
            if endIndex != -1:
                pastedContent = self.pasteBuffer[:endIndex]
                remaining = self.pasteBuffer[endIndex + len(BRACKETED_PASTE_END) :]

                self.pasteMode = False
                self.pasteBuffer = ""
                self.pendingKittyPrintableCodepoint = None

                self.emit("paste", pastedContent)

                if len(remaining) > 0:
                    self.process(remaining)
            return

        startIndex = self.buffer.find(BRACKETED_PASTE_START)
        if startIndex != -1:
            if startIndex > 0:
                beforePaste = self.buffer[:startIndex]
                result = extractCompleteSequences(beforePaste)
                for sequence in result["sequences"]:
                    self.emitDataSequence(sequence)

            self.pendingKittyPrintableCodepoint = None
            self.buffer = self.buffer[startIndex + len(BRACKETED_PASTE_START) :]
            self.pasteMode = True
            self.pasteBuffer = self.buffer
            self.buffer = ""

            endIndex = self.pasteBuffer.find(BRACKETED_PASTE_END)
            if endIndex != -1:
                pastedContent = self.pasteBuffer[:endIndex]
                remaining = self.pasteBuffer[endIndex + len(BRACKETED_PASTE_END) :]

                self.pasteMode = False
                self.pasteBuffer = ""
                self.pendingKittyPrintableCodepoint = None

                self.emit("paste", pastedContent)

                if len(remaining) > 0:
                    self.process(remaining)
            return

        result = extractCompleteSequences(self.buffer)
        self.buffer = result["remainder"]

        for sequence in result["sequences"]:
            self.emitDataSequence(sequence)

        if len(self.buffer) > 0:
            # Only a recognizable reply header establishes a delimited string.
            # Flushing it would turn a delayed tail into text and an OSC's BEL
            # terminator into Ctrl+G (the external editor shortcut). Other input,
            # including Alt+] followed by text, must retain bounded recovery.
            if TERMINAL_REPLY_HEADER_PATTERN.test(self.buffer):
                return
            timeoutMs = self.escapeTimeoutMs if self.buffer == ESC else self.timeoutMs

            def onTimeout() -> None:
                flushed = self.flush()

                for sequence in flushed:
                    if len(sequence) > 2 and isControlString(sequence):
                        # Recover the Alt key and reparse its tail so batched text,
                        # Enter, and control keys are not swallowed as one event.
                        self.emitDataSequence(sequence[:2])
                        self.process(sequence[2:])
                    else:
                        self.emitDataSequence(sequence)

            # MISAKA fork: `setTimeout` on the event loop (see `loop`); input arrives on the loop's thread.
            loop = self.loop if self.loop is not None else asyncio.get_running_loop()
            self.timeout = loop.call_later(timeoutMs / 1000, onTimeout)

    def emitDataSequence(self, sequence: str) -> None:
        # MISAKA fork: a character outside the BMP is one sequence here, so its raw duplicate after a Kitty CSI-u
        # sequence is dropped too (JavaScript sees two surrogate halves, neither matching the codepoint).
        rawCodepoint = ord(sequence) if len(sequence) == 1 else None
        if rawCodepoint is not None and rawCodepoint == self.pendingKittyPrintableCodepoint:
            self.pendingKittyPrintableCodepoint = None
            return

        self.pendingKittyPrintableCodepoint = parseUnmodifiedKittyPrintableCodepoint(sequence)
        self.emit("data", sequence)

    def flush(self) -> list[str]:
        if self.timeout:
            self.timeout.cancel()
            self.timeout = None

        if len(self.buffer) == 0:
            return []

        sequences = [self.buffer]
        self.buffer = ""
        self.pendingKittyPrintableCodepoint = None
        return sequences

    def clear(self) -> None:
        if self.timeout:
            self.timeout.cancel()
            self.timeout = None
        self.buffer = ""
        self.pasteMode = False
        self.pasteBuffer = ""
        self.pendingKittyPrintableCodepoint = None

    def getBuffer(self) -> str:
        return self.buffer

    def destroy(self) -> None:
        self.clear()


__all__ = [
    "StdinBuffer",
    "StdinBufferEventMap",
    "StdinBufferOptions",
]
