"""Translated from grok-mermaid 0.2.3's ``src/labels.ts``."""

from __future__ import annotations

from typing import Final

import regex

from misaka.utils.js import jsSplitWhitespace, jsTrim

from .width import measured, stringWidth

# Node labels wrap to at most this many display columns per line ...
WRAP_WIDTH: Final = 24
# ... and at most this many lines; overflow is truncated with an ellipsis.
MAX_LINES: Final = 4
# Edge labels are truncated to this many columns.
MAX_LABEL: Final = 28

# Identifier-boundary characters preferred as break points when a single word
# is too wide to fit, so it is not sliced mid-segment.
#
# Mirrors `TOKEN_BREAK_CHARS` in grok-build's
# `third_party/mermaid-to-svg/src/text_wrap.rs`; the two renderers are
# deliberately independent, so keep these in sync.
LABEL_BREAK_CHARS: Final = ["_", "-", ".", "/"]


def asciiLower(s: str) -> str:
    """ASCII-only case folding, matching Rust's ``to_ascii_lowercase``.

    ``String.prototype.toLowerCase`` can change a string's length (``İ`` becomes two
    code points), which would desync the byte offsets some parsers slice with.
    """
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in s)


def asciiUpper(s: str) -> str:
    return "".join(chr(ord(c) - 32) if "a" <= c <= "z" else c for c in s)


# C0 and C1 controls, less the `\t\n\r` the parsers and `srcLines` read.
#
# They measure one column and paint none, so a box sized around one is drawn a
# column short of its own border; NUL also collides with the `CONT` sentinel
# and is dropped after layout has already paid for its cell; ESC would inject
# ANSI into the caller's scrollback. `decodeEntityBody` refuses to decode an
# entity into one — this closes the same hole for literals.
_CONTROLS: Final = regex.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def stripControls(src: str) -> str:
    """Applied by every public entry point that takes untrusted source."""
    return _CONTROLS.sub("", src)


def srcLines(src: str) -> list[str]:
    """Split source into lines the way Rust's ``str::lines()`` does: on ``\\n``, with a
    trailing ``\\r`` stripped, and *without* a final empty line when the input ends
    in a newline. ``String.split`` yields that extra element, which would show up
    as a spurious blank row inside a source box."""
    out = [line.removesuffix("\r") for line in src.split("\n")]
    if len(out) > 0 and out[-1] == "":
        out.pop()
    return out


_ALNUM: Final = regex.compile(r"[\p{Alphabetic}\p{N}]")


def isAlphanumeric(c: str | None) -> bool:
    """Matches Rust's ``char::is_alphanumeric``."""
    # `ALNUM.test(undefined)` tests the string "undefined".
    return _ALNUM.search(c if c is not None else "undefined") is not None


def isIdChar(c: str | None) -> bool:
    """Characters allowed in a bare node/state/class identifier."""
    return isAlphanumeric(c) or c == "_"


ENTITY_LOOKAHEAD: Final = 10

NAMED_ENTITIES: Final = {
    "lt": "<",
    "gt": ">",
    "amp": "&",
    "quot": '"',
    "apos": "'",
}


def decodeEntityBody(body: str) -> str | None:
    named = NAMED_ENTITIES.get(body)
    if named is not None:
        return named
    if not body.startswith("#"):
        return None
    num = body[1:]
    hex = num[:1] in ("x", "X")
    digits = num[1:] if hex else num
    if not regex.fullmatch(r"[0-9a-fA-F]+" if hex else r"[0-9]+", digits):
        return None
    code = int(digits, 16 if hex else 10)
    # Surrogates and out-of-range values are not characters at all.
    if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
        return None
    # Reject control chars: NUL collides with the CONT sentinel and ESC would
    # inject ANSI into scrollback.
    if code < 0x20 or 0x7F <= code <= 0x9F:
        return None
    return chr(code)


def decodeHtmlEntities(s: str) -> str:
    """Decode HTML entities in label text. Called once per label: via ``cleanLabel``
    for bracketed labels, or explicitly at each direct-push sink."""
    if "&" not in s:
        return s
    chars = list(s)
    out = ""
    i = 0
    while i < len(chars):
        if chars[i] != "&":
            out += chars[i]
            i += 1
            continue
        # Scan a bounded window including the terminating `;`, so a stray `&` or an
        # over-long run stays literal.
        hi = min(i + 1 + ENTITY_LOOKAHEAD, len(chars))
        semi = -1
        for j in range(i + 1, hi):
            if chars[j] == ";":
                semi = j
                break
        decoded = None if semi == -1 else decodeEntityBody("".join(chars[i + 1 : semi]))
        if decoded is None:
            out += "&"
            i += 1
        else:
            # Resume past the `;`. The single pass never re-scans emitted text, so
            # `&amp;lt;` decodes to the literal `&lt;` rather than to `<`.
            out += decoded
            i = semi + 1
    return out


def stripMarkdown(s: str) -> str:
    """Strip markdown emphasis from a `` `backtick` `` label string."""
    noCode = "".join(c for c in s if c != "`")
    noStrong = noCode.replace("**", "").replace("__", "")
    chars = list(noStrong)
    out = ""
    for i, c in enumerate(chars):
        # Keep `*`/`_` only when they sit inside a word, so snake_case survives.
        inWord = i > 0 and isAlphanumeric(chars[i - 1]) and i + 1 < len(chars) and isAlphanumeric(chars[i + 1])
        if c in ("*", "_") and not inWord:
            continue
        out += c
    return jsTrim(out)


# Inline formatting tags that carry no meaning in a terminal. Anything else
# that looks like a tag — `Vec<String>`, `<id>` — is left alone.
HTML_FORMAT_TAGS: Final = frozenset({
    "b", "strong", "i", "em", "u", "s", "strike", "del", "ins", "mark", "small", "big", "sub", "sup", "code",
    "kbd", "samp", "var", "tt", "span", "font", "q", "abbr", "cite", "pre",
})


def _isAsciiAlnum(c: str) -> bool:
    return "0" <= c <= "9" or "A" <= c <= "Z" or "a" <= c <= "z"


def htmlTagAt(chars: list[str], start: int) -> tuple[str, int] | None:
    """Read a tag starting at ``start``, returning its name and the index after ``>``."""
    i = start + 1
    if i < len(chars) and chars[i] == "/":
        i += 1
    nameStart = i
    while i < len(chars) and _isAsciiAlnum(chars[i]):
        i += 1
    if i == nameStart:
        return None
    name = "".join(chars[nameStart:i])
    while i < len(chars) and chars[i] != ">":
        if chars[i] == "<":
            return None
        i += 1
    return (name, i + 1) if i < len(chars) and chars[i] == ">" else None


def stripHtmlTags(s: str) -> str:
    chars = list(s)
    out = ""
    i = 0
    while i < len(chars):
        if chars[i] == "<":
            tag = htmlTagAt(chars, i)
            if tag:
                lower = tag[0].lower()
                if lower == "br":
                    out += " "
                    i = tag[1]
                    continue
                if lower in HTML_FORMAT_TAGS:
                    i = tag[1]
                    continue
        out += chars[i]
        i += 1
    return out


def unwrap(s: str, open: str, close: str) -> str | None:
    """Strip one matching pair of wrapping delimiters, if present."""
    return (
        s[len(open) : len(s) - len(close)]
        if len(s) >= len(open) + len(close) and s.startswith(open) and s.endswith(close)
        else None
    )


def cleanLabel(raw: str) -> str:
    """Normalise raw label text: strip markup, unquote, and decode entities.

    Decoding happens after tag-stripping so ``<b>`` is removed as markup while
    ``&lt;b&gt;`` survives as the literal text ``<b>``.
    """
    trimmed = jsTrim(stripHtmlTags(jsTrim(raw)))
    quoted = unwrap(trimmed, '"', '"')
    if quoted is None:
        quoted = unwrap(trimmed, "'", "'")
    unquoted = jsTrim(quoted if quoted is not None else trimmed)
    md = unwrap(unquoted, "`", "`")
    return decodeHtmlEntities(unquoted if md is None else stripMarkdown(jsTrim(md)))


def lastBreak(s: str) -> int:
    """Index of the last identifier-boundary character, or -1."""
    best = -1
    for c in LABEL_BREAK_CHARS:
        best = max(best, s.rfind(c))
    return best


def wrapLabel(label: str, width: int, maxLines: int) -> list[str]:
    """Wrap a label to ``width`` columns over at most ``maxLines`` lines, truncating the
    last line with an ellipsis if it overflows.

    A word too wide to fit is broken after the last identifier boundary
    (``_-./``) that fits, falling back to a per-character break when it has none.
    """
    width = max(1, width)
    lines: list[str] = []
    cur = ""
    curW = 0

    for word in (w for w in jsSplitWhitespace(label) if w != ""):
        ww = stringWidth(word)
        if ww > width:
            if cur != "":
                lines.append(cur)
                cur = ""
            chunk = ""
            chunkW = 0
            for ch, cw in measured(word):
                if chunkW + cw > width and chunk != "":
                    p = lastBreak(chunk)
                    carry = "" if p == -1 else chunk[p + 1 :]
                    lines.append(chunk if p == -1 else chunk[: p + 1])
                    chunk = carry
                    chunkW = stringWidth(carry)
                chunk += ch
                chunkW += cw
            cur = chunk
            curW = chunkW
        elif cur == "":
            cur = word
            curW = ww
        elif curW + 1 + ww <= width:
            cur += f" {word}"
            curW += 1 + ww
        else:
            lines.append(cur)
            cur = word
            curW = ww
    if cur != "":
        lines.append(cur)
    if len(lines) == 0:
        lines.append("")

    if len(lines) > maxLines:
        del lines[maxLines:]
        target = max(1, width - 1)
        s = ""
        sw = 0
        for ch, cw in measured(lines[-1]):
            if sw + cw > target:
                break
            s += ch
            sw += cw
        lines[-1] = f"{s}…"
    return lines


def fitLabel(label: str, inner: int) -> str:
    """Truncate to ``inner`` columns, leaving room for the ellipsis."""
    if stringWidth(label) <= inner:
        return label
    out = ""
    used = 0
    for c, cw in measured(label):
        if used + cw + 1 > inner:
            break
        out += c
        used += cw
    return f"{out}…"


__all__ = [
    "LABEL_BREAK_CHARS",
    "MAX_LABEL",
    "MAX_LINES",
    "WRAP_WIDTH",
    "asciiLower",
    "asciiUpper",
    "cleanLabel",
    "decodeHtmlEntities",
    "fitLabel",
    "isAlphanumeric",
    "isIdChar",
    "srcLines",
    "stripControls",
    "stripHtmlTags",
    "stripMarkdown",
    "wrapLabel",
]
