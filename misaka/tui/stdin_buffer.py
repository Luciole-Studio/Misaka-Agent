"""Buffered stdin sequence splitting for terminal input streams.

PORT-NOTE: upstream receives strings from Node after setEncoding("utf8"), where
StringDecoder buffers partial multi-byte sequences across chunks. Here the input is raw
bytes, so an incremental decoder is required; otherwise a >4KB CJK paste split at an
os.read boundary yields U+FFFD (reliably reproducible).
The 10ms flush timer runs on the event loop via loop.call_soon_threadsafe (Node's
single-threaded semantics), which removes the race between the timer thread and the
main thread over the buffer.
"""

from __future__ import annotations

import asyncio
import codecs
import threading
from collections import defaultdict
from collections.abc import Callable
from typing import Any, Literal

_ESC = "\x1b"
_BRACKETED_PASTE_START = "\x1b[200~"
_BRACKETED_PASTE_END = "\x1b[201~"

type SequenceStatus = Literal["complete", "incomplete", "not-escape"]
type StdinBufferEvent = Literal["data", "paste"]


def _is_complete_sequence(data: str) -> SequenceStatus:
    if not data.startswith(_ESC):
        return "not-escape"
    if len(data) == 1:
        return "incomplete"

    after_esc = data[1:]
    if after_esc.startswith("["):
        if after_esc.startswith("[M"):
            return "complete" if len(data) >= 6 else "incomplete"
        return _is_complete_csi_sequence(data)
    if after_esc.startswith("]"):
        return _is_complete_osc_sequence(data)
    if after_esc.startswith("P"):
        return _is_complete_dcs_sequence(data)
    if after_esc.startswith("_"):
        return _is_complete_apc_sequence(data)
    if after_esc.startswith("O"):
        return "complete" if len(after_esc) >= 2 else "incomplete"
    if len(after_esc) == 1:
        return "complete"
    return "complete"


def _is_complete_csi_sequence(data: str) -> SequenceStatus:
    if not data.startswith(f"{_ESC}["):
        return "complete"
    if len(data) < 3:
        return "incomplete"

    payload = data[2:]
    last_char = payload[-1]
    last_char_code = ord(last_char)
    if 0x40 <= last_char_code <= 0x7E:
        if payload.startswith("<"):
            if _matches_sgr_mouse(payload):
                return "complete"
            if last_char in {"M", "m"}:
                parts = payload[1:-1].split(";")
                if len(parts) == 3 and all(part.isdigit() for part in parts):
                    return "complete"
            return "incomplete"
        return "complete"
    return "incomplete"


def _is_complete_osc_sequence(data: str) -> SequenceStatus:
    if not data.startswith(f"{_ESC}]"):
        return "complete"
    return "complete" if data.endswith(f"{_ESC}\\") or data.endswith("\x07") else "incomplete"


def _is_complete_dcs_sequence(data: str) -> SequenceStatus:
    if not data.startswith(f"{_ESC}P"):
        return "complete"
    return "complete" if data.endswith(f"{_ESC}\\") else "incomplete"


def _is_complete_apc_sequence(data: str) -> SequenceStatus:
    if not data.startswith(f"{_ESC}_"):
        return "complete"
    return "complete" if data.endswith(f"{_ESC}\\") else "incomplete"


def _matches_sgr_mouse(payload: str) -> bool:
    if len(payload) < 2 or payload[0] != "<" or payload[-1] not in {"M", "m"}:
        return False
    parts = payload[1:-1].split(";")
    return len(parts) == 3 and all(part.isdigit() for part in parts)


def _parse_unmodified_kitty_printable_codepoint(sequence: str) -> int | None:
    import re

    match = re.match(r"^\x1b\[(\d+)(?::\d*)?(?::\d+)?u$", sequence)
    if match is None:
        return None
    codepoint = int(match.group(1))
    return codepoint if codepoint >= 32 else None


def _extract_complete_sequences(buffer: str) -> tuple[list[str], str]:
    sequences: list[str] = []
    pos = 0

    while pos < len(buffer):
        remaining = buffer[pos:]
        if remaining.startswith(_ESC):
            seq_end = 1
            while seq_end <= len(remaining):
                candidate = remaining[:seq_end]
                status = _is_complete_sequence(candidate)
                if status == "complete":
                    if candidate == "\x1b\x1b":
                        next_char = remaining[seq_end] if seq_end < len(remaining) else None
                        if next_char in {"[", "]", "O", "P", "_"}:
                            sequences.append(_ESC)
                            pos += 1
                            break
                    sequences.append(candidate)
                    pos += seq_end
                    break
                if status == "incomplete":
                    seq_end += 1
                    continue
                sequences.append(candidate)
                pos += seq_end
                break

            if seq_end > len(remaining):
                return sequences, remaining
        else:
            sequences.append(remaining[0])
            pos += 1

    return sequences, ""


class StdinBuffer:
    """Buffer partial stdin chunks into logical terminal input sequences."""

    def __init__(self, options: dict[str, Any] | None = None,
                 loop: asyncio.AbstractEventLoop | None = None) -> None:
        opts = options or {}
        self.buffer = ""
        self.timeout: threading.Timer | None = None
        timeout = opts.get("timeout")
        self.timeoutMs = 10 if timeout is None else timeout
        self.pasteMode = False
        self.pasteBuffer = ""
        self.pendingKittyPrintableCodepoint: int | None = None
        self._listeners: dict[str, list[Callable[..., Any]]] = defaultdict(list)
        self._loop = loop
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def on(self, event: StdinBufferEvent, listener: Callable[[str], Any]) -> StdinBuffer:
        self._listeners[event].append(listener)
        return self

    def process(self, data: str | bytes | bytearray) -> None:
        if self.timeout is not None:
            self.timeout.cancel()
            self.timeout = None

        if isinstance(data, (bytes, bytearray)):
            raw = self._decoder.decode(bytes(data))  # a partial multi-byte sequence stays in the decoder until the next chunk
        else:
            raw = data

        if raw == "" and self.buffer == "":
            self._emitDataSequence("")
            return

        self.buffer += raw

        if self.pasteMode:
            self.pasteBuffer += self.buffer
            self.buffer = ""
            end_index = self.pasteBuffer.find(_BRACKETED_PASTE_END)
            if end_index != -1:
                pasted_content = self.pasteBuffer[:end_index]
                remaining = self.pasteBuffer[end_index + len(_BRACKETED_PASTE_END) :]
                self.pasteMode = False
                self.pasteBuffer = ""
                self.pendingKittyPrintableCodepoint = None
                self._emit("paste", pasted_content)
                if remaining:
                    self.process(remaining)
            return

        start_index = self.buffer.find(_BRACKETED_PASTE_START)
        if start_index != -1:
            if start_index > 0:
                before_paste = self.buffer[:start_index]
                sequences, _remainder = _extract_complete_sequences(before_paste)
                for sequence in sequences:
                    self._emitDataSequence(sequence)

            self.pendingKittyPrintableCodepoint = None
            self.buffer = self.buffer[start_index + len(_BRACKETED_PASTE_START) :]
            self.pasteMode = True
            self.pasteBuffer = self.buffer
            self.buffer = ""

            end_index = self.pasteBuffer.find(_BRACKETED_PASTE_END)
            if end_index != -1:
                pasted_content = self.pasteBuffer[:end_index]
                remaining = self.pasteBuffer[end_index + len(_BRACKETED_PASTE_END) :]
                self.pasteMode = False
                self.pasteBuffer = ""
                self.pendingKittyPrintableCodepoint = None
                self._emit("paste", pasted_content)
                if remaining:
                    self.process(remaining)
            return

        sequences, remainder = _extract_complete_sequences(self.buffer)
        self.buffer = remainder
        for sequence in sequences:
            self._emitDataSequence(sequence)

        if self.buffer:
            self.timeout = threading.Timer(self.timeoutMs / 1000.0, self._on_flush_timer)
            self.timeout.daemon = True
            self.timeout.start()

    def _emitDataSequence(self, sequence: str) -> None:
        raw_codepoint = ord(sequence) if len(sequence) == 1 else None
        if (
            raw_codepoint is not None
            and self.pendingKittyPrintableCodepoint is not None
            and raw_codepoint == self.pendingKittyPrintableCodepoint
        ):
            self.pendingKittyPrintableCodepoint = None
            return

        self.pendingKittyPrintableCodepoint = _parse_unmodified_kitty_printable_codepoint(sequence)
        self._emit("data", sequence)

    def flush(self) -> list[str]:
        if self.timeout is not None:
            self.timeout.cancel()
            self.timeout = None

        if self.buffer == "":
            return []

        sequences = [self.buffer]
        self.buffer = ""
        self.pendingKittyPrintableCodepoint = None
        return sequences

    def clear(self) -> None:
        if self.timeout is not None:
            self.timeout.cancel()
            self.timeout = None
        self._decoder.reset()
        self.buffer = ""
        self.pasteMode = False
        self.pasteBuffer = ""
        self.pendingKittyPrintableCodepoint = None

    def getBuffer(self) -> str:
        return self.buffer

    def destroy(self) -> None:
        self.clear()

    def _on_flush_timer(self) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._flush_timeout)
                return
            except RuntimeError:
                pass
        self._flush_timeout()

    def _flush_timeout(self) -> None:
        flushed = self.flush()
        for sequence in flushed:
            self._emitDataSequence(sequence)

    def _emit(self, event: StdinBufferEvent, *args: Any) -> None:
        for listener in list(self._listeners.get(event, [])):
            listener(*args)


StdinBufferOptions = dict[str, Any]
StdinBufferEventMap = dict[str, tuple[str]]

__all__ = [
    "StdinBuffer",
    "StdinBufferEventMap",
    "StdinBufferOptions",
]
