"""ANSI-stripping helpers derived from the upstream runtime.

Portions of this file are derived from:
- ansi-regex (https://github.com/chalk/ansi-regex)
- strip-ansi (https://github.com/chalk/strip-ansi)

MIT License

Copyright (c) Sindre Sorhus <sindresorhus@gmail.com> (https://sindresorhus.com)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from misaka.utils.js import UNDEFINED, jsLength, jsSlice

# MISAKA fork: `\d` is spelled `[0-9]` (a Python `\d` also matches non-ASCII digits) and the end anchor is `\Z` (a
# JavaScript `$` without the `m` flag does not match before a final newline).

# Valid string terminator sequences are BEL, ESC\, and 0x9c
_ST = r"(?:\u0007|\u001B\\|\u009C)"

# OSC sequences: ESC ] ... ST
_OSC_START = r"\u001B\]"

# CSI and related: ESC/C1, optional intermediates, optional params (supports ; and :), then final byte
_CSI_START = r"[\u001B\u009B][\[\]()#;?]*(?:[0-9]{1,4}(?:[;:][0-9]{0,4})*)?"
_CSI_FINAL = r"[0-9A-PR-TZcf-nq-uy=><~]"

# Complete sequences. OSC is non-greedy until the first ST.
_ansiRegex = re.compile(rf"(?:{_OSC_START}[\s\S]*?{_ST})|{_CSI_START}{_CSI_FINAL}")

# Unfinished sequence at the end of the text: OSC without its ST (a trailing ESC may start ESC\),
# or CSI without its final byte.
_unfinishedAnsiAtEndRegex = re.compile(rf"(?:{_OSC_START}(?:[^\u0007\u009C\u001B]|\u001B(?!\\))*|{_CSI_START})\Z")

# Longest unfinished sequence held back while streaming. Longer ones are processed as-is.
_MAX_PENDING_ANSI_LENGTH = 256


class SplitAnsiText(NamedTuple):
    """MISAKA fork: upstream's `{ complete, pending }` object."""

    complete: str
    pending: str


def splitIncompleteAnsiSuffix(value: str) -> SplitAnsiText:
    """Split streamed text into a part that is safe to pass to stripAnsi now and a trailing
    unfinished escape sequence that should be prepended to the next chunk."""
    if "\u001b" not in value and "\u009b" not in value:
        return SplitAnsiText(complete=value, pending="")
    windowStart = max(0, jsLength(value) - _MAX_PENDING_ANSI_LENGTH)
    window = jsSlice(value, windowStart)
    match = _unfinishedAnsiAtEndRegex.search(window)
    if not match:
        return SplitAnsiText(complete=value, pending="")
    splitAt = windowStart + jsLength(window[: match.start()])
    return SplitAnsiText(complete=jsSlice(value, 0, splitAt), pending=jsSlice(value, splitAt))


def _jsTypeof(value: object) -> str:
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "object"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float | complex):
        return "number"
    if isinstance(value, str):
        return "string"
    return "object"


def stripAnsi(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"Expected a `string`, got `{_jsTypeof(value)}`")

    # Fast path: ANSI codes require ESC (7-bit) or CSI (8-bit) introducer
    if "\u001b" not in value and "\u009b" not in value:
        return value

    return _ansiRegex.sub("", value)


__all__ = ["SplitAnsiText", "splitIncompleteAnsiSuffix", "stripAnsi"]
