"""Translated from pi-tui's ``packages/tui/src/utils.ts`` (v1.3.0).

MISAKA forks, each marked where it happens:

- ``Intl.Segmenter`` is a small class over the ``regex`` module: ``\\X`` for graphemes (the same clusters as ICU) and
  Unicode default word boundaries for words (ICU's CJK/Thai dictionaries are not available, so a Han or Hiragana run
  segments per character).
- ``\\p{RGI_Emoji}`` is a lookup in the Unicode 17 RGI sequence set (``vendor/unicode_rgi_emoji.py``): Python regex
  engines have no string properties.
- CSI sequences follow ECMA-48 (``ansiCodeLength``), where upstream scans for the first ``m``/``G``/``K``/``H``/``J``.
- Speed: a character table measures strings that cannot form multi-character clusters in one ``translate`` call,
  ``graphemeWidth`` is memoised, the width cache is larger, and ``asciiVisibleWidth`` uses ``str`` methods.
- String offsets count code points (Python strings), where upstream counts UTF-16 code units; both are consistent
  within their side.
- Parameters pi names ``str`` are ``str_``: the functions use the ``str`` type.
- ``getGraphemeCellRange``, ``getActiveBackgroundAnsi`` and ``AnsiCodeTracker.getActiveBackgroundCode`` serve only
  the fullscreen renderer (``tui-alt-screen.ts``, ``layout.ts``), which is not ported (D1).
"""

from __future__ import annotations

import functools
import itertools
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Literal

import regex

from misaka.utils import js_regexp
from misaka.utils.js import UNDEFINED, jsLength, jsTrim, jsTrimEnd
from misaka.utils.js_regexp import RegExp

from .vendor.get_east_asian_width import eastAsianWidth
from .vendor.unicode_rgi_emoji import RGI_EMOJI


@dataclass(slots=True)
class SegmentData:
    """``Intl.SegmentData``: the segment, its index in the input, and (word granularity) whether it is word-like."""

    segment: str
    index: int
    input: str
    isWordLike: bool | None = None


_GRAPHEME_CLUSTER = regex.compile(r"\X")
_WORD_BOUNDARY = regex.compile(r"\b", regex.WORD)
_WORD_LIKE = regex.compile(r"[\p{L}\p{N}]")


class Segmenter:
    """MISAKA fork: ``Intl.Segmenter`` over the ``regex`` module.

    Graphemes are ``\\X``, the same extended grapheme clusters as ICU's. Words split at Unicode default word
    boundaries; a segment is word-like when it holds a letter or a digit. ICU additionally segments Han, Hiragana and
    Thai with dictionaries, which are not available here: those runs segment per character.
    """

    def __init__(self, granularity: Literal["grapheme", "word"] = "grapheme") -> None:
        self.granularity = granularity

    def segment(self, text: str) -> list[SegmentData]:
        if self.granularity == "grapheme":
            return [SegmentData(match.group(), match.start(), text) for match in _GRAPHEME_CLUSTER.finditer(text)]
        boundaries = sorted({0, len(text), *(match.start() for match in _WORD_BOUNDARY.finditer(text))})
        segments: list[SegmentData] = []
        for start, end in itertools.pairwise(boundaries):
            segment = text[start:end]
            segments.append(SegmentData(segment, start, text, _WORD_LIKE.search(segment) is not None))
        return segments


# segmenters (shared instance)
graphemeSegmenter = Segmenter("grapheme")
wordSegmenter = Segmenter("word")


def getGraphemeSegmenter() -> Segmenter:
    """Get the shared grapheme segmenter instance."""
    return graphemeSegmenter


def getWordSegmenter() -> Segmenter:
    """Get the shared word segmenter instance."""
    return wordSegmenter


def couldBeEmoji(segment: str) -> bool:
    """Check if a grapheme cluster (after segmentation) could possibly be an RGI emoji.
    This is a fast heuristic to avoid the expensive rgiEmojiRegex test.
    The tested Unicode blocks are deliberately broad to account for future
    Unicode additions.
    """
    cp = ord(segment[0])
    return (
        (cp >= 0x1F000 and cp <= 0x1FBFF)  # Emoji and Pictograph
        or (cp >= 0x2300 and cp <= 0x23FF)  # Misc technical
        or (cp >= 0x2600 and cp <= 0x27BF)  # Misc symbols, dingbats
        or (cp >= 0x2B50 and cp <= 0x2B55)  # Specific stars/circles
        or "️" in segment  # Contains VS16 (emoji presentation selector)
        or jsLength(segment) > 2  # Multi-codepoint sequences (ZWJ, skin tones, etc.)
    )


# Regexes for character classification (same as string-width library)
zeroWidthRegex = RegExp(r"^(?:\p{Default_Ignorable_Code_Point}|\p{Control}|\p{Mark}|\p{Surrogate})+$", "v")
leadingNonPrintingRegex = RegExp(r"^[\p{Default_Ignorable_Code_Point}\p{Control}\p{Format}\p{Mark}\p{Surrogate}]+", "v")
nonPrintingCharRegex = RegExp(r"^(?:\p{Default_Ignorable_Code_Point}|\p{Control}|\p{Format}|\p{Mark}|\p{Surrogate})$", "v")
markCharRegex = RegExp(r"^\p{Mark}$", "v")
# Marks that terminals allocate cells for when attached to a base character.
# This includes Unicode spacing marks and non-spacing exceptions in legacy wcwidth tables.
terminalSpacingMarkRegex = RegExp(
    r"^(?:[\p{Spacing_Mark}--[᜴〮〯]]|[ٟཿါာေဳ-ဵး်-ှ])+$",
    "v",
)


class _RgiEmojiTest:
    """MISAKA fork: ``/^\\p{RGI_Emoji}$/v`` as a lookup in the RGI sequence set."""

    def test(self, segment: str) -> bool:
        return segment in RGI_EMOJI


rgiEmojiRegex = _RgiEmojiTest()

# Cache for non-ASCII strings
# MISAKA fork: larger than upstream's 512; a long transcript re-measures thousands of distinct lines per render.
WIDTH_CACHE_SIZE = 16384
widthCache: dict[str, int] = {}

cjkBreakRegex = RegExp(
    r"[\p{Script_Extensions=Han}\p{Script_Extensions=Hiragana}\p{Script_Extensions=Katakana}\p{Script_Extensions=Hangul}\p{Script_Extensions=Bopomofo}]",
    "u",
)

# CJK letters remain part of words and paths; only punctuation can separate prose from completions.
cjkPunctuationRegex = RegExp(
    f"(?:(?=\\p{{Punctuation}}){cjkBreakRegex.source}|[，．：；！？（）［］｛｝“”‘’…—])",
    "u",
)
autocompleteSeparatorRegex = RegExp(f"(?:\\s|{cjkPunctuationRegex.source})", "u")
autocompleteBoundaryRegex = RegExp(f"(?:^|{autocompleteSeparatorRegex.source})", "u")


def isPrintableAscii(str_: str) -> bool:
    # MISAKA fork: `str` methods for upstream's charCode loop (U+0020-U+007E, the same set).
    return str_.isascii() and str_.isprintable()


@dataclass(slots=True)
class SliceResult:
    """``{ text, width }``."""

    text: str
    width: int


def truncateFragmentToWidth(text: str, maxWidth: int) -> SliceResult:
    if maxWidth <= 0 or len(text) == 0:
        return SliceResult(text="", width=0)

    if isPrintableAscii(text):
        clipped = text[:maxWidth]
        return SliceResult(text=clipped, width=len(clipped))

    hasAnsi = "\x1b" in text
    hasTabs = "\t" in text
    if not hasAnsi and not hasTabs:
        result = ""
        width = 0
        for data in graphemeSegmenter.segment(text):
            segment = data.segment
            w = graphemeWidth(segment)
            if width + w > maxWidth:
                break
            result += segment
            width += w
        return SliceResult(text=result, width=width)

    result = ""
    width = 0
    i = 0
    pendingAnsi = ""

    while i < len(text):
        ansi = extractAnsiCode(text, i)
        if ansi:
            pendingAnsi += ansi.code
            i += ansi.length
            continue

        if text[i] == "\t":
            if width + 3 > maxWidth:
                break
            if pendingAnsi:
                result += pendingAnsi
                pendingAnsi = ""
            result += "\t"
            width += 3
            i += 1
            continue

        end = i
        while end < len(text) and text[end] != "\t":
            nextAnsi = extractAnsiCode(text, end)
            if nextAnsi:
                break
            end += 1

        for data in graphemeSegmenter.segment(text[i:end]):
            segment = data.segment
            w = graphemeWidth(segment)
            if width + w > maxWidth:
                return SliceResult(text=result, width=width)
            if pendingAnsi:
                result += pendingAnsi
                pendingAnsi = ""
            result += segment
            width += w
        i = end

    return SliceResult(text=result, width=width)


def finalizeTruncatedResult(
    prefix: str,
    prefixWidth: int,
    ellipsis: str,
    ellipsisWidth: int,
    maxWidth: int,
    pad: bool,
) -> str:
    reset = "\x1b[0m"
    hyperlinkClose = getActiveOsc8Close(prefix)
    visibleWidth = prefixWidth + ellipsisWidth

    if len(ellipsis) > 0:
        result = f"{prefix}{hyperlinkClose}{reset}{ellipsis}{reset}"
    else:
        result = f"{prefix}{hyperlinkClose}{reset}"

    return result + " " * max(0, maxWidth - visibleWidth) if pad else result


# MISAKA fork: memoised (the function is pure); a render measures the same clusters again and again.
@functools.lru_cache(maxsize=65536)
def graphemeWidth(segment: str) -> int:
    """Calculate the terminal width of a single grapheme cluster.
    Based on code from the string-width library, but includes a possible-emoji
    check to avoid running the RGI_Emoji regex unnecessarily.
    """
    if len(segment) == 1:
        code = ord(segment[0])
        if code >= 0x20 and code <= 0x7E:
            return 1
        if code == 0x09:
            return 3

    # Some marks occupy cells even without a base character.
    if terminalSpacingMarkRegex.test(segment):
        return len(segment)

    # Zero-width clusters
    if zeroWidthRegex.test(segment):
        return 0

    # Emoji check with pre-filter
    if couldBeEmoji(segment) and rgiEmojiRegex.test(segment):
        return 2

    # Get base visible codepoint
    base = js_regexp.replace(segment, leadingNonPrintingRegex, "")
    if not base:
        return 0
    cp = ord(base[0])

    # Regional indicator symbols (U+1F1E6..U+1F1FF) are often rendered as
    # full-width emoji in terminals, even when isolated during streaming.
    # Keep width conservative (2) to avoid terminal auto-wrap drift artifacts.
    if cp >= 0x1F1E6 and cp <= 0x1F1FF:
        return 2

    width = eastAsianWidth(cp)

    # Intl.Segmenter can group multiple terminal-spacing code points into one
    # grapheme. Count trailing visible code points that terminals may allocate
    # cells for: Indic consonants after marks, halfwidth/fullwidth forms, and
    # Thai/Lao AM vowels.
    followsMark = False
    chars = list(base)
    for char in chars[1:]:
        if terminalSpacingMarkRegex.test(char):
            width += 1
            followsMark = False
        elif markCharRegex.test(char):
            followsMark = True
        elif not nonPrintingCharRegex.test(char):
            c = ord(char)
            if followsMark or (c >= 0xFF00 and c <= 0xFFEF):
                # halfwidth + fullwidth forms
                width += eastAsianWidth(c)
            elif c == 0x0E33 or c == 0x0EB3:
                width += 1
            followsMark = False

    return width


# MISAKA fork: pi measures width grapheme by grapheme (Intl.Segmenter), which V8 makes cheap and CPython does not; a
# full render of a long transcript spent most of its time there. A character whose Grapheme_Cluster_Break is Other, LV
# or LVT never joins a neighbour into a cluster, so a string made only of such characters has, cluster by cluster, the
# widths of its characters: it is measured by one translate through this table (code point -> "1" or "2"). Anything
# else, or a character of width 0, takes pi's path. The digits are in the table from the start so that an
# untranslated "2" can never be read as a width.
_WIDTHS: dict[int, str] = {ord("1"): "1", ord("2"): "1"}
_CLUSTERING: set[str] = set()
_NEVER_JOINS = regex.compile(r"[\p{Grapheme_Cluster_Break=Other}\p{Grapheme_Cluster_Break=LV}\p{Grapheme_Cluster_Break=LVT}]")


def _simpleWidth(char: str) -> int:
    """The width of a character that never joins a cluster; -1 when pi's grapheme path must decide."""
    if _NEVER_JOINS.fullmatch(char) is None:
        return -1
    width = graphemeWidth(char)
    return width if width >= 1 else -1


def _widths(text: str) -> str | None:
    """``text`` with every character replaced by its column width, "1" or "2"; None when a character may join a
    grapheme cluster or is zero-width. A character seen for the first time is classified once."""
    widths = text.translate(_WIDTHS)
    if widths.count("1") + widths.count("2") == len(widths):
        return widths
    for char in set(text):
        if ord(char) in _WIDTHS:
            continue
        if char in _CLUSTERING:
            return None
        width = _simpleWidth(char)
        if width < 1:
            _CLUSTERING.add(char)
            return None
        _WIDTHS[ord(char)] = "2" if width == 2 else "1"
    return text.translate(_WIDTHS)


def visibleWidth(str_: str) -> int:
    """Calculate the visible width of a string in terminal columns."""
    if len(str_) == 0:
        return 0

    # Fast path: printable ASCII, tabs, and ANSI escape sequences. Styled lines take this path, so
    # re-rendering after a theme change does not run grapheme segmentation on every line.
    asciiWidth = asciiVisibleWidth(str_)
    if asciiWidth != -1:
        return asciiWidth

    # Check cache
    cached = widthCache.get(str_)
    if cached is not None:
        return cached

    # Normalize: tabs to 3 spaces, strip ANSI escape codes
    clean = str_
    if "\t" in str_:
        clean = clean.replace("\t", "   ")
    if "\x1b" in clean:
        # Strip supported ANSI/OSC/APC escape sequences in one pass.
        # This covers CSI styling/cursor codes, OSC hyperlinks and prompt markers,
        # and APC sequences like CURSOR_MARKER.
        # MISAKA fork: one regex pass for upstream's indexOf/ansiCodeLength loop (what is left of an ESC is text).
        clean = _ESCAPE_REGEX.sub("", clean)

    # Calculate width
    # MISAKA fork: the character table first (see `_WIDTHS`).
    widths = _widths(clean)
    if widths is not None:
        width = len(widths) + widths.count("2")
    else:
        width = 0
        for data in graphemeSegmenter.segment(clean):
            width += graphemeWidth(data.segment)

    # Cache result
    if len(widthCache) >= WIDTH_CACHE_SIZE:
        firstKey = next(iter(widthCache), None)
        if firstKey is not None:
            del widthCache[firstKey]
    widthCache[str_] = width

    return width


def stripTerminalSequences(str_: str) -> str:
    """Remove ANSI, OSC, and APC control sequences while preserving visible text."""
    if "\x1b" not in str_:
        return str_
    result = ""
    i = 0
    while i < len(str_):
        ansi = extractAnsiCode(str_, i)
        if ansi:
            i += ansi.length
            continue
        result += str_[i]
        i += 1
    return result


_OSC8_LINK = RegExp(r"^\x1b\]8;[^;]*;([^\x07\x1b]*)(?:\x07|\x1b\\)$")


def getOsc8LinkAtColumn(line: str, column: int) -> str | None:
    """Return the OSC 8 hyperlink covering a visible terminal column."""
    activeUrl: str | None = None
    currentCol = 0
    i = 0
    while i < len(line):
        ansi = extractAnsiCode(line, i)
        if ansi:
            hyperlink = _OSC8_LINK.exec(ansi.code)
            if hyperlink:
                activeUrl = hyperlink[1] or None
            i += ansi.length
            continue
        textEnd = i
        while textEnd < len(line) and not extractAnsiCode(line, textEnd):
            textEnd += 1
        for data in graphemeSegmenter.segment(line[i:textEnd]):
            segment = data.segment
            width = 3 if segment == "\t" else graphemeWidth(segment)
            if column >= currentCol and column < currentCol + width:
                return activeUrl
            currentCol += width
        i = textEnd
    return None


# Normalize text for terminal output without changing logical editor content.
# Some terminals render precomposed Thai/Lao AM vowels inconsistently during
# differential repaint. Their compatibility decompositions have the same cell
# width but avoid stale-cell artifacts in terminal renderers. Visible tabs are
# expanded to the fixed width used by layout so terminal tab stops cannot wrap
# a logical line, while tabs inside terminal string sequences stay untouched.
THAI_LAO_AM_REGEX = RegExp(r"[ำຳ]")
THAI_LAO_AM_GLOBAL_REGEX = RegExp(r"[ำຳ]", "g")


def normalizeTerminalOutput(str_: str) -> str:
    normalized = str_
    if THAI_LAO_AM_REGEX.test(normalized):
        normalized = js_regexp.replace(
            normalized,
            THAI_LAO_AM_GLOBAL_REGEX,
            lambda char: "ํา" if char == "ำ" else "ໍາ",
        )
    if "\t" not in normalized:
        return normalized

    result = ""
    i = 0
    while i < len(normalized):
        ansi = extractAnsiCode(normalized, i)
        if ansi:
            result += ansi.code
            i += ansi.length
            continue
        result += "   " if normalized[i] == "\t" else normalized[i]
        i += 1
    return result


@dataclass(slots=True)
class AnsiMatch:
    """``{ code, length }``."""

    code: str
    length: int


def extractAnsiCode(str_: str, pos: int) -> AnsiMatch | None:
    """Extract ANSI escape sequences from a string at the given position."""
    length = ansiCodeLength(str_, pos)
    return AnsiMatch(code=str_[pos : pos + length], length=length) if length > 0 else None


def asciiVisibleWidth(str_: str) -> int:
    """Width of a string made of printable ASCII, tabs, and ANSI escape sequences, or -1 if it contains
    anything else. Matches `visibleWidth` for those strings without allocating.
    """
    # MISAKA fork: `str` methods and the escape regex for upstream's charCode loop; the same results.
    if not str_.isascii():
        return -1
    tabs = str_.count("\t")
    if "\x1b" not in str_:
        if tabs == 0:
            return len(str_) if str_.isprintable() else -1
        return len(str_) + 2 * tabs if str_.replace("\t", "").isprintable() else -1
    width = 0
    i = 0
    length = len(str_)
    while i < length:
        escape = str_.find("\x1b", i)
        end = length if escape == -1 else escape
        if end > i:
            run = str_[i:end]
            runTabs = run.count("\t")
            if not run.replace("\t", "").isprintable():
                return -1
            width += len(run) + 2 * runTabs
        if escape == -1:
            break
        codeLength = ansiCodeLength(str_, escape)
        if codeLength == 0:
            return -1
        i = escape + codeLength
    return width


# MISAKA fork: ECMA-48 CSI: parameter bytes 0x30-0x3F, then intermediate bytes 0x20-0x2F, then one final byte
# 0x40-0x7E. Upstream instead scans forward for the first `m`, `G`, `K`, `H` or `J`, which makes any other CSI
# (`\x1b[10A`, `\x1b[6n`, `\x1b[?25l`) swallow every character up to the next `m`. The TUI appends `\x1b[0m` to every
# line, so that `m` always exists, and `visibleWidth` then counts the swallowed text as zero: lines are wrapped and
# truncated against a width that is far too small, the terminal soft-wraps them, and the over-wide-line guard never
# fires. A sequence that does not terminate is not an escape. OSC and APC strings run to BEL or ESC \ (a lone ESC
# inside them is part of the string), as upstream.
_ESCAPE_REGEX = re.compile(
    r"\x1b(?:\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]|[\]_](?:[^\x07\x1b]|\x1b(?!\\))*(?:\x07|\x1b\\))"
)


def ansiCodeLength(str_: str, pos: int) -> int:
    """Length of the ANSI/OSC/APC escape sequence starting at `pos`, or 0 if there is none."""
    if pos >= len(str_) or str_[pos] != "\x1b":
        return 0
    match = _ESCAPE_REGEX.match(str_, pos)
    return match.end() - pos if match is not None else 0


type Osc8Terminator = Literal["\x07", "\x1b\\"]


@dataclass(slots=True)
class ActiveHyperlink:
    params: str
    url: str
    terminator: Osc8Terminator


def parseOsc8Hyperlink(ansiCode: str) -> Any:
    """An open hyperlink, ``None`` (null) for a close, or ``UNDEFINED`` when the code is not an OSC 8 link.

    MISAKA fork: upstream's ``undefined`` is ``misaka.utils.js.UNDEFINED`` (``null`` is ``None``)."""
    if not ansiCode.startswith("\x1b]8;"):
        return UNDEFINED

    terminator: Osc8Terminator = "\x07" if ansiCode.endswith("\x07") else "\x1b\\"
    body = ansiCode[4 : -1 if terminator == "\x07" else -2]
    separatorIndex = body.find(";")
    if separatorIndex == -1:
        return UNDEFINED

    params = body[:separatorIndex]
    url = body[separatorIndex + 1 :]
    if not url:
        return None
    return ActiveHyperlink(params=params, url=url, terminator=terminator)


def formatOsc8Hyperlink(hyperlink: ActiveHyperlink) -> str:
    return f"\x1b]8;{hyperlink.params};{hyperlink.url}{hyperlink.terminator}"


def formatOsc8Close(terminator: Osc8Terminator) -> str:
    return f"\x1b]8;;{terminator}"


def getActiveOsc8Close(prefix: str) -> str:
    if "\x1b]8;" not in prefix:
        return ""

    activeHyperlink: ActiveHyperlink | None = None
    i = 0
    while i < len(prefix):
        ansi = extractAnsiCode(prefix, i)
        if ansi:
            hyperlink = parseOsc8Hyperlink(ansi.code)
            if hyperlink is not UNDEFINED:
                activeHyperlink = hyperlink
            i += ansi.length
        else:
            i += 1
    return formatOsc8Close(activeHyperlink.terminator) if activeHyperlink else ""


_SGR_PARAMS = RegExp(r"\x1b\[([\d;]*)m")


class AnsiCodeTracker:
    """Track active ANSI SGR codes to preserve styling across line breaks."""

    def __init__(self) -> None:
        # Track individual attributes separately so we can reset them specifically
        self.bold = False
        self.dim = False
        self.italic = False
        self.underline = False
        self.blink = False
        self.inverse = False
        self.hidden = False
        self.strikethrough = False
        self.fgColor: str | None = None  # Stores the full code like "31" or "38;5;240"
        self.bgColor: str | None = None  # Stores the full code like "41" or "48;5;240"
        self.activeHyperlink: ActiveHyperlink | None = None

    def process(self, ansiCode: str) -> None:
        # OSC 8 hyperlink: \x1b]8;;<url>\x1b\\ (open) or \x1b]8;;\x1b\\ (close).
        # Preserve the original terminator because some terminals only make BEL-terminated
        # links clickable. OAuth login URLs use BEL, so reopening wrapped lines with ST
        # made only the first physical line clickable in those terminals.
        hyperlink = parseOsc8Hyperlink(ansiCode)
        if hyperlink is not UNDEFINED:
            self.activeHyperlink = hyperlink
            return

        if not ansiCode.endswith("m"):
            return

        # MISAKA fork: the SGR parameters are parsed once per distinct code (`_sgrOperations` runs upstream's
        # parameter switch, below, on a recorder) and the recorded changes are replayed here.
        for name, value in _sgrOperations(ansiCode):
            if name == "reset":
                self.reset()
            else:
                setattr(self, name, value)

    def reset(self) -> None:
        self.bold = False
        self.dim = False
        self.italic = False
        self.underline = False
        self.blink = False
        self.inverse = False
        self.hidden = False
        self.strikethrough = False
        self.fgColor = None
        self.bgColor = None
        # SGR reset does not affect OSC 8 hyperlink state

    def clear(self) -> None:
        """Clear all state for reuse."""
        self.reset()
        self.activeHyperlink = None

    def getActiveCodes(self) -> str:
        codes: list[str] = []
        if self.bold:
            codes.append("1")
        if self.dim:
            codes.append("2")
        if self.italic:
            codes.append("3")
        if self.underline:
            codes.append("4")
        if self.blink:
            codes.append("5")
        if self.inverse:
            codes.append("7")
        if self.hidden:
            codes.append("8")
        if self.strikethrough:
            codes.append("9")
        if self.fgColor:
            codes.append(self.fgColor)
        if self.bgColor:
            codes.append(self.bgColor)

        result = f"\x1b[{';'.join(codes)}m" if len(codes) > 0 else ""
        if self.activeHyperlink:
            result += formatOsc8Hyperlink(self.activeHyperlink)
        return result

    def hasActiveCodes(self) -> bool:
        return (
            self.bold
            or self.dim
            or self.italic
            or self.underline
            or self.blink
            or self.inverse
            or self.hidden
            or self.strikethrough
            or self.fgColor is not None
            or self.bgColor is not None
            or self.activeHyperlink is not None
        )

    def getLineEndReset(self) -> str:
        """Get reset codes for attributes that need to be turned off at line end.
        Underline must be closed to prevent bleeding into padding.
        Active OSC 8 hyperlinks must be closed and re-opened on the next line.
        Returns empty string if no attributes need closing.
        """
        result = ""
        if self.underline:
            result += "\x1b[24m"  # Underline off only
        if self.activeHyperlink:
            result += formatOsc8Close(self.activeHyperlink.terminator)  # Re-opened at line start via getActiveCodes()
        return result


class _SgrRecorder:
    """MISAKA fork: records the changes upstream's SGR switch makes to a tracker."""

    operations: list[tuple[str, Any]]

    def __init__(self) -> None:
        object.__setattr__(self, "operations", [])

    def __setattr__(self, name: str, value: Any) -> None:
        self.operations.append((name, value))

    def reset(self) -> None:
        self.operations.append(("reset", None))


@functools.lru_cache(maxsize=4096)
def _sgrOperations(ansiCode: str) -> tuple[tuple[str, Any], ...]:
    """MISAKA fork: what `AnsiCodeTracker.process` changes for an SGR code, in order."""
    recorder = _SgrRecorder()
    _parseSgr(recorder, ansiCode)
    return tuple(recorder.operations)


def _parseSgr(self: Any, ansiCode: str) -> None:
    """Upstream's `process` from the parameter extraction on, unchanged (`self` is a `_SgrRecorder`)."""
    # Extract the parameters between \x1b[ and m
    match = _SGR_PARAMS.exec(ansiCode)
    if not match:
        return

    params = match[1]
    if params == "" or params == "0":
        # Full reset
        self.reset()
        return

    # Parse parameters (can be semicolon-separated)
    parts = params.split(";")
    i = 0
    while i < len(parts):
        code = _parseInt(parts[i])

        # Handle 256-color and RGB codes which consume multiple parameters
        if code == 38 or code == 48:
            # 38;5;N (256 color fg) or 38;2;R;G;B (RGB fg)
            # 48;5;N (256 color bg) or 48;2;R;G;B (RGB bg)
            if _part(parts, i + 1) == "5" and _part(parts, i + 2) is not None:
                # 256 color: 38;5;N or 48;5;N
                colorCode = f"{parts[i]};{parts[i + 1]};{parts[i + 2]}"
                if code == 38:
                    self.fgColor = colorCode
                else:
                    self.bgColor = colorCode
                i += 3
                continue
            elif _part(parts, i + 1) == "2" and _part(parts, i + 4) is not None:
                # RGB color: 38;2;R;G;B or 48;2;R;G;B
                colorCode = f"{parts[i]};{parts[i + 1]};{parts[i + 2]};{parts[i + 3]};{parts[i + 4]}"
                if code == 38:
                    self.fgColor = colorCode
                else:
                    self.bgColor = colorCode
                i += 5
                continue

        # Standard SGR codes
        if code == 0:
            self.reset()
        elif code == 1:
            self.bold = True
        elif code == 2:
            self.dim = True
        elif code == 3:
            self.italic = True
        elif code == 4:
            self.underline = True
        elif code == 5:
            self.blink = True
        elif code == 7:
            self.inverse = True
        elif code == 8:
            self.hidden = True
        elif code == 9:
            self.strikethrough = True
        elif code == 21:
            self.bold = False  # Some terminals
        elif code == 22:
            self.bold = False
            self.dim = False
        elif code == 23:
            self.italic = False
        elif code == 24:
            self.underline = False
        elif code == 25:
            self.blink = False
        elif code == 27:
            self.inverse = False
        elif code == 28:
            self.hidden = False
        elif code == 29:
            self.strikethrough = False
        elif code == 39:
            self.fgColor = None  # Default fg
        elif code == 49:
            self.bgColor = None  # Default bg
        elif code is not None:
            # Standard foreground colors 30-37, 90-97
            if (code >= 30 and code <= 37) or (code >= 90 and code <= 97):
                self.fgColor = str(code)
            # Standard background colors 40-47, 100-107
            elif (code >= 40 and code <= 47) or (code >= 100 and code <= 107):
                self.bgColor = str(code)
        i += 1


def _part(parts: list[str], index: int) -> str | None:
    """``parts[index]``: ``undefined`` past the end."""
    return parts[index] if index < len(parts) else None


def _parseInt(text: str) -> int | None:
    """``Number.parseInt(text, 10)``: the leading decimal integer, or ``None`` (NaN) when there is none."""
    digits = ""
    for char in text:
        if not char.isascii() or not char.isdigit():
            break
        digits += char
    return int(digits) if digits else None


def updateTrackerFromText(text: str, tracker: AnsiCodeTracker) -> None:
    # MISAKA fork: one regex pass for upstream's indexOf/ansiCodeLength loop (the same sequences, in order).
    if "\x1b" not in text:
        return
    for match in _ESCAPE_REGEX.finditer(text):
        tracker.process(match.group())


def graphemeSegments(text: str) -> Iterator[str]:
    for data in graphemeSegmenter.segment(text):
        yield data.segment


# MISAKA fork: the wrap path below is run-based. Upstream measures and places a line token by token and grapheme by
# grapheme, which V8 makes cheap and CPython does not (re-wrapping a long transcript for a new width took several
# seconds). Here a line is cut into the tokens upstream's splitIntoTokensWithAnsi makes (runs of spaces, runs of other
# text, one CJK character each) with their widths known at once, and a run of consecutive CJK characters is kept as one
# unit that is broken between any two characters, as upstream breaks between any two of its CJK tokens. The results are
# upstream's (checked against upstream's own functions run under Node).
_CJK_CLASS = (
    r"\p{Script_Extensions=Han}\p{Script_Extensions=Hiragana}\p{Script_Extensions=Katakana}"
    r"\p{Script_Extensions=Hangul}\p{Script_Extensions=Bopomofo}"
)
_UNIT_REGEX = regex.compile(f"([{_CJK_CLASS}]+)|( +)|([^ {_CJK_CLASS}]+)")

# (token, width, cjk): the width is -1 for a token measured later; cjk keeps a run of CJK tokens together as
# (codes before, characters, their widths, codes after).
type _Unit = tuple[str, int, tuple[str, str, str, str] | None]


def _textRunEnd(text: str, index: int) -> int:
    """Where the visible text starting at ``index`` ends: at the next escape sequence, or at the end. An ESC that
    starts no sequence is text."""
    match = _ESCAPE_REGEX.search(text, index + 1)
    return match.start() if match is not None else len(text)


def splitIntoTokensWithAnsi(text: str) -> list[_Unit]:
    """Split text into words while keeping ANSI codes attached.

    MISAKA fork: tokens carry their widths, and a run of CJK tokens is one unit (see above)."""
    units: list[_Unit] = []
    current = ""
    currentWidth = 0
    currentKind: str | None = None
    pendingAnsi = ""  # ANSI codes waiting to be attached to next visible content
    index = 0
    length = len(text)
    while index < length:
        if text[index] == "\x1b":
            match = _ESCAPE_REGEX.match(text, index)
            if match is not None:
                # Hold ANSI codes separately - they'll be attached to the next visible char
                pendingAnsi += match.group()
                index = match.end()
                continue
        # Visible text runs up to the next escape sequence.
        end = _textRunEnd(text, index)
        run = text[index:end]
        index = end
        if isPrintableAscii(run):
            widths = None  # every character one column wide
        else:
            widths = _widths(run)
            if widths is None:  # characters that may form clusters: upstream's grapheme loop, measured later
                for segment in graphemeSegments(run):
                    if segment != " " and cjkBreakRegex.test(segment):
                        if current:
                            units.append((current, currentWidth, None))
                            current, currentWidth, currentKind = "", 0, None
                        units.append((pendingAnsi + segment, -1, None))
                        pendingAnsi = ""
                        continue
                    kind = "space" if segment == " " else "word"
                    if current and currentKind != kind:
                        units.append((current, currentWidth, None))
                        current = ""
                    if pendingAnsi:
                        current += pendingAnsi
                        pendingAnsi = ""
                    currentKind = kind
                    current += segment
                    currentWidth = -1
                continue
        offset = 0
        for cjk, space, word in _UNIT_REGEX.findall(run):
            if cjk:
                if current:
                    units.append((current, currentWidth, None))
                    current, currentWidth, currentKind = "", 0, None
                cjkWidths = "1" * len(cjk) if widths is None else widths[offset : offset + len(cjk)]
                units.append((pendingAnsi + cjk, len(cjk) + cjkWidths.count("2"), (pendingAnsi, cjk, cjkWidths, "")))
                pendingAnsi = ""
                offset += len(cjk)
                continue
            token = space or word
            kind = "space" if space else "word"
            width = len(token) if widths is None else len(token) + widths.count("2", offset, offset + len(token))
            offset += len(token)
            if current and currentKind != kind:
                units.append((current, currentWidth, None))
                current, currentWidth = "", 0
            # Attach any pending ANSI codes to this visible character
            if pendingAnsi:
                current += pendingAnsi
                pendingAnsi = ""
            currentKind = kind
            current += token
            if currentWidth >= 0:
                currentWidth += width

    # Handle any remaining pending ANSI codes (attach to last token)
    if pendingAnsi:
        if current:
            current += pendingAnsi
        elif units:
            token, width, cjkRun = units[-1]
            units[-1] = (
                token + pendingAnsi,
                width,
                cjkRun and (cjkRun[0], cjkRun[1], cjkRun[2], cjkRun[3] + pendingAnsi),
            )
        else:
            current = pendingAnsi
    if current:
        units.append((current, currentWidth, None))
    return units


def _singleCharacters(unit: _Unit) -> Iterator[_Unit]:
    """A CJK run as the tokens upstream sees, one character each (for widths too narrow for the run loop)."""
    token, width, cjk = unit
    if cjk is None:
        yield token, width, None
        return
    codesBefore, characters, widths, codesAfter = cjk
    last = len(characters) - 1
    for position, character in enumerate(characters):
        piece = (codesBefore if position == 0 else "") + character + (codesAfter if position == last else "")
        yield piece, int(widths[position]), None


def _fitting(widths: str, position: int, count: int, room: int) -> int:
    """How many of the characters from ``position`` on fit into ``room`` columns."""
    if room <= 0:
        return 0
    span = count - position
    wide = widths.count("2", position, count)
    if wide == 0:
        return min(span, room)
    if wide == span:
        return min(span, room // 2)
    low, high = 0, min(span, room)
    while low < high:
        middle = (low + high + 1) // 2
        if middle + widths.count("2", position, position + middle) <= room:
            low = middle
        else:
            high = middle - 1
    return low


def flattenLines(lines: list[str]) -> None:
    """Flatten cached lines. V8 keeps a string built by concatenation as a tree of its parts until something reads it
    whole, and a cached line kept as such a tree retains several times its own size. Converting a string to a number
    reads it whole, so V8 flattens it in place; the strings' values do not change.

    MISAKA fork: a CPython string is always flat, so this does nothing; it keeps the call sites as upstream's.
    """


_LINE_BREAK = RegExp(r"\r\n|\r|\n")


def wrapTextWithAnsi(text: str, width: int) -> list[str]:
    """Wrap text with ANSI codes preserved.

    ONLY does word wrapping - NO padding, NO background colors.
    Returns lines where each line is <= width visible chars.
    Active ANSI codes are preserved across line breaks.

    :param text: Text to wrap (may contain ANSI codes and newlines)
    :param width: Maximum visible width per line
    :returns: Array of wrapped lines (NOT padded to width)
    """
    if not text:
        return [""]

    # Handle newlines by processing each line separately
    # Track ANSI state across lines so styles carry over after literal newlines
    inputLines = js_regexp.split(text, _LINE_BREAK)
    result: list[str] = []
    tracker = AnsiCodeTracker()

    for inputLine in inputLines:
        # Prepend active ANSI codes from previous lines (except for first line)
        prefix = tracker.getActiveCodes() if len(result) > 0 else ""
        wrappedLines = wrapSingleLine(prefix + inputLine, width)
        for wrappedLine in wrappedLines:
            result.append(wrappedLine)  # noqa: PERF402 - upstream's loop
        # Update tracker with codes from this line for next iteration
        updateTrackerFromText(inputLine, tracker)

    return result if len(result) > 0 else [""]


def wrapSingleLine(line: str, width: int) -> list[str]:
    # MISAKA fork: run-based (see `splitIntoTokensWithAnsi`); the tracker's active codes and line-end reset are kept
    # and recomputed only when a token carries codes.
    if not line:
        return [""]

    visibleLength = visibleWidth(line)
    if visibleLength <= width:
        return [line]

    wrapped: list[str] = []
    tracker = AnsiCodeTracker()
    active = ""  # tracker.getActiveCodes() for the tokens placed so far ...
    reset = ""  # ... and getLineEndReset(): both change only with a token that carries codes
    tokens = splitIntoTokensWithAnsi(line)
    if width < 2:  # a CJK character may be wider than the line: upstream's long-word path, one character each
        tokens = [piece for unit in tokens for piece in _singleCharacters(unit)]

    currentLine = ""
    currentVisibleLength = 0

    for token, tokenVisibleLength, cjk in tokens:
        if tokenVisibleLength < 0:
            tokenVisibleLength = visibleWidth(token)
        if cjk is not None and currentVisibleLength + tokenVisibleLength > width:
            # Upstream sees one token per character and breaks wherever the next no longer fits; the run is placed
            # by the same rule, as many characters as fill the line at once.
            codesBefore, characters, widths, codesAfter = cjk
            position = 0
            count = len(characters)
            while position < count:
                fitting = _fitting(widths, position, count, width - currentVisibleLength)
                if fitting == 0:  # the line holds something already: end it
                    wrapped.append(jsTrimEnd(currentLine) + reset)
                    currentLine = active
                    currentVisibleLength = 0
                    continue
                piece = characters[position : position + fitting]
                if position == 0:
                    piece = codesBefore + piece
                position += fitting
                if position == count:
                    piece += codesAfter
                currentLine += piece
                currentVisibleLength += fitting + widths.count("2", position - fitting, position)
                if position == fitting and codesBefore:  # placed with its first character: its codes apply from here
                    updateTrackerFromText(codesBefore, tracker)
                    active = tracker.getActiveCodes()
                    reset = tracker.getLineEndReset()
            if codesAfter:
                updateTrackerFromText(codesAfter, tracker)
                active = tracker.getActiveCodes()
                reset = tracker.getLineEndReset()
            continue

        isWhitespace = jsTrim(token) == ""

        # Token itself is too long - break it character by character
        if tokenVisibleLength > width and not isWhitespace:
            if currentLine:
                # Add specific reset for underline only (preserves background)
                wrapped.append(currentLine + reset)
                currentLine = ""
                currentVisibleLength = 0

            # Break long token - breakLongWord handles its own resets
            broken = breakLongWord(token, width, tracker)
            wrapped.extend(broken[:-1])
            currentLine = broken[-1]
            currentVisibleLength = visibleWidth(currentLine)
            if "\x1b" in token:  # breakLongWord fed the tracker
                active = tracker.getActiveCodes()
                reset = tracker.getLineEndReset()
            continue

        # Check if adding this token would exceed width
        if currentVisibleLength + tokenVisibleLength > width and currentVisibleLength > 0:
            # Trim trailing whitespace, then add underline reset (not full reset, to preserve background)
            wrapped.append(jsTrimEnd(currentLine) + reset)
            if isWhitespace:
                # Don't start new line with whitespace
                currentLine = active
                currentVisibleLength = 0
            else:
                currentLine = active + token
                currentVisibleLength = tokenVisibleLength
        else:
            # Add to current line
            currentLine += token
            currentVisibleLength += tokenVisibleLength
        if "\x1b" in token:
            updateTrackerFromText(token, tracker)
            active = tracker.getActiveCodes()
            reset = tracker.getLineEndReset()

    if currentLine:
        # No reset at end of final line - let caller handle it
        wrapped.append(currentLine)

    # Trailing whitespace can cause lines to exceed the requested width
    return [jsTrimEnd(item) for item in wrapped] if len(wrapped) > 0 else [""]


PUNCTUATION_REGEX = RegExp(r"""[(){}[\]<>.,;:'"!?+\-=*/\\|&%^$#@~`]""")
_WHITESPACE_REGEX = RegExp(r"\s")


def isWhitespaceChar(char: str) -> bool:
    """Check if a character is whitespace."""
    return _WHITESPACE_REGEX.test(char)


def isPunctuationChar(char: str) -> bool:
    """Check if a character is punctuation."""
    return PUNCTUATION_REGEX.test(char)


def breakLongWord(word: str, width: int, tracker: AnsiCodeTracker) -> list[str]:
    # MISAKA fork: runs measured through the character table where they can be (see `_WIDTHS`).
    lines: list[str] = []
    currentLine = tracker.getActiveCodes()
    currentWidth = 0
    index = 0
    length = len(word)
    while index < length:
        if word[index] == "\x1b":
            match = _ESCAPE_REGEX.match(word, index)
            if match is not None:
                currentLine += match.group()
                tracker.process(match.group())
                index = match.end()
                continue
        end = _textRunEnd(word, index)
        run = word[index:end]
        index = end
        widths = _widths(run)
        pieces: Iterable[tuple[str, int]]
        if widths is None:
            pieces = [(segment, visibleWidth(segment)) for segment in graphemeSegments(run)]
        else:
            pieces = zip(run, map(int, widths), strict=True)
        for grapheme, graphemeWidth_ in pieces:
            # Skip empty graphemes to avoid issues with string-width calculation
            if not grapheme:
                continue
            if currentWidth + graphemeWidth_ > width:
                # Add specific reset for underline only (preserves background)
                lineEndReset = tracker.getLineEndReset()
                if lineEndReset:
                    currentLine += lineEndReset
                lines.append(currentLine)
                currentLine = tracker.getActiveCodes()
                currentWidth = 0
            currentLine += grapheme
            currentWidth += graphemeWidth_

    if currentLine:
        # No reset at end of final segment - caller handles continuation
        lines.append(currentLine)

    return lines if len(lines) > 0 else [""]


def applyBackgroundToLine(line: str, width: int, bgFn: Callable[[str], str]) -> str:
    """Apply background color to a line, padding to full width.

    :param line: Line of text (may contain ANSI codes)
    :param width: Total width to pad to
    :param bgFn: Background color function
    :returns: Line with background applied and padded to width
    """
    # Calculate padding needed
    visibleLen = visibleWidth(line)
    paddingNeeded = max(0, width - visibleLen)
    padding = " " * paddingNeeded

    # Apply background to content + padding
    withPadding = line + padding
    return bgFn(withPadding)


def truncateToWidth(text: str, maxWidth: int, ellipsis: str = "...", pad: bool = False) -> str:
    """Truncate text to fit within a maximum visible width, adding ellipsis if needed.
    Optionally pad with spaces to reach exactly maxWidth.
    Properly handles ANSI escape codes (they don't count toward width).

    :param text: Text to truncate (may contain ANSI codes)
    :param maxWidth: Maximum visible width
    :param ellipsis: Ellipsis string to append when truncating (default: "...")
    :param pad: If true, pad result with spaces to exactly maxWidth (default: false)
    :returns: Truncated text, optionally padded to exactly maxWidth
    """
    if maxWidth <= 0:
        return ""

    if len(text) == 0:
        return " " * maxWidth if pad else ""

    ellipsisWidth = visibleWidth(ellipsis)
    if ellipsisWidth >= maxWidth:
        textWidth = visibleWidth(text)
        if textWidth <= maxWidth:
            return text + " " * (maxWidth - textWidth) if pad else text

        clippedEllipsis = truncateFragmentToWidth(ellipsis, maxWidth)
        if clippedEllipsis.width == 0:
            return " " * maxWidth if pad else ""
        return finalizeTruncatedResult("", 0, clippedEllipsis.text, clippedEllipsis.width, maxWidth, pad)

    if isPrintableAscii(text):
        if len(text) <= maxWidth:
            return text + " " * (maxWidth - len(text)) if pad else text
        targetWidth = maxWidth - ellipsisWidth
        return finalizeTruncatedResult(text[:targetWidth], targetWidth, ellipsis, ellipsisWidth, maxWidth, pad)

    targetWidth = maxWidth - ellipsisWidth
    result = ""
    pendingAnsi = ""
    visibleSoFar = 0
    keptWidth = 0
    keepContiguousPrefix = True
    overflowed = False
    exhaustedInput = False
    hasAnsi = "\x1b" in text
    hasTabs = "\t" in text

    if not hasAnsi and not hasTabs:
        for data in graphemeSegmenter.segment(text):
            segment = data.segment
            width = graphemeWidth(segment)
            if keepContiguousPrefix and keptWidth + width <= targetWidth:
                result += segment
                keptWidth += width
            else:
                keepContiguousPrefix = False
            visibleSoFar += width
            if visibleSoFar > maxWidth:
                overflowed = True
                break
        exhaustedInput = not overflowed
    else:
        i = 0
        while i < len(text):
            ansi = extractAnsiCode(text, i)
            if ansi:
                pendingAnsi += ansi.code
                i += ansi.length
                continue

            if text[i] == "\t":
                if keepContiguousPrefix and keptWidth + 3 <= targetWidth:
                    if pendingAnsi:
                        result += pendingAnsi
                        pendingAnsi = ""
                    result += "\t"
                    keptWidth += 3
                else:
                    keepContiguousPrefix = False
                    pendingAnsi = ""
                visibleSoFar += 3
                if visibleSoFar > maxWidth:
                    overflowed = True
                    break
                i += 1
                continue

            end = i
            while end < len(text) and text[end] != "\t":
                nextAnsi = extractAnsiCode(text, end)
                if nextAnsi:
                    break
                end += 1

            for data in graphemeSegmenter.segment(text[i:end]):
                segment = data.segment
                width = graphemeWidth(segment)
                if keepContiguousPrefix and keptWidth + width <= targetWidth:
                    if pendingAnsi:
                        result += pendingAnsi
                        pendingAnsi = ""
                    result += segment
                    keptWidth += width
                else:
                    keepContiguousPrefix = False
                    pendingAnsi = ""

                visibleSoFar += width
                if visibleSoFar > maxWidth:
                    overflowed = True
                    break
            if overflowed:
                break
            i = end
        exhaustedInput = i >= len(text)

    if not overflowed and exhaustedInput:
        return text + " " * max(0, maxWidth - visibleSoFar) if pad else text

    return finalizeTruncatedResult(result, keptWidth, ellipsis, ellipsisWidth, maxWidth, pad)


def sliceByColumn(line: str, startCol: int, length: int, strict: bool = False) -> str:
    """Extract a range of visible columns from a line. Handles ANSI codes and wide chars.

    :param strict: If true, exclude wide chars at boundary that would extend past the range
    """
    return sliceWithWidth(line, startCol, length, strict).text


def sliceWithWidth(line: str, startCol: int, length: int, strict: bool = False) -> SliceResult:
    """Like sliceByColumn but also returns the actual visible width of the result."""
    if length <= 0:
        return SliceResult(text="", width=0)
    endCol = startCol + length
    result = ""
    resultWidth = 0
    currentCol = 0
    i = 0
    pendingAnsi = ""

    while i < len(line):
        ansi = extractAnsiCode(line, i)
        if ansi:
            if currentCol >= startCol and currentCol < endCol:
                # Keep original order: codes from before the range must precede codes at the boundary
                result += pendingAnsi + ansi.code
                pendingAnsi = ""
            elif currentCol < startCol:
                pendingAnsi += ansi.code
            i += ansi.length
            continue

        textEnd = i
        while textEnd < len(line) and not extractAnsiCode(line, textEnd):
            textEnd += 1

        for data in graphemeSegmenter.segment(line[i:textEnd]):
            segment = data.segment
            w = graphemeWidth(segment)
            inRange = currentCol >= startCol and currentCol < endCol
            fits = not strict or currentCol + w <= endCol
            if inRange and fits:
                if pendingAnsi:
                    result += pendingAnsi
                    pendingAnsi = ""
                result += segment
                resultWidth += w
            currentCol += w
            if currentCol >= endCol:
                break
        i = textEnd
        if currentCol >= endCol:
            break
    return SliceResult(text=result, width=resultWidth)


# Pooled tracker instance for extractSegments (avoids allocation per call)
pooledStyleTracker = AnsiCodeTracker()


@dataclass(slots=True)
class ExtractedSegments:
    """``{ before, beforeWidth, after, afterWidth }``."""

    before: str
    beforeWidth: int
    after: str
    afterWidth: int


def extractSegments(
    line: str,
    beforeEnd: int,
    afterStart: int,
    afterLen: int,
    strictAfter: bool = False,
) -> ExtractedSegments:
    """Extract "before" and "after" segments from a line in a single pass.
    Used for overlay compositing where we need content before and after the overlay region.
    Preserves styling from before the overlay that should affect content after it.
    """
    before = ""
    beforeWidth = 0
    after = ""
    afterWidth = 0
    currentCol = 0
    i = 0
    pendingAnsiBefore = ""
    afterStarted = False
    afterEnd = afterStart + afterLen

    # Track styling state so "after" inherits styling from before the overlay
    pooledStyleTracker.clear()

    while i < len(line):
        ansi = extractAnsiCode(line, i)
        if ansi:
            # Track all SGR codes to know styling state at afterStart
            pooledStyleTracker.process(ansi.code)
            # Include ANSI codes in their respective segments
            if currentCol < beforeEnd:
                pendingAnsiBefore += ansi.code
            elif (
                currentCol >= afterStart
                and currentCol < afterEnd
                and (afterStarted or ansi.code.startswith("\x1b_"))
            ):
                # SGR codes are only included after "after" started (styling already prepended). Zero-width APC
                # markers like CURSOR_MARKER are not tracked styling, so keep them at the boundary too.
                after += ansi.code
            i += ansi.length
            continue

        textEnd = i
        while textEnd < len(line) and not extractAnsiCode(line, textEnd):
            textEnd += 1

        for data in graphemeSegmenter.segment(line[i:textEnd]):
            segment = data.segment
            w = graphemeWidth(segment)

            if currentCol < beforeEnd and currentCol + w <= beforeEnd:
                if pendingAnsiBefore:
                    before += pendingAnsiBefore
                    pendingAnsiBefore = ""
                before += segment
                beforeWidth += w
            elif currentCol >= afterStart and currentCol < afterEnd:
                fits = not strictAfter or currentCol + w <= afterEnd
                if fits:
                    # On first "after" grapheme, prepend inherited styling from before overlay
                    if not afterStarted:
                        after += pooledStyleTracker.getActiveCodes()
                        afterStarted = True
                    after += segment
                    afterWidth += w

            currentCol += w
            # Early exit: done with "before" only, or done with both segments
            if (currentCol >= beforeEnd) if afterLen <= 0 else (currentCol >= afterEnd):
                break
        i = textEnd
        if (currentCol >= beforeEnd) if afterLen <= 0 else (currentCol >= afterEnd):
            break

    return ExtractedSegments(before=before, beforeWidth=beforeWidth, after=after, afterWidth=afterWidth)


__all__ = [
    "PUNCTUATION_REGEX",
    "AnsiMatch",
    "ExtractedSegments",
    "SegmentData",
    "Segmenter",
    "SliceResult",
    "applyBackgroundToLine",
    "autocompleteBoundaryRegex",
    "autocompleteSeparatorRegex",
    "cjkBreakRegex",
    "cjkPunctuationRegex",
    "extractAnsiCode",
    "extractSegments",
    "flattenLines",
    "getGraphemeSegmenter",
    "getOsc8LinkAtColumn",
    "getWordSegmenter",
    "isPunctuationChar",
    "isWhitespaceChar",
    "normalizeTerminalOutput",
    "sliceByColumn",
    "sliceWithWidth",
    "stripTerminalSequences",
    "truncateToWidth",
    "visibleWidth",
    "wrapTextWithAnsi",
]
