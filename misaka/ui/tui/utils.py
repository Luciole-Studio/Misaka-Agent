"""ANSI-aware width, wrapping, truncation, and slicing helpers."""

from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache

from wcwidth import wcswidth

_THAI_LAO_AM_RE = re.compile(r"[\u0e33\u0eb3]")
_PUNCTUATION_RE = re.compile(r"""[(){}\[\]<>.,;:'"!?+\-=*/\\|&%^$#@~`]""")
_WIDTH_CACHE_SIZE = 16384         # a long transcript re-measures thousands of distinct lines per render
_width_cache: OrderedDict[str, int] = OrderedDict()
# PORT-NOTE: pi measures width grapheme by grapheme (Intl.Segmenter); V8 makes that cheap, CPython
# does not -- one full render of a 2000-row transcript spent most of its time here, and wrapping
# a 2 MB transcript for a new width took 1.5 s. A character that starts no cluster (no combining
# mark, format char, ZWJ, variation selector, modifier or regional indicator follows the rules
# below) has the width wcwidth gives it, so a string made only of such characters is measured by
# one C-level translate through this table (code point -> "1" or "2"); anything else takes pi's
# path. The digits themselves are in the table from the start so that a "2" left untranslated
# can never be mistaken for a width.
_WIDTHS: dict[int, str] = {ord("1"): "1", ord("2"): "1"}
_CLUSTERING: set[str] = set()     # characters that may join a cluster, or are zero-width: pi's path


@dataclass(slots=True)
class SegmentData:
    segment: str
    index: int
    input: str


class GraphemeSegmenter:
    def segment(self, text: str) -> list[SegmentData]:
        segments: list[SegmentData] = []
        index = 0
        for grapheme in _iter_graphemes(text):
            segments.append(SegmentData(segment=grapheme, index=index, input=text))
            index += len(grapheme)
        return segments


_SEGMENTER = GraphemeSegmenter()


def get_segmenter() -> GraphemeSegmenter:
    return _SEGMENTER


def _is_printable_ascii(text: str) -> bool:
    return all(0x20 <= ord(char) <= 0x7E for char in text)


def _is_variation_selector(codepoint: int) -> bool:
    return 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF


def _is_emoji_modifier(codepoint: int) -> bool:
    return 0x1F3FB <= codepoint <= 0x1F3FF


def _is_regional_indicator(codepoint: int) -> bool:
    return 0x1F1E6 <= codepoint <= 0x1F1FF


def _is_extend_char(char: str) -> bool:
    codepoint = ord(char)
    if codepoint == 0x200D:
        return False  # ZWJ is handled by the join branch in _iter_graphemes; treating it as extend would split family emoji into three clusters
    return (
        unicodedata.combining(char) != 0
        # Mc (spacing combining marks, e.g. Devanagari vowel signs) and Hangul V/T jamo attach to the
        # preceding cluster; approximates Intl.Segmenter's UAX#29
        or unicodedata.category(char) in {"Cf", "Mn", "Me", "Mc"}
        or 0x1160 <= codepoint <= 0x11FF
        or _is_variation_selector(codepoint)
        or _is_emoji_modifier(codepoint)
    )


def _iter_graphemes(text: str) -> Iterator[str]:
    index = 0
    length = len(text)
    while index < length:
        start = index
        index += 1
        first_codepoint = ord(text[start])

        if _is_regional_indicator(first_codepoint):
            if index < length and _is_regional_indicator(ord(text[index])):
                index += 1
            yield text[start:index]
            continue

        while index < length and _is_extend_char(text[index]):
            index += 1

        while index < length and text[index] == "\u200d":
            index += 1
            if index >= length:
                break
            index += 1
            while index < length and _is_extend_char(text[index]):
                index += 1

        yield text[start:index]


@lru_cache(maxsize=65536)
def _grapheme_width(segment: str) -> int:
    if not segment:
        return 0
    if segment == "\t":
        return 3
    stripped = "".join(
        char
        for char in segment
        if unicodedata.category(char) not in {"Cc", "Cf", "Cs"} and not unicodedata.combining(char)
    )
    if stripped == "":
        return 0

    codepoint = ord(stripped[0])
    if _is_regional_indicator(codepoint) and len(stripped) == 1:
        return 2

    width = wcswidth(segment)
    return max(width, 0)


def _simple_width(char: str) -> int:
    """wcwidth of a character that can never join a cluster; -1 when pi's grapheme rules apply."""
    codepoint = ord(char)
    if (char == "\t" or codepoint == 0x200D or _is_regional_indicator(codepoint) or _is_extend_char(char)
            or 0xD800 <= codepoint <= 0xDFFF):
        return -1
    width = wcswidth(char)
    return width if width >= 0 else -1


def _widths(text: str) -> str | None:
    """``text`` with every character replaced by its column width, "1" or "2"; None when a
    character may join a grapheme cluster (or is zero-width), where pi's grapheme rules
    decide. A character seen for the first time is classified once."""
    widths = text.translate(_WIDTHS)
    if widths.count("1") + widths.count("2") == len(widths):
        return widths
    for char in set(text):
        if ord(char) in _WIDTHS:
            continue
        if char in _CLUSTERING:
            return None
        width = _simple_width(char)
        if width < 1:
            _CLUSTERING.add(char)
            return None
        _WIDTHS[ord(char)] = "2" if width == 2 else "1"
    return text.translate(_WIDTHS)


def visible_width(text: str) -> int:
    if len(text) == 0:
        return 0
    if text.isascii() and text.isprintable():
        return len(text)

    cached = _width_cache.get(text)
    if cached is not None:
        return cached

    clean = text.replace("\t", "   ") if "\t" in text else text
    if "\x1b" in clean:
        clean = _ESCAPE_RE.sub("", clean)       # what is left of an ESC is text, as in pi's scan

    widths = _widths(clean)
    if widths is not None:
        width = len(widths) + widths.count("2")
    else:
        width = sum(_grapheme_width(segment) for segment in _iter_graphemes(clean))
    if len(_width_cache) >= _WIDTH_CACHE_SIZE:
        _width_cache.popitem(last=False)
    _width_cache[text] = width
    return width


def normalize_terminal_output(text: str) -> str:
    normalized = text
    if _THAI_LAO_AM_RE.search(normalized):
        normalized = _THAI_LAO_AM_RE.sub(
            lambda match: "\u0e4d\u0e32" if match.group(0) == "\u0e33" else "\u0ecd\u0eb2",
            normalized,
        )
    if "\t" not in normalized:
        return normalized
    result: list[str] = []
    index = 0
    while index < len(normalized):
        ansi = extract_ansi_code(normalized, index)
        if ansi is not None:
            result.append(ansi.code)
            index += ansi.length
            continue
        result.append("   " if normalized[index] == "\t" else normalized[index])
        index += 1
    return "".join(result)


@dataclass(slots=True)
class AnsiMatch:
    code: str
    length: int


# ECMA-48 CSI: parameter bytes 0x30-0x3F, then intermediate bytes 0x20-0x2F, then one final
# byte 0x40-0x7E. Pi (utils.ts:406-420) instead scans forward for `[mGKHJ]`, which makes any
# other CSI — `\x1b[10A`, `\x1b[6n`, `\x1b[?25l` — swallow every character up to the next `m`.
# `TUI.applyLineResets` appends `\x1b[0m` to every line, so that `m` always exists, and
# `visible_width` then counts the swallowed text as zero: lines get wrapped and truncated
# against a width that is far too small, the terminal soft-wraps them, and the over-wide-line
# guard in `_doRenderInner` never fires. Match the real grammar instead, and report "not an
# escape" for anything that does not terminate. OSC and APC strings run to BEL or ESC \ (a
# lone ESC inside them is part of the string, as in pi's scan).
_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]|[\]_](?:[^\x07\x1b]|\x1b(?!\\))*(?:\x07|\x1b\\))"
)


def extract_ansi_code(text: str, pos: int) -> AnsiMatch | None:
    match = _ESCAPE_RE.match(text, pos)
    if match is None:
        return None
    return AnsiMatch(code=match.group(), length=match.end() - pos)


type Osc8Terminator = str


@dataclass(slots=True)
class ActiveHyperlink:
    params: str
    url: str
    terminator: Osc8Terminator


def _parse_osc8_hyperlink(ansi_code: str) -> ActiveHyperlink | None | object:
    if not ansi_code.startswith("\x1b]8;"):
        return NotImplemented
    terminator = "\x07" if ansi_code.endswith("\x07") else "\x1b\\"
    body = ansi_code[4:-1] if terminator == "\x07" else ansi_code[4:-2]
    separator_index = body.find(";")
    if separator_index == -1:
        return NotImplemented
    params = body[:separator_index]
    url = body[separator_index + 1 :]
    if not url:
        return None
    return ActiveHyperlink(params=params, url=url, terminator=terminator)


def _format_osc8_hyperlink(hyperlink: ActiveHyperlink) -> str:
    return f"\x1b]8;{hyperlink.params};{hyperlink.url}{hyperlink.terminator}"


def _format_osc8_close(terminator: Osc8Terminator) -> str:
    return f"\x1b]8;;{terminator}"


_SGR_RE = re.compile(r"\x1b\[([\d;]*)m")
_SGR_FLAGS = {
    1: ("bold", True), 2: ("dim", True), 3: ("italic", True), 4: ("underline", True),
    5: ("blink", True), 7: ("inverse", True), 8: ("hidden", True), 9: ("strikethrough", True),
    21: ("bold", False), 23: ("italic", False), 24: ("underline", False), 25: ("blink", False),
    27: ("inverse", False), 28: ("hidden", False), 29: ("strikethrough", False),
    39: ("fgColor", None), 49: ("bgColor", None),
}
_SGR_ACTIONS: dict[str, tuple[tuple[str, object], ...]] = {}   # sequence -> what it does to a tracker


def _sgr_actions(ansi_code: str) -> tuple[tuple[str, object], ...]:
    """What an SGR sequence does to the tracker's fields, in order: (field, value) pairs, with
    ("reset", None) for SGR 0. A sequence recurs on every line of a transcript, so it is parsed
    once (pi parses per call)."""
    match = _SGR_RE.match(ansi_code)
    if match is None:
        return ()
    params = match.group(1)
    if params in {"", "0"}:
        return (("reset", None),)
    actions: list[tuple[str, object]] = []
    parts = params.split(";")
    index = 0
    while index < len(parts):
        try:
            code = int(parts[index])
        except ValueError:
            index += 1
            continue
        if code in {38, 48}:
            field = "fgColor" if code == 38 else "bgColor"
            if index + 2 < len(parts) and parts[index + 1] == "5":
                actions.append((field, ";".join(parts[index : index + 3])))
                index += 3
                continue
            if index + 4 < len(parts) and parts[index + 1] == "2":
                actions.append((field, ";".join(parts[index : index + 5])))
                index += 5
                continue
        if code == 0:
            actions.append(("reset", None))
        elif code == 22:
            actions.append(("bold", False))
            actions.append(("dim", False))
        elif code in _SGR_FLAGS:
            actions.append(_SGR_FLAGS[code])
        elif 30 <= code <= 37 or 90 <= code <= 97:
            actions.append(("fgColor", str(code)))
        elif 40 <= code <= 47 or 100 <= code <= 107:
            actions.append(("bgColor", str(code)))
        index += 1
    return tuple(actions)


class AnsiCodeTracker:
    def __init__(self) -> None:
        self.clear()

    def process(self, ansi_code: str) -> None:
        hyperlink = _parse_osc8_hyperlink(ansi_code)
        if hyperlink is not NotImplemented:
            self.activeHyperlink = hyperlink
            return

        if not ansi_code.endswith("m"):
            return

        actions = _SGR_ACTIONS.get(ansi_code)
        if actions is None:
            actions = _sgr_actions(ansi_code)
            if len(_SGR_ACTIONS) < 4096:
                _SGR_ACTIONS[ansi_code] = actions
        for attribute, value in actions:
            if attribute == "reset":
                self._reset()
            else:
                setattr(self, attribute, value)

    def _reset(self) -> None:
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

    def clear(self) -> None:
        self._reset()
        self.activeHyperlink: ActiveHyperlink | None = None

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
        if self.fgColor is not None:
            codes.append(self.fgColor)
        if self.bgColor is not None:
            codes.append(self.bgColor)
        result = f"\x1b[{';'.join(codes)}m" if codes else ""
        if self.activeHyperlink is not None:
            result += _format_osc8_hyperlink(self.activeHyperlink)
        return result

    def getLineEndReset(self) -> str:
        result = ""
        if self.underline:
            result += "\x1b[24m"
        if self.activeHyperlink is not None:
            result += _format_osc8_close(self.activeHyperlink.terminator)
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


def _update_tracker_from_text(text: str, tracker: AnsiCodeTracker) -> None:
    for match in _ESCAPE_RE.finditer(text):
        tracker.process(match.group())


def _text_run_end(text: str, index: int) -> int:
    """Where the visible text starting at ``index`` ends: at the next escape sequence, or
    at the end. An ESC that starts no sequence is text (pi: extractAnsiCode returns null)."""
    match = _ESCAPE_RE.search(text, index + 1)
    return match.start() if match is not None else len(text)


# Characters that may break lines anywhere (approximates the TS Script_Extensions check for
# Han/Hiragana/Katakana/Hangul/Bopomofo; Python re has no \p{Script}, so the main blocks are
# listed explicitly, covering all common planes)
_CJK_CLASS = (
    r"\u2e80-\u2eff\u3005\u3007\u3041-\u30ff\u3100-\u312f"
    r"\u31a0-\u31bf\u31f0-\u31ff\u3130-\u318f\u3400-\u4dbf"
    r"\u4e00-\u9fff\ua960-\ua97f\uac00-\ud7ff\uf900-\ufaff"
    r"\uff66-\uff9d\U00020000-\U0002ffff"
)
_CJK_BREAK_RE = re.compile(f"[{_CJK_CLASS}]")
# A run of visible text cut where pi's token loop cuts: CJK characters (one token each there,
# one run here), runs of spaces, runs of everything else.
_UNIT_RE = re.compile(f"([{_CJK_CLASS}]+)|( +)|([^ {_CJK_CLASS}]+)")


def _units(text: str) -> list[tuple[str, int, tuple[str, str, str, str] | None]]:
    """pi's splitIntoTokensWithAnsi: the line as tokens -- runs of spaces, runs of other text,
    one CJK character each -- with escape codes attached to the token that follows them (or
    to the last one). Each entry is (token, width, cjk): the width is -1 for a token pi must
    measure grapheme by grapheme; cjk keeps a run of consecutive CJK tokens together as
    (codes before, characters, their widths, codes after), and the wrap loop breaks such a run
    between any two characters, as pi breaks between any two of its tokens."""
    units: list[tuple[str, int, tuple[str, str, str, str] | None]] = []
    current = ""
    current_width = 0
    current_kind: str | None = None
    pending = ""
    index = 0
    length = len(text)
    while index < length:
        if text[index] == "\x1b":
            match = _ESCAPE_RE.match(text, index)
            if match is not None:
                pending += match.group()
                index = match.end()
                continue
        end = _text_run_end(text, index)
        run = text[index:end]
        index = end
        if run.isascii() and run.isprintable():
            widths = None                       # every character one column wide
        else:
            widths = _widths(run)
            if widths is None:                  # clusters: pi's grapheme loop, measured later
                for segment in _iter_graphemes(run):
                    if segment != " " and _CJK_BREAK_RE.search(segment):
                        if current:
                            units.append((current, current_width, None))
                            current, current_width, current_kind = "", 0, None
                        units.append((pending + segment, -1, None))
                        pending = ""
                        continue
                    kind = "space" if segment == " " else "word"
                    if current and current_kind != kind:
                        units.append((current, current_width, None))
                        current = ""
                    if pending:
                        current += pending
                        pending = ""
                    current_kind = kind
                    current += segment
                    current_width = -1
                continue
        offset = 0
        for cjk, space, word in _UNIT_RE.findall(run):
            if cjk:
                if current:
                    units.append((current, current_width, None))
                    current, current_width, current_kind = "", 0, None
                cjk_widths = "1" * len(cjk) if widths is None else widths[offset:offset + len(cjk)]
                units.append((pending + cjk, len(cjk) + cjk_widths.count("2"), (pending, cjk, cjk_widths, "")))
                pending = ""
                offset += len(cjk)
                continue
            token = space or word
            kind = "space" if space else "word"
            width = len(token) if widths is None else len(token) + widths.count("2", offset, offset + len(token))
            offset += len(token)
            if current and current_kind != kind:
                units.append((current, current_width, None))
                current, current_width = "", 0
            if pending:
                current += pending
                pending = ""
            current_kind = kind
            current += token
            if current_width >= 0:
                current_width += width

    if pending:
        if current:
            current += pending
        elif units:
            token, width, cjk = units[-1]
            units[-1] = (token + pending, width, cjk and (cjk[0], cjk[1], cjk[2], cjk[3] + pending))
        else:
            current = pending
    if current:
        units.append((current, current_width, None))
    return units


def _single_characters(unit: tuple[str, int, tuple[str, str, str, str] | None]):
    """A CJK run as the tokens pi sees, one character each (for widths too narrow for the run loop)."""
    token, width, cjk = unit
    if cjk is None:
        yield token, width, None
        return
    codes_before, characters, widths, codes_after = cjk
    last = len(characters) - 1
    for position, character in enumerate(characters):
        piece = (codes_before if position == 0 else "") + character + (codes_after if position == last else "")
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


def wrap_text_with_ansi(text: str, width: int) -> list[str]:
    if not text:
        return [""]

    input_lines = re.split(r"\r\n|\r|\n", text)
    result: list[str] = []
    tracker = AnsiCodeTracker()
    last = len(input_lines) - 1
    for index, input_line in enumerate(input_lines):
        prefix = tracker.getActiveCodes() if result else ""
        result.extend(_wrap_single_line(prefix + input_line, width))
        if index < last:                  # the codes carry into the next line; none follows the last
            _update_tracker_from_text(input_line, tracker)
    return result or [""]


def _wrap_single_line(line: str, width: int) -> list[str]:
    if not line:
        return [""]
    if visible_width(line) <= width:
        return [line]

    wrapped: list[str] = []
    tracker = AnsiCodeTracker()
    active = ""                 # tracker.getActiveCodes() for the tokens placed so far ...
    reset = ""                  # ... and getLineEndReset(): both change only with a token that carries codes
    units = _units(line)
    if width < 2:               # a CJK character may be wider than the line: pi's long-word path
        units = [piece for unit in units for piece in _single_characters(unit)]
    current_line = ""
    current_visible_length = 0

    for token, token_visible_length, cjk in units:
        if token_visible_length < 0:
            token_visible_length = visible_width(token)
        if cjk is not None and current_visible_length + token_visible_length > width:
            # pi sees one token per character and breaks wherever the next no longer fits;
            # the run is placed by the same rule, as many characters as fill the line at once.
            codes_before, characters, widths, codes_after = cjk
            position = 0
            count = len(characters)
            while position < count:
                fitting = _fitting(widths, position, count, width - current_visible_length)
                if fitting == 0:                # the line holds something already: end it
                    wrapped.append(current_line.rstrip() + reset)
                    current_line = active
                    current_visible_length = 0
                    continue
                piece = characters[position:position + fitting]
                if position == 0:
                    piece = codes_before + piece
                position += fitting
                if position == count:
                    piece += codes_after
                current_line += piece
                current_visible_length += fitting + widths.count("2", position - fitting, position)
                if position == fitting and codes_before:   # placed with its first character: its codes apply from here
                    _update_tracker_from_text(codes_before, tracker)
                    active = tracker.getActiveCodes()
                    reset = tracker.getLineEndReset()
            if codes_after:
                _update_tracker_from_text(codes_after, tracker)
                active = tracker.getActiveCodes()
                reset = tracker.getLineEndReset()
            continue

        if token_visible_length > width and not token.isspace():
            if current_line:
                wrapped.append(current_line + reset)
                current_line = ""
                current_visible_length = 0
            broken = _break_long_word(token, width, tracker)
            wrapped.extend(broken[:-1])
            current_line = broken[-1]
            current_visible_length = visible_width(current_line)
            if "\x1b" in token:                 # _break_long_word fed the tracker
                active = tracker.getActiveCodes()
                reset = tracker.getLineEndReset()
            continue

        if current_visible_length + token_visible_length > width and current_visible_length > 0:
            wrapped.append(current_line.rstrip() + reset)
            if token.isspace():
                current_line = active
                current_visible_length = 0
            else:
                current_line = active + token
                current_visible_length = token_visible_length
        else:
            current_line += token
            current_visible_length += token_visible_length
        if "\x1b" in token:
            _update_tracker_from_text(token, tracker)
            active = tracker.getActiveCodes()
            reset = tracker.getLineEndReset()

    if current_line:
        wrapped.append(current_line)
    return [line.rstrip() for line in wrapped] or [""]


def is_whitespace_char(char: str) -> bool:
    return bool(re.search(r"\s", char))


def is_punctuation_char(char: str) -> bool:
    return bool(_PUNCTUATION_RE.search(char))


def _break_long_word(word: str, width: int, tracker: AnsiCodeTracker) -> list[str]:
    lines: list[str] = []
    current_line = tracker.getActiveCodes()
    current_width = 0
    index = 0
    length = len(word)
    while index < length:
        if word[index] == "\x1b":
            match = _ESCAPE_RE.match(word, index)
            if match is not None:
                current_line += match.group()
                tracker.process(match.group())
                index = match.end()
                continue
        end = _text_run_end(word, index)
        run = word[index:end]
        index = end
        widths = _widths(run)
        if widths is None:
            pieces = [(segment, visible_width(segment)) for segment in _iter_graphemes(run)]
        else:
            pieces = zip(run, map(int, widths))
        for segment, segment_width in pieces:
            if current_width + segment_width > width:
                reset = tracker.getLineEndReset()
                if reset:
                    current_line += reset
                lines.append(current_line)
                current_line = tracker.getActiveCodes()
                current_width = 0
            current_line += segment
            current_width += segment_width

    if current_line:
        lines.append(current_line)
    return lines or [""]


def apply_background_to_line(line: str, width: int, bg_fn: callable) -> str:
    visible_len = visible_width(line)
    padding_needed = max(0, width - visible_len)
    return bg_fn(line + (" " * padding_needed))


def _truncate_fragment_to_width(text: str, max_width: int) -> tuple[str, int]:
    if max_width <= 0 or not text:
        return ("", 0)
    if _is_printable_ascii(text):
        clipped = text[:max_width]
        return (clipped, len(clipped))

    result = ""
    width = 0
    index = 0
    pending_ansi = ""
    while index < len(text):
        ansi = extract_ansi_code(text, index)
        if ansi is not None:
            pending_ansi += ansi.code
            index += ansi.length
            continue
        if text[index] == "\t":
            if width + 3 > max_width:
                break
            if pending_ansi:
                result += pending_ansi
                pending_ansi = ""
            result += "\t"
            width += 3
            index += 1
            continue

        end = index
        while end < len(text) and text[end] != "\t" and extract_ansi_code(text, end) is None:
            end += 1
        for segment in _iter_graphemes(text[index:end]):
            segment_width = _grapheme_width(segment)
            if width + segment_width > max_width:
                return (result, width)
            if pending_ansi:
                result += pending_ansi
                pending_ansi = ""
            result += segment
            width += segment_width
        index = end
    return (result, width)


def _get_active_osc8_close(prefix: str) -> str:
    """If the kept prefix ends inside an unclosed OSC 8 link, return the closing sequence (preserving its BEL/ST terminator). (pi b780d20 #7657)"""
    if "\x1b]8;" not in prefix:  # skip the per-character scan for plain-text prefixes (pi 229afb8 #7665)
        return ""
    active: ActiveHyperlink | None = None
    i = 0
    while i < len(prefix):
        ansi = extract_ansi_code(prefix, i)
        if ansi:
            hyperlink = _parse_osc8_hyperlink(ansi.code)
            if hyperlink is not NotImplemented:
                active = hyperlink
            i += ansi.length
        else:
            i += 1
    return _format_osc8_close(active.terminator) if active else ""


def _finalize_truncated_result(
    prefix: str,
    prefix_width: int,
    ellipsis: str,
    ellipsis_width: int,
    max_width: int,
    pad: bool,
) -> str:
    reset = "\x1b[0m"
    hyperlink_close = _get_active_osc8_close(prefix)
    visible = prefix_width + ellipsis_width
    result = (f"{prefix}{hyperlink_close}{reset}{ellipsis}{reset}" if ellipsis
              else f"{prefix}{hyperlink_close}{reset}")
    return result + (" " * max(0, max_width - visible)) if pad else result


def truncate_to_width(text: str, max_width: int, ellipsis: str = "...", pad: bool = False) -> str:
    if max_width <= 0:
        return ""
    if text == "":
        return " " * max_width if pad else ""

    ellipsis_width = visible_width(ellipsis)
    if ellipsis_width >= max_width:
        text_width = visible_width(text)
        if text_width <= max_width:
            return text + (" " * (max_width - text_width)) if pad else text
        clipped_ellipsis, clipped_width = _truncate_fragment_to_width(ellipsis, max_width)
        if clipped_width == 0:
            return " " * max_width if pad else ""
        return _finalize_truncated_result("", 0, clipped_ellipsis, clipped_width, max_width, pad)

    if _is_printable_ascii(text):
        if len(text) <= max_width:
            return text + (" " * (max_width - len(text))) if pad else text
        target_width = max_width - ellipsis_width
        return _finalize_truncated_result(text[:target_width], target_width, ellipsis, ellipsis_width, max_width, pad)

    target_width = max_width - ellipsis_width
    result = ""
    pending_ansi = ""
    visible_so_far = 0
    kept_width = 0
    keep_contiguous_prefix = True
    overflowed = False
    exhausted_input = False
    index = 0

    while index < len(text):
        ansi = extract_ansi_code(text, index)
        if ansi is not None:
            pending_ansi += ansi.code
            index += ansi.length
            continue

        if text[index] == "\t":
            if keep_contiguous_prefix and kept_width + 3 <= target_width:
                if pending_ansi:
                    result += pending_ansi
                    pending_ansi = ""
                result += "\t"
                kept_width += 3
            else:
                keep_contiguous_prefix = False
                pending_ansi = ""
            visible_so_far += 3
            if visible_so_far > max_width:
                overflowed = True
                break
            index += 1
            continue

        end = index
        while end < len(text) and text[end] != "\t" and extract_ansi_code(text, end) is None:
            end += 1
        for segment in _iter_graphemes(text[index:end]):
            segment_width = _grapheme_width(segment)
            if keep_contiguous_prefix and kept_width + segment_width <= target_width:
                if pending_ansi:
                    result += pending_ansi
                    pending_ansi = ""
                result += segment
                kept_width += segment_width
            else:
                keep_contiguous_prefix = False
                pending_ansi = ""
            visible_so_far += segment_width
            if visible_so_far > max_width:
                overflowed = True
                break
        if overflowed:
            break
        index = end

    exhausted_input = index >= len(text)
    if not overflowed and exhausted_input:
        return text + (" " * max(0, max_width - visible_so_far)) if pad else text
    return _finalize_truncated_result(result, kept_width, ellipsis, ellipsis_width, max_width, pad)


def slice_by_column(line: str, start_col: int, length: int, strict: bool = False) -> str:
    return slice_with_width(line, start_col, length, strict).text


@dataclass(slots=True)
class SliceResult:
    text: str
    width: int


def slice_with_width(line: str, start_col: int, length: int, strict: bool = False) -> SliceResult:
    if length <= 0:
        return SliceResult(text="", width=0)

    end_col = start_col + length
    result = ""
    result_width = 0
    current_col = 0
    index = 0
    pending_ansi = ""
    while index < len(line):
        ansi = extract_ansi_code(line, index)
        if ansi is not None:
            if start_col <= current_col < end_col:
                result += ansi.code
            elif current_col < start_col:
                pending_ansi += ansi.code
            index += ansi.length
            continue

        text_end = index
        while text_end < len(line) and extract_ansi_code(line, text_end) is None:
            text_end += 1

        for segment in _iter_graphemes(line[index:text_end]):
            segment_width = _grapheme_width(segment)
            in_range = start_col <= current_col < end_col
            fits = not strict or current_col + segment_width <= end_col
            if in_range and fits:
                if pending_ansi:
                    result += pending_ansi
                    pending_ansi = ""
                result += segment
                result_width += segment_width
            current_col += segment_width
            if current_col >= end_col:
                break
        index = text_end
        if current_col >= end_col:
            break
    return SliceResult(text=result, width=result_width)


_pooled_style_tracker = AnsiCodeTracker()


@dataclass(slots=True)
class ExtractedSegments:
    before: str
    beforeWidth: int
    after: str
    afterWidth: int


def extract_segments(
    line: str,
    before_end: int,
    after_start: int,
    after_len: int,
    strict_after: bool = False,
) -> ExtractedSegments:
    before = ""
    before_width = 0
    after = ""
    after_width = 0
    current_col = 0
    index = 0
    pending_ansi_before = ""
    after_started = False
    after_end = after_start + after_len
    _pooled_style_tracker.clear()

    while index < len(line):
        ansi = extract_ansi_code(line, index)
        if ansi is not None:
            _pooled_style_tracker.process(ansi.code)
            if current_col < before_end:
                pending_ansi_before += ansi.code
            elif after_start <= current_col < after_end and after_started:
                after += ansi.code
            index += ansi.length
            continue

        text_end = index
        while text_end < len(line) and extract_ansi_code(line, text_end) is None:
            text_end += 1

        for segment in _iter_graphemes(line[index:text_end]):
            segment_width = _grapheme_width(segment)
            if current_col < before_end and current_col + segment_width <= before_end:
                if pending_ansi_before:
                    before += pending_ansi_before
                    pending_ansi_before = ""
                before += segment
                before_width += segment_width
            elif after_start <= current_col < after_end:
                fits = not strict_after or current_col + segment_width <= after_end
                if fits:
                    if not after_started:
                        after += _pooled_style_tracker.getActiveCodes()
                        after_started = True
                    after += segment
                    after_width += segment_width

            current_col += segment_width
            limit = after_end if after_len > 0 else before_end
            if current_col >= limit:
                break
        index = text_end
        limit = after_end if after_len > 0 else before_end
        if current_col >= limit:
            break

    return ExtractedSegments(before=before, beforeWidth=before_width, after=after, afterWidth=after_width)


visibleWidth = visible_width
normalizeTerminalOutput = normalize_terminal_output
wrapTextWithAnsi = wrap_text_with_ansi
isWhitespaceChar = is_whitespace_char
isPunctuationChar = is_punctuation_char
applyBackgroundToLine = apply_background_to_line
truncateToWidth = truncate_to_width
sliceByColumn = slice_by_column
sliceWithWidth = slice_with_width
extractSegments = extract_segments
getSegmenter = get_segmenter

__all__ = [
    "applyBackgroundToLine",
    "extractSegments",
    "getSegmenter",
    "isPunctuationChar",
    "isWhitespaceChar",
    "normalizeTerminalOutput",
    "sliceByColumn",
    "sliceWithWidth",
    "truncateToWidth",
    "visibleWidth",
    "wrapTextWithAnsi",
]
