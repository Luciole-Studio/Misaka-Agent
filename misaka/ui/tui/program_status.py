"""Translated from pi-tui's ``packages/tui/src/program-status.ts`` (v1.1.0).

Program Status Protocol (OSC 7501): a program tells the terminal whether it is idle, working,
blocked on the user, done, or failed. Only the root record is supported.

Spec: https://www.superlogical.com/rex/docs/build/program-status
"""

from __future__ import annotations

import base64
from typing import Literal, NotRequired, TypedDict

from misaka.utils import js_regexp
from misaka.utils.js import jsTrim, toWellFormed
from misaka.utils.js_regexp import RegExp


class ProgramStatus(TypedDict):
    # `clear` removes the status instead of reporting one.
    state: Literal["idle", "working", "blocked", "done", "error", "clear"]
    # Stable program name, `[A-Za-z0-9_.+-]{1,32}`. Other values are omitted.
    app: NotRequired[str]
    # What a blocked program waits for. Omitted for other states.
    kind: NotRequired[Literal["permission", "question", "auth"]]
    # One human-readable line. Control characters become spaces; longer text is cut to the spec limit.
    message: NotRequired[str]


# Feature detection query. A supporting terminal replies with the same body.
PROGRAM_STATUS_QUERY = "\x1b]7501;?\x1b\\"


def isProgramStatusReply(sequence: str) -> bool:
    """The reply to {@link PROGRAM_STATUS_QUERY}. Later spec revisions may add pairs after the `?`."""
    return RegExp(r"^\x1b\]7501;\?[^\x07\x1b]*(?:\x07|\x1b\\)$").test(sequence)


APP_PATTERN = RegExp(r"^[A-Za-z0-9_.+-]{1,32}$")
CONTROL_CHARACTERS = RegExp(r"[\u0000-\u001f\u007f-\u009f]+", "g")
# Decoded `msg` limit. Its base64 encoding stays under the 2732-byte encoded limit.
MAX_MESSAGE_BYTES = 2048


def byteLength(text: str) -> int:
    """MISAKA fork: ``Buffer.byteLength(text, "utf8")``; a lone surrogate counts as the U+FFFD that replaces it."""
    return len(toWellFormed(text).encode("utf-8"))


def truncateUtf8(text: str, maxBytes: int) -> str:
    if byteLength(text) <= maxBytes:
        return text
    bytes_ = 0
    end = 0
    for char in text:
        size = byteLength(char)
        if bytes_ + size > maxBytes:
            break
        bytes_ += size
        end += len(char)
    return text[:end]


def formatProgramStatus(status: ProgramStatus) -> str:
    """Encode a status report. Terminals discard reports whose text contains control characters, so they are replaced."""
    pairs = [f"state={status['state']}"]
    app = status.get("app")
    if app is not None and APP_PATTERN.test(app):
        pairs.append(f"app={app}")
    kind = status.get("kind")
    if status["state"] == "blocked" and kind:
        pairs.append(f"kind={kind}")
    message = truncateUtf8(jsTrim(js_regexp.replace(status.get("message") or "", CONTROL_CHARACTERS, " ")), MAX_MESSAGE_BYTES)
    if message:
        pairs.append(f"msg={base64.b64encode(toWellFormed(message).encode('utf-8')).decode('ascii')}")
    return f"\x1b]7501;{':'.join(pairs)}\x1b\\"


__all__ = [
    "PROGRAM_STATUS_QUERY",
    "ProgramStatus",
    "formatProgramStatus",
    "isProgramStatusReply",
]
