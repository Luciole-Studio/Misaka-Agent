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
_WIDTH_CACHE_SIZE = 512
_width_cache: OrderedDict[str, int] = OrderedDict()


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


@lru_cache(maxsize=1024)
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


def visible_width(text: str) -> int:
    if len(text) == 0:
        return 0
    if _is_printable_ascii(text):
        return len(text)

    cached = _width_cache.get(text)
    if cached is not None:
        return cached

    clean = text.replace("\t", "   ") if "\t" in text else text
    if "\x1b" in clean:
        stripped_chars: list[str] = []
        index = 0
        while index < len(clean):
            ansi = extract_ansi_code(clean, index)
            if ansi is not None:
                index += ansi.length
                continue
            stripped_chars.append(clean[index])
            index += 1
        clean = "".join(stripped_chars)

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


def extract_ansi_code(text: str, pos: int) -> AnsiMatch | None:
    if pos >= len(text) or text[pos] != "\x1b":
        return None
    if pos + 1 >= len(text):
        return None

    next_char = text[pos + 1]
    if next_char == "[":
        # ECMA-48 CSI: parameter bytes 0x30-0x3F, then intermediate bytes 0x20-0x2F, then one
        # final byte 0x40-0x7E. Pi (utils.ts:406-420) instead scans forward for `[mGKHJ]`, which
        # makes any other CSI — `\x1b[10A`, `\x1b[6n`, `\x1b[?25l` — swallow every character up
        # to the next `m`. `TUI.applyLineResets` appends `\x1b[0m` to every line, so that `m`
        # always exists, and `visible_width` then counts the swallowed text as zero: lines get
        # wrapped and truncated against a width that is far too small, the terminal soft-wraps
        # them, and the over-wide-line guard in `_doRenderInner` never fires. Scan the real
        # grammar instead, and report "not an escape" for anything that does not terminate.
        end = pos + 2
        while end < len(text) and "\x30" <= text[end] <= "\x3f":
            end += 1
        while end < len(text) and "\x20" <= text[end] <= "\x2f":
            end += 1
        if end < len(text) and "\x40" <= text[end] <= "\x7e":
            return AnsiMatch(code=text[pos : end + 1], length=end + 1 - pos)
        return None

    if next_char in {"]", "_"}:
        end = pos + 2
        while end < len(text):
            if text[end] == "\x07":
                return AnsiMatch(code=text[pos : end + 1], length=end + 1 - pos)
            if text[end] == "\x1b" and end + 1 < len(text) and text[end + 1] == "\\":
                return AnsiMatch(code=text[pos : end + 2], length=end + 2 - pos)
            end += 1
        return None

    return None


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

        match = re.match(r"\x1b\[([\d;]*)m", ansi_code)
        if match is None:
            return
        params = match.group(1)
        if params in {"", "0"}:
            self._reset()
            return

        parts = params.split(";")
        index = 0
        while index < len(parts):
            try:
                code = int(parts[index])
            except ValueError:
                index += 1
                continue

            if code in {38, 48}:
                if index + 2 < len(parts) and parts[index + 1] == "5":
                    color_code = ";".join(parts[index : index + 3])
                    if code == 38:
                        self.fgColor = color_code
                    else:
                        self.bgColor = color_code
                    index += 3
                    continue
                if index + 4 < len(parts) and parts[index + 1] == "2":
                    color_code = ";".join(parts[index : index + 5])
                    if code == 38:
                        self.fgColor = color_code
                    else:
                        self.bgColor = color_code
                    index += 5
                    continue

            match code:
                case 0:
                    self._reset()
                case 1:
                    self.bold = True
                case 2:
                    self.dim = True
                case 3:
                    self.italic = True
                case 4:
                    self.underline = True
                case 5:
                    self.blink = True
                case 7:
                    self.inverse = True
                case 8:
                    self.hidden = True
                case 9:
                    self.strikethrough = True
                case 21:
                    self.bold = False
                case 22:
                    self.bold = False
                    self.dim = False
                case 23:
                    self.italic = False
                case 24:
                    self.underline = False
                case 25:
                    self.blink = False
                case 27:
                    self.inverse = False
                case 28:
                    self.hidden = False
                case 29:
                    self.strikethrough = False
                case 39:
                    self.fgColor = None
                case 49:
                    self.bgColor = None
                case _:
                    if 30 <= code <= 37 or 90 <= code <= 97:
                        self.fgColor = str(code)
                    elif 40 <= code <= 47 or 100 <= code <= 107:
                        self.bgColor = str(code)
            index += 1

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
    index = 0
    while index < len(text):
        ansi = extract_ansi_code(text, index)
        if ansi is not None:
            tracker.process(ansi.code)
            index += ansi.length
            continue
        index += 1


# Characters that may break lines anywhere (approximates the TS Script_Extensions check for
# Han/Hiragana/Katakana/Hangul/Bopomofo; Python re has no \p{Script}, so the main blocks are
# listed explicitly, covering all common planes)
_CJK_BREAK_RE = re.compile(
    r"[\u2e80-\u2eff\u3005\u3007\u3041-\u30ff\u3100-\u312f"
    r"\u31a0-\u31bf\u31f0-\u31ff\u3130-\u318f\u3400-\u4dbf"
    r"\u4e00-\u9fff\ua960-\ua97f\uac00-\ud7ff\uf900-\ufaff"
    r"\uff66-\uff9d\U00020000-\U0002ffff]"
)


def _split_into_tokens_with_ansi(text: str) -> list[str]:
    tokens: list[str] = []
    current = ""
    pending_ansi = ""
    current_kind: str | None = None
    index = 0

    def flush_current() -> None:
        nonlocal current, current_kind
        if current:
            tokens.append(current)
            current = ""
            current_kind = None

    while index < len(text):
        ansi = extract_ansi_code(text, index)
        if ansi is not None:
            pending_ansi += ansi.code
            index += ansi.length
            continue
        text_end = index
        while text_end < len(text) and extract_ansi_code(text, text_end) is None:
            text_end += 1
        for segment in _iter_graphemes(text[index:text_end]):
            segment_is_space = segment == " "
            if not segment_is_space and _CJK_BREAK_RE.search(segment):
                flush_current()  # each CJK character is its own token: a line may break between any two
                tokens.append(pending_ansi + segment)
                pending_ansi = ""
                continue
            segment_kind = "space" if segment_is_space else "word"
            if current and current_kind != segment_kind:
                flush_current()
            if pending_ansi:
                current += pending_ansi
                pending_ansi = ""
            current_kind = segment_kind
            current += segment
        index = text_end

    if pending_ansi:
        if current:
            current += pending_ansi
        elif tokens:
            tokens[-1] += pending_ansi
        else:
            current = pending_ansi
    flush_current()
    return tokens


def wrap_text_with_ansi(text: str, width: int) -> list[str]:
    if not text:
        return [""]

    input_lines = re.split(r"\r\n|\r|\n", text)
    result: list[str] = []
    tracker = AnsiCodeTracker()
    for input_line in input_lines:
        prefix = tracker.getActiveCodes() if result else ""
        result.extend(_wrap_single_line(prefix + input_line, width))
        _update_tracker_from_text(input_line, tracker)
    return result or [""]


def _wrap_single_line(line: str, width: int) -> list[str]:
    if not line:
        return [""]
    if visible_width(line) <= width:
        return [line]

    wrapped: list[str] = []
    tracker = AnsiCodeTracker()
    tokens = _split_into_tokens_with_ansi(line)
    current_line = ""
    current_visible_length = 0

    for token in tokens:
        token_visible_length = visible_width(token)
        is_whitespace = token.strip() == ""
        if token_visible_length > width and not is_whitespace:
            if current_line:
                reset = tracker.getLineEndReset()
                if reset:
                    current_line += reset
                wrapped.append(current_line)
                current_line = ""
                current_visible_length = 0

            broken = _break_long_word(token, width, tracker)
            wrapped.extend(broken[:-1])
            current_line = broken[-1]
            current_visible_length = visible_width(current_line)
            continue

        total_needed = current_visible_length + token_visible_length
        if total_needed > width and current_visible_length > 0:
            line_to_wrap = current_line.rstrip()
            reset = tracker.getLineEndReset()
            if reset:
                line_to_wrap += reset
            wrapped.append(line_to_wrap)
            if is_whitespace:
                current_line = tracker.getActiveCodes()
                current_visible_length = 0
            else:
                current_line = tracker.getActiveCodes() + token
                current_visible_length = token_visible_length
        else:
            current_line += token
            current_visible_length += token_visible_length
        _update_tracker_from_text(token, tracker)

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
    segments: list[tuple[str, str]] = []
    index = 0
    while index < len(word):
        ansi = extract_ansi_code(word, index)
        if ansi is not None:
            segments.append(("ansi", ansi.code))
            index += ansi.length
            continue
        end = index
        while end < len(word) and extract_ansi_code(word, end) is None:
            end += 1
        for segment in _iter_graphemes(word[index:end]):
            segments.append(("grapheme", segment))
        index = end

    for segment_type, value in segments:
        if segment_type == "ansi":
            current_line += value
            tracker.process(value)
            continue
        if not value:
            continue
        width_value = visible_width(value)
        if current_width + width_value > width:
            reset = tracker.getLineEndReset()
            if reset:
                current_line += reset
            lines.append(current_line)
            current_line = tracker.getActiveCodes()
            current_width = 0
        current_line += value
        current_width += width_value

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
