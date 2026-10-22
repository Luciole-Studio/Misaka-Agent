"""MISAKA fork: the JavaScript built-ins the translated pi packages rely on (ai, agent, the TUI, the coding agent, MCP,
codemode, Chord, durable and env), where Python's counterparts differ.

pi's packages run on Node. A literal translation goes wrong in a few recurring places:
``JSON.stringify`` and ``json.dumps`` print numbers and lone surrogates differently, ``trim()`` and
``str.strip()`` strip different characters, ``String(x)`` is not ``str(x)``, a JavaScript ``async``
function starts running when it is called while a coroutine waits to be awaited, and
``AbortController`` carries a reason that misaka's own controllers do not. Each helper here stands
in for one of those built-ins, so the translated modules can stay line for line with upstream.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import functools
import inspect
import json
import math
import os
import re
import sys
import weakref
from collections.abc import (
    Awaitable,
    Callable,
    Coroutine,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from functools import partial
from typing import Any, Final, TypedDict

from misaka.utils._abort_signal import wait_for_abort
from misaka.utils.values import signal_aborted


class _Undefined:
    """JavaScript's ``undefined``, where it differs from ``null`` (an absent ``data`` field). Use the
    one instance, ``UNDEFINED``."""

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "undefined"

    # A copy, a deep copy or a pickle round trip is still the one instance, as `undefined` is.
    def __copy__(self) -> _Undefined:
        return self

    def __deepcopy__(self, _memo: Any) -> _Undefined:
        return self

    def __reduce__(self) -> str:
        return "UNDEFINED"


UNDEFINED: Final = _Undefined()

# `String.prototype.trim()` strips ECMAScript WhiteSpace and LineTerminator, which is not the set
# `str.strip()` strips (U+FEFF versus U+001C-U+001F and U+0085).
_ECMASCRIPT_WHITESPACE: Final = (
    "\u0009\u000a\u000b\u000c\u000d   "
    "           "
    "    　﻿"
)
# The set for callers that test single characters (JavaScript's `\s`).
ECMASCRIPT_WHITESPACE: Final = _ECMASCRIPT_WHITESPACE
# JavaScript's `\s` as the inside of a character class, for patterns that must not use Python's `\s`.
JS_WHITESPACE_CLASS: Final = re.escape(_ECMASCRIPT_WHITESPACE)
_JS_WHITESPACE_RUN: Final = re.compile(f"[{JS_WHITESPACE_CLASS}]+")


def jsSplitWhitespace(text: str, limit: int | None = None) -> list[str]:
    """``text.split(/\\s+/, limit)`` with JavaScript's ``\\s``: ``limit`` keeps that many parts."""
    parts = _JS_WHITESPACE_RUN.split(text)
    return parts if limit is None else parts[:limit]


_SURROGATE = re.compile("[\ud800-\udfff]")


def isNumber(value: Any) -> bool:
    """``typeof value === "number"``: a Python bool is an int, a JavaScript boolean is not a number."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def jsTruthy(value: Any) -> bool:
    """A JavaScript condition: every object is true (``{}`` and ``[]`` too), and ``NaN`` is false."""
    if isinstance(value, dict | list):
        return True
    if isinstance(value, float) and math.isnan(value):
        return False
    return bool(value)


def defined(value: dict[str, Any]) -> dict[str, Any]:
    """``value`` without the keys a JavaScript object would hold as ``undefined`` (here: ``None``), so option bags
    and persisted shapes carry only what is set."""
    return {key: item for key, item in value.items() if item is not None}


def jsTrim(text: str) -> str:
    """``String.prototype.trim()``."""
    return text.strip(_ECMASCRIPT_WHITESPACE)


def jsCollapseWhitespace(text: str) -> str:
    """``text.replace(/\\s+/g, " ")``: ``\\s`` is the ECMAScript whitespace set, which is not the one Python's ``\\s`` is."""
    return _JS_WHITESPACE_RUN.sub(" ", text)


def jsTrimStart(text: str) -> str:
    """``String.prototype.trimStart()``."""
    return text.lstrip(_ECMASCRIPT_WHITESPACE)


def jsTrimEnd(text: str) -> str:
    """``String.prototype.trimEnd()``."""
    return text.rstrip(_ECMASCRIPT_WHITESPACE)


_DECIMAL_LITERAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_RADIX_LITERAL = re.compile(r"0(?P<prefix>[bBoOxX])(?P<digits>[0-9A-Za-z]+)")


def _integral(value: float) -> int | float:
    return int(value) if math.isfinite(value) and value.is_integer() and abs(value) < 1e21 else value


def jsNumber(value: Any) -> int | float:
    """``Number(value)`` for a JSON value: ``null`` is 0, a boolean 0 or 1, a string is read as a
    numeric literal once trimmed (``""`` is 0, ``Infinity`` is allowed, ``0x``/``0o``/``0b`` are
    unsigned), an array converts through ``String()``, and anything else is ``NaN``. An integral
    result stays an ``int`` so it prints without a fraction."""
    if value is None:
        return 0
    if value is UNDEFINED:
        return math.nan
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        # `JSON.parse` already made an integer beyond the double range `Infinity`.
        try:
            float(value)
        except OverflowError:
            return math.inf if value > 0 else -math.inf
        return value
    if isinstance(value, float):
        return _integral(value)
    if isinstance(value, str):
        text = jsTrim(value)
        if text == "":
            return 0
        if text in ("Infinity", "+Infinity"):
            return math.inf
        if text == "-Infinity":
            return -math.inf
        radix = _RADIX_LITERAL.fullmatch(text)
        if radix is not None:
            try:
                return int(radix.group("digits"), {"b": 2, "o": 8, "x": 16}[radix.group("prefix").lower()])
            except ValueError:
                return math.nan
        if _DECIMAL_LITERAL.fullmatch(text) is None:
            return math.nan
        return _integral(float(text))
    if isinstance(value, list | tuple):
        return jsNumber(jsString(value))
    return math.nan


_MAX_SAFE_INTEGER: Final = 2**53


def formatNumber(value: float) -> str:
    """``Number::toString``: a number as JavaScript prints it, ``500`` not ``500.0``, ``0.00001`` not
    ``1e-05``, and the shortest digits that read back the same double."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if abs(value) <= _MAX_SAFE_INTEGER:
            return str(value)
        try:
            value = float(value)
        except OverflowError:
            return "Infinity" if value > 0 else "-Infinity"
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0:
        return "0"
    _, digitTuple, exponent = Decimal(repr(abs(value))).as_tuple()
    digits = "".join(map(str, digitTuple)).rstrip("0")
    exponent = int(exponent) + len(digitTuple) - len(digits)
    count = len(digits)
    point = count + exponent
    sign = "-" if value < 0 else ""
    if count <= point <= 21:
        return f"{sign}{digits}{'0' * (point - count)}"
    if 0 < point <= 21:
        return f"{sign}{digits[:point]}.{digits[point:]}"
    if -6 < point <= 0:
        return f"{sign}0.{'0' * -point}{digits}"
    exp = point - 1
    mantissa = digits if count == 1 else f"{digits[0]}.{digits[1:]}"
    return f"{sign}{mantissa}e{'+' if exp >= 0 else '-'}{abs(exp)}"


def jsRound(value: float) -> int:
    """``Math.round(value)`` for a finite number: ties go toward positive infinity."""
    return int((Decimal(value) + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR))


def toFixed(value: float, digits: int) -> str:
    """``Number.prototype.toFixed(digits)``: the exact value of the double, rounded half up in magnitude."""
    if not math.isfinite(value) or abs(value) >= 1e21:
        return formatNumber(value)
    if value == 0:
        value = 0.0
    rounded = Decimal(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    return format(rounded, "f")


def toPrecision(value: float, precision: int) -> str:
    """``Number.prototype.toPrecision(precision)`` for a finite number."""
    if not math.isfinite(value):
        return formatNumber(value)
    if value == 0:
        return "0" if precision == 1 else f"0.{'0' * (precision - 1)}"
    sign = "-" if value < 0 else ""
    exact = Decimal(abs(value))
    exponent = exact.adjusted()
    scaled = exact.scaleb(precision - 1 - exponent).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    if scaled >= 10**precision:
        scaled //= 10
        exponent += 1
    digits = str(int(scaled))
    if exponent < -6 or exponent >= precision:
        mantissa = digits if precision == 1 else f"{digits[0]}.{digits[1:]}"
        return f"{sign}{mantissa}e{'+' if exponent >= 0 else '-'}{abs(exponent)}"
    if exponent == precision - 1:
        return f"{sign}{digits}"
    if exponent >= 0:
        return f"{sign}{digits[: exponent + 1]}.{digits[exponent + 1 :]}"
    return f"{sign}0.{'0' * -(exponent + 1)}{digits}"


class RangeError(ValueError):
    """JavaScript's ``RangeError``."""


def jsIsInteger(value: Any) -> bool:
    """``Number.isInteger(value)``: a finite number with no fractional part (not a boolean)."""
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value) and value.is_integer())


def jsIsSafeInteger(value: Any) -> bool:
    """``Number.isSafeInteger(value)``: an integer no further from zero than 2**53 - 1."""
    return jsIsInteger(value) and abs(value) <= 2**53 - 1


_ASTRAL = re.compile("[\U00010000-\U0010ffff]")


def toUtf16Units(text: str) -> str:
    """The string as UTF-16 code units, one ``str`` character each: an astral character becomes its two
    surrogates. Indexing, slicing, ``find`` and comparison on the result count code units, as a
    JavaScript string does."""
    if text.isascii():
        return text

    def split(match: re.Match[str]) -> str:
        code = ord(match.group()) - 0x10000
        return chr(0xD800 + (code >> 10)) + chr(0xDC00 + (code & 0x3FF))

    return _ASTRAL.sub(split, text)


def fromUtf16Units(units: str) -> str:
    """The inverse of :func:`toUtf16Units`: surrogate pairs become the astral characters again; a lone
    surrogate stays."""
    if units.isascii():
        return units
    return units.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


def jsLength(text: str) -> int:
    """``text.length``: UTF-16 code units."""
    if text.isascii():
        return len(text)
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def jsSlice(text: str, start: int = 0, end: int | None = None) -> str:
    """``text.slice(start, end)``: indices count UTF-16 code units; a negative one counts from the end."""
    if text.isascii():
        return text[start:end]
    return fromUtf16Units(toUtf16Units(text)[start:end])


def bufferFromBase64(text: str) -> bytes:
    """``Buffer.from(text, "base64")``: whitespace and other characters outside the alphabet are
    skipped, URL-safe characters are accepted, padding is optional, and decoding stops at the first
    ``=``."""
    cleaned = re.sub(r"[^A-Za-z0-9+/_=-]", "", text).split("=", 1)[0].replace("-", "+").replace("_", "/")
    if len(cleaned) % 4 == 1:
        cleaned = cleaned[:-1]
    try:
        return base64.b64decode(cleaned + "=" * (-len(cleaned) % 4))
    except binascii.Error:
        return b""


def toWellFormed(text: str) -> str:
    """``String.prototype.toWellFormed``: a lone surrogate becomes U+FFFD."""
    return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def jsString(value: Any) -> str:
    """``String(value)``."""
    if value is UNDEFINED:
        return "undefined"
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return formatNumber(value)
    if isinstance(value, BaseException):
        name = getattr(value, "name", type(value).__name__)
        message = str(value)
        return f"{name}: {message}" if message else str(name)
    if isinstance(value, list | tuple):
        return ",".join("" if item is None or item is UNDEFINED else jsString(item) for item in value)
    if isinstance(value, dict):
        return "[object Object]"
    return str(value)


@functools.lru_cache(maxsize=1024)
def _positionalArity(function: Callable[..., Any]) -> int | None:
    """How many positional arguments ``function`` takes, or ``None`` when it takes any number."""
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return None
    count = 0
    for parameter in parameters:
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return None
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            count += 1
    return count


def jsCall(function: Callable[..., Any], *args: Any) -> Any:
    """Call ``function`` as JavaScript calls a function: arguments it does not declare are dropped."""
    try:
        arity = _positionalArity(function)
    except TypeError:  # an unhashable callable
        arity = None
    return function(*(args if arity is None else args[:arity]))


def jsIndex[T](array: Sequence[T], index: int) -> T | None:
    """``array[index]``: ``undefined`` (``None``) for an index outside the array, a negative one included, where
    Python would wrap around or raise."""
    return array[index] if 0 <= index < len(array) else None


_FLOAT_PREFIX = re.compile(r"[+-]?(?:Infinity|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")


def parseFloat(value: Any) -> float:
    """``parseFloat(value)``: the longest decimal prefix of the string after leading white space, or ``NaN``."""
    match = _FLOAT_PREFIX.match(jsTrimStart(jsString(value)))
    if match is None:
        return math.nan
    text = match.group()
    if text.lstrip("+-") == "Infinity":
        return -math.inf if text.startswith("-") else math.inf
    return float(text)


def _isArrayIndex(key: str) -> bool:
    return key.isascii() and key.isdigit() and (key == "0" or not key.startswith("0")) and int(key) <= 4294967294


def objectKeys(value: Mapping[Any, Any]) -> list[Any]:
    """``Object.keys(value)``: array indices (``"0"``, ``"12"``) first in ascending order, then the other keys as
    inserted."""
    indices = sorted((key for key in value if isinstance(key, str) and _isArrayIndex(key)), key=int)
    return [*indices, *(key for key in value if not (isinstance(key, str) and _isArrayIndex(key)))]


def objectEntries(value: Mapping[Any, Any]) -> list[tuple[Any, Any]]:
    """``Object.entries(value)``, in ``Object.keys`` order."""
    return [(key, value[key]) for key in objectKeys(value)]


def jsObjectOrder(value: Any) -> Any:
    """``value`` with every object's keys in JavaScript's property order: array indices (``"0"``, ``"12"``) first in
    ascending order, then the other keys as inserted. ``JSON.parse`` and every later ``Object.keys`` see that order."""
    if isinstance(value, dict):
        indices = sorted((key for key in value if _isArrayIndex(key)), key=int)
        ordered = {key: jsObjectOrder(value[key]) for key in indices}
        for key, item in value.items():
            if key not in ordered:
                ordered[key] = jsObjectOrder(item)
        return ordered
    if isinstance(value, list):
        return [jsObjectOrder(item) for item in value]
    return value


def nullishCoalesce(value: Any, fallback: Any) -> Any:
    """``value ?? fallback``: the fallback stands for ``null`` (``None``) and ``undefined`` (``UNDEFINED``), and for nothing else."""
    return fallback if value is None or value is UNDEFINED else value


class Utf8Decoder:
    """``new TextDecoder()`` (WHATWG ``utf-8``, ``fatal`` and ``ignoreBOM`` off) in streaming use: ``decode(chunk)`` keeps an
    incomplete sequence for the next chunk, an invalid byte becomes U+FFFD at once (``ED A0`` is two, not a sequence still
    waiting), a byte order mark at the start of the stream is dropped, and ``decode(b"", final=True)`` flushes an incomplete
    sequence as one U+FFFD and starts the next stream. ``ignoreBOM=True`` is ``new TextDecoder("utf-8", {ignoreBOM: true})``:
    a leading U+FEFF is text."""

    def __init__(self, ignoreBOM: bool = False) -> None:
        self._ignoreBOM = ignoreBOM
        self._needed = 0
        self._seen = 0
        self._lower = 0x80
        self._upper = 0xBF
        self._codePoint = 0
        self._bomSeen = False

    def _reset(self) -> None:
        self._needed = self._seen = self._codePoint = 0
        self._lower, self._upper = 0x80, 0xBF

    def _consume(self, data: bytes, position: int, out: list[str]) -> int:
        """Run the decoder's state machine from ``position`` until it is at a character boundary again; returns the new position."""
        while position < len(data):
            byte = data[position]
            if self._needed == 0:
                position += 1
                if byte <= 0x7F:
                    out.append(chr(byte))
                    return position
                if 0xC2 <= byte <= 0xDF:
                    self._needed, self._codePoint = 1, byte & 0x1F
                elif 0xE0 <= byte <= 0xEF:
                    if byte == 0xE0:
                        self._lower = 0xA0
                    elif byte == 0xED:
                        self._upper = 0x9F
                    self._needed, self._codePoint = 2, byte & 0xF
                elif 0xF0 <= byte <= 0xF4:
                    if byte == 0xF0:
                        self._lower = 0x90
                    elif byte == 0xF4:
                        self._upper = 0x8F
                    self._needed, self._codePoint = 3, byte & 0x7
                else:
                    out.append("\ufffd")
                    return position
                continue
            if not self._lower <= byte <= self._upper:
                # The byte is not part of the sequence: it is replaced, and read again as the start of the next one.
                self._reset()
                out.append("\ufffd")
                return position
            self._lower, self._upper = 0x80, 0xBF
            self._codePoint = (self._codePoint << 6) | (byte & 0x3F)
            self._seen += 1
            position += 1
            if self._seen == self._needed:
                out.append(chr(self._codePoint))
                self._reset()
                return position
        return position

    def decode(self, data: bytes, final: bool = False) -> str:
        out: list[str] = []
        position = 0
        while position < len(data):
            if self._needed == 0:
                try:
                    out.append(data[position:].decode("utf-8"))
                    position = len(data)
                    break
                except UnicodeDecodeError as error:
                    if error.start > 0:
                        out.append(data[position : position + error.start].decode("utf-8"))
                        position += error.start
            position = self._consume(data, position, out)
        if final and self._needed != 0:
            self._reset()
            out.append("\ufffd")
        text = "".join(out)
        if not self._ignoreBOM and not self._bomSeen and text:
            self._bomSeen = True
            text = text.removeprefix("\ufeff")
        if final:
            self._bomSeen = False
        return text


def _bufferToString(data: bytes) -> str:
    """``buffer.toString("utf8")``: WHATWG decoding with a leading byte order mark kept."""
    return Utf8Decoder(ignoreBOM=True).decode(data, final=True)


class StringDecoder:
    """Node's ``new StringDecoder("utf8")`` (``src/string_decoder.cc``): ``write(buffer)`` decodes all complete characters
    and holds back the bytes of one that the buffer cuts off; ``end(buffer)`` writes ``buffer`` and decodes what is held
    back as it is. Unlike a streaming ``TextDecoder``, a held-back character that the next chunk does not continue is
    decoded on its own, and the byte order mark stays."""

    def __init__(self, encoding: str = "utf8") -> None:
        if encoding.lower() not in ("utf8", "utf-8"):
            raise ValueError(f"Unknown encoding: {encoding}")
        self.encoding = "utf8"
        self._buffer = b""
        self._missing = 0

    def write(self, buffer: bytes) -> str:
        data = bytes(buffer)
        prepend: str | None = None
        nread = len(data)
        offset = 0
        if self._missing > 0:
            # If an incomplete character is found at a chunk boundary, use its remainder and decode it as is.
            for i in range(min(nread, self._missing)):
                if (data[i] & 0xC0) != 0x80:
                    # This byte is not a continuation byte even though it should have been one: stop decoding the
                    # incomplete character here and assume that the byte starts a new one.
                    self._missing = 0
                    self._buffer += data[:i]
                    offset = i
                    nread -= i
                    break
            found = min(nread, self._missing)
            self._buffer += data[offset : offset + found]
            offset += found
            nread -= found
            self._missing -= found
            if self._missing == 0:
                prepend = _bufferToString(self._buffer)
                self._buffer = b""
        if nread == 0:
            return prepend if prepend is not None else ""
        buffered = 0
        if data[offset + nread - 1] & 0x80:
            # The chunk ends on a non-ASCII byte: find where its character begins.
            i = offset + nread - 1
            while True:
                buffered += 1
                if (data[i] & 0xC0) == 0x80:
                    # A trailing byte.
                    if buffered >= 4 or i == offset:
                        # More than 4 trailing bytes, or the previous chunk was not entirely valid UTF-8: treat the
                        # remaining bytes as complete.
                        buffered = 0
                        break
                else:
                    # The first byte of a character: its upper bits tell how long the character should be.
                    if (data[i] & 0xE0) == 0xC0:
                        self._missing = 2
                    elif (data[i] & 0xF0) == 0xE0:
                        self._missing = 3
                    elif (data[i] & 0xF8) == 0xF0:
                        self._missing = 4
                    else:
                        # A lead byte of a character outside the representable range.
                        buffered = 0
                        break
                    if buffered >= self._missing:
                        # As many trailing bytes as the lead byte announced (valid data) or more (invalid anyway).
                        self._missing = 0
                        buffered = 0
                    self._missing -= buffered
                    break
                i -= 1
        if buffered > 0:
            nread -= buffered
            self._buffer = data[offset + nread : offset + nread + buffered]
        body = _bufferToString(data[offset : offset + nread]) if nread > 0 else ""
        return body if prepend is None else prepend + body

    def end(self, buffer: bytes | None = None) -> str:
        text = "" if buffer is None else self.write(buffer)
        if self._buffer:
            text += _bufferToString(self._buffer)
            self._buffer = b""
        self._missing = 0
        return text


def liveKeys(mapping: dict[Any, Any]) -> Iterator[Any]:
    """Iterate a ``Set`` (a dict used for its keys) or the keys of a ``Map`` as JavaScript does while it changes: an entry
    removed before its turn is skipped, and one added during the pass is visited at its end."""
    visited: list[Any] = []
    while True:
        for key in list(mapping):
            if not any(key is seen for seen in visited):
                visited.append(key)
                yield key
                break
        else:
            return


class ErrorOptions(TypedDict, total=False):
    """The ``ErrorOptions`` an ``Error`` constructor takes; ``cause`` is also chained as ``__cause__``."""

    cause: Any

def errorMessage(error: Any) -> str:
    """``error instanceof Error ? error.message : String(error)``. An ``ExceptionGroup`` (``AggregateError``) is its
    message alone, without the sub-exception count ``str()`` appends."""
    if isinstance(error, BaseExceptionGroup):
        return error.message
    if isinstance(error, BaseException):
        return str(error)
    return jsString(error)


def pathResolve(path: str) -> str:
    """``path.resolve(path)``: an absolute, normalized path. POSIX allows a path to keep exactly two leading slashes, which
    ``os.path.abspath`` honours and Node collapses to one."""
    resolved = os.path.abspath(path)
    if resolved.startswith("//") and not resolved.startswith("///"):
        resolved = resolved[1:]
    return resolved


def _quote(text: str) -> str:
    return _SURROGATE.sub(lambda match: f"\\u{ord(match.group()):04x}", json.dumps(text, ensure_ascii=False))


def _jsonNumber(value: float) -> str:
    if isinstance(value, float) and not math.isfinite(value):
        return "null"
    if isinstance(value, int) and abs(value) > _MAX_SAFE_INTEGER:
        try:
            value = float(value)
        except OverflowError:
            return "null"
    return formatNumber(value)


def _jsonText(value: Any, gap: str, indent: str, path: set[int]) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, int | float):
        return _jsonNumber(value)
    if not isinstance(value, list | tuple | dict):
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")
    if id(value) in path:
        raise ValueError("Converting circular structure to JSON")
    path.add(id(value))
    try:
        inner = indent + gap
        if isinstance(value, dict):
            members = [
                f"{_quote(str(key))}:{' ' if gap else ''}{_jsonText(item, gap, inner, path)}"
                for key, item in value.items()
                if item is not UNDEFINED
            ]
            opening, closing = "{", "}"
        else:
            members = ["null" if item is UNDEFINED else _jsonText(item, gap, inner, path) for item in value]
            opening, closing = "[", "]"
    finally:
        path.discard(id(value))
    if not members:
        return f"{opening}{closing}"
    if not gap:
        return f"{opening}{','.join(members)}{closing}"
    return f"{opening}\n{inner}{(',' + chr(10) + inner).join(members)}\n{indent}{closing}"


def jsonStringify(value: Any, indent: int | str | None = None) -> str:
    """``JSON.stringify(value, null, indent)``: numbers as ``Number::toString`` prints them, non-finite
    numbers and ``undefined`` array items as ``null``, ``undefined`` properties left out, text kept as
    text, and lone surrogates escaped (well-formed ``JSON.stringify``). ``indent`` is a count of spaces
    or the indent string itself."""
    # An indent of 0 or "" means no formatting; longer ones are capped at 10, as the spec says.
    gap = ""
    if isinstance(indent, int) and not isinstance(indent, bool):
        gap = " " * min(max(indent, 0), 10)
    elif isinstance(indent, str):
        gap = indent[:10]
    return _jsonText(value, gap, "", set())


def _rejectConstant(name: str) -> Any:
    raise ValueError(f"Unexpected token {name} in JSON")


def jsonParse(text: str) -> Any:
    """``JSON.parse``: ``NaN`` and ``Infinity`` are not JSON."""
    return json.loads(text, parse_constant=_rejectConstant)


def spawn(awaitable: Coroutine[Any, Any, Any], holder: set[asyncio.Task[Any]]) -> asyncio.Task[Any]:
    """``void asyncFunction()``: the call starts running right away, as a JavaScript async function
    does, and its failure is observed instead of left "never retrieved". ``holder`` keeps the task
    alive until it finishes (asyncio only holds tasks weakly)."""
    task = asyncio.Task(awaitable, loop=asyncio.get_running_loop(), eager_start=True)
    if not task.done():
        holder.add(task)
        task.add_done_callback(holder.discard)
    task.add_done_callback(_observe)
    return task


def startTask[T](awaitable: Awaitable[T]) -> asyncio.Future[T]:
    """Start an awaitable as a JavaScript promise starts: a coroutine runs at once, up to its first
    suspension, instead of waiting for the next turn of the event loop. A future or task is returned as is."""
    if asyncio.iscoroutine(awaitable):
        return asyncio.Task(awaitable, loop=asyncio.get_running_loop(), eager_start=True)
    return asyncio.ensure_future(awaitable)


def _observe(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


def eager[**P, T](function: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, asyncio.Future[T]]:
    """Mark an ``async def`` as running when it is called, as a JavaScript async function does: the call starts the body
    up to its first suspension and returns the future of its result."""

    @functools.wraps(function)
    def start(*args: P.args, **kwargs: P.kwargs) -> asyncio.Future[T]:
        return startTask(function(*args, **kwargs))

    return start


def queueMicrotask(callback: Callable[[], None]) -> None:
    """``queueMicrotask(callback)``: run ``callback`` on a later turn of the event loop, never inline."""
    asyncio.get_running_loop().call_soon(callback)


class Interval:
    """The handle ``setInterval`` returns: a timer that re-arms itself on the event loop that was running when it was
    set, or a daemon ``threading.Timer`` when no loop was running."""

    def __init__(self, callback: Callable[[], None], ms: float) -> None:
        self._callback = callback
        self._seconds = max(ms, 1) / 1000
        self._handle: asyncio.TimerHandle | Any | None = None
        self._cleared = False
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self._arm()

    def _arm(self) -> None:
        if self._loop is not None:
            self._handle = self._loop.call_later(self._seconds, self._tick)
            return
        import threading

        timer = threading.Timer(self._seconds, self._tick)
        timer.daemon = True
        self._handle = timer
        timer.start()

    def _tick(self) -> None:
        if self._cleared:
            return
        self._arm()
        self._callback()

    def cancel(self) -> None:
        self._cleared = True
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None


def setInterval(callback: Callable[[], None], ms: float) -> Interval:
    """``setInterval(callback, ms)``."""
    return Interval(callback, ms)


def clearInterval(interval: Interval | None) -> None:
    """``clearInterval(interval)``."""
    if interval is not None:
        interval.cancel()


_TIMEOUT_MAX: Final = 2**31 - 1


def _timerDelay(ms: Any) -> float:
    """Node's timer delay: one below 1 ms, NaN, or past the 32-bit signed range runs after 1 ms."""
    return ms if isNumber(ms) and 1 <= ms <= _TIMEOUT_MAX else 1


class Timeout:
    """The handle ``setTimeout`` returns: a one-shot timer on the event loop that was running when it was set, or a
    daemon ``threading.Timer`` when no loop was running."""

    def __init__(self, callback: Callable[[], None], ms: float) -> None:
        self._callback = callback
        self._cleared = False
        seconds = _timerDelay(ms) / 1000
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            import threading

            timer = threading.Timer(seconds, self._fire)
            timer.daemon = True
            self._handle: asyncio.TimerHandle | Any = timer
            timer.start()
            return
        self._handle = loop.call_later(seconds, self._fire)

    def _fire(self) -> None:
        if self._cleared:
            return
        self._cleared = True
        self._callback()

    def cancel(self) -> None:
        self._cleared = True
        self._handle.cancel()


def setTimeout(callback: Callable[[], None], ms: float = 0) -> Timeout:
    """``setTimeout(callback, ms)``."""
    return Timeout(callback, ms)


def clearTimeout(timeout: Timeout | None) -> None:
    """``clearTimeout(timeout)``."""
    if timeout is not None:
        timeout.cancel()


async def awaitValue(value: Any) -> Any:
    """``await value``, with the turns JavaScript's ``await`` takes: the caller resumes a turn after the awaited promise
    settles, even one that had settled already, and a value that is not awaitable comes back after one turn. An awaited
    coroutine is started at once and not run inline, so it settles like the promise of an async function."""
    if not inspect.isawaitable(value):
        await asyncio.sleep(0)
        return value
    future = startTask(value)
    if future.done():
        await asyncio.sleep(0)
    return await future


def rejectedPromise(error: BaseException) -> asyncio.Future[Any]:
    """``Promise.reject(error)``: a future that has already failed."""
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    future.set_exception(error)
    return future



def resolvedPromise(value: Any) -> asyncio.Future[Any]:
    """``Promise.resolve(value)``: a future that has already finished. An awaitable is started and returned."""
    if inspect.isawaitable(value):
        return startTask(value)
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    future.set_result(value)
    return future


class Settled:
    """One ``Promise.allSettled`` result: ``status`` is ``"fulfilled"`` with a ``value`` or ``"rejected"`` with a ``reason``."""

    __slots__ = ("reason", "status", "value")

    def __init__(self, status: str, value: Any = None, reason: BaseException | None = None) -> None:
        self.status = status
        self.value = value
        self.reason = reason


async def allSettled(awaitables: Iterable[Any]) -> list[Settled]:
    """``Promise.allSettled(values)``: every awaitable starts at once (a coroutine runs to its first suspension, as a
    JavaScript async function does) and a plain value counts as fulfilled with itself."""
    started = [startTask(item) if inspect.isawaitable(item) else item for item in awaitables]
    pending = [item for item in started if isinstance(item, asyncio.Future)]
    if pending:
        await asyncio.wait(pending)
    else:
        await asyncio.sleep(0)
    results: list[Settled] = []
    for item in started:
        if not isinstance(item, asyncio.Future):
            results.append(Settled("fulfilled", item))
        elif item.cancelled():
            results.append(Settled("rejected", reason=asyncio.CancelledError()))
        elif (error := item.exception()) is not None:
            results.append(Settled("rejected", reason=error))
        else:
            results.append(Settled("fulfilled", item.result()))
    return results


async def promiseAll(awaitables: Iterable[Any]) -> list[Any]:
    """``Promise.all(values)``: every awaitable starts at once, the first failure fails the whole, and the results keep
    their order."""
    started = [startTask(item) if inspect.isawaitable(item) else item for item in awaitables]
    pending = [item for item in started if isinstance(item, asyncio.Future)]
    if not pending:
        await asyncio.sleep(0)
        return started
    await asyncio.gather(*pending)
    return [item.result() if isinstance(item, asyncio.Future) else item for item in started]


class DOMException(Exception):
    """The ``DOMException`` an abort raises: ``name`` ``"AbortError"`` or ``"TimeoutError"``."""

    def __init__(self, message: str = "", name: str = "Error") -> None:
        super().__init__(message)
        self.message = message
        self.name = name


class AbortSignal:
    """``AbortSignal``: ``aborted``, ``reason``, ``throwIfAborted()``, and an awaitable ``wait()`` so
    misaka's abort helpers accept it."""

    def __init__(self) -> None:
        self.aborted = False
        self.reason: Any = None
        self._event = asyncio.Event()
        # A listener's return value is ignored, as a DOM event listener's is.
        self._listeners: list[Callable[[], object]] = []
        # Signals made by `AbortSignal.any` that follow this one; held weakly, as the DOM holds them, so a
        # long-lived signal does not keep every derived signal alive.
        self._dependents: weakref.WeakSet[AbortSignal] = weakref.WeakSet()

    async def wait(self) -> None:
        await self._event.wait()

    def addEventListener(self, type: str, listener: Callable[[], object], once: bool = False) -> None:
        """``addEventListener("abort", listener)``. A signal aborts once, so a listener fires at most once
        whatever ``once`` says, and one added after the abort never fires. Adding the same function twice
        registers it once."""
        if type == "abort" and not self.aborted and listener not in self._listeners:
            self._listeners.append(listener)

    def removeEventListener(self, type: str, listener: Callable[[], object]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _abort(self, reason: Any) -> None:
        if self.aborted:
            return
        self.reason = reason
        self.aborted = True
        self._event.set()
        listeners, self._listeners = self._listeners, []
        for listener in listeners:
            try:
                listener()
            except Exception:  # noqa: BLE001 - a listener's failure is reported, as a DOM event does, and the rest still run
                sys.excepthook(*sys.exc_info())
        for dependent in list(self._dependents):
            dependent._abort(reason)

    @staticmethod
    def any(signals: Iterable[Any]) -> AbortSignal:
        """``AbortSignal.any(signals)``: aborts with the reason of the first of ``signals`` that aborts."""
        combined = AbortSignal()
        sources = list(signals)
        for source in sources:
            if signal_aborted(source):
                combined._abort(abortReason(source))
                return combined
        for source in sources:
            dependents = getattr(source, "_dependents", None)
            if dependents is not None:
                dependents.add(combined)
            else:
                # A signal of another kind, which has no listeners: watch it.
                onAbort(source, partial(_abortFollower, combined, source))
        return combined

    def throwIfAborted(self) -> None:
        if self.aborted:
            raise self.reason if isinstance(self.reason, BaseException) else DOMException(jsString(self.reason))

    @staticmethod
    def timeout(ms: float) -> AbortSignal:
        """``AbortSignal.timeout(ms)``: aborts by itself with a ``TimeoutError`` once ``ms`` have passed. As in Node,
        ``ms`` is an unsigned 32-bit integer, and its timer runs a delay past the signed range after 1 ms."""
        if not isNumber(ms) or (isinstance(ms, float) and (math.isnan(ms) or (math.isfinite(ms) and not ms.is_integer()))):
            raise RangeError(f'The value of "delay" is out of range. It must be an integer. Received {formatNumber(ms)}')
        if not 0 <= ms <= 4_294_967_295:
            raise RangeError(
                f'The value of "delay" is out of range. It must be >= 0 && <= 4294967295. Received {formatNumber(ms)}'
            )
        controller = AbortController()
        handle = asyncio.get_running_loop().call_later(
            _timerDelay(ms) / 1000,
            lambda: controller.abort(DOMException("The operation was aborted due to timeout", "TimeoutError")),
        )
        controller.signal._timer = handle  # type: ignore[attr-defined]
        return controller.signal


class AbortController:
    """``AbortController``: the first ``abort(reason)`` wins; without a reason it is an ``AbortError``."""

    def __init__(self) -> None:
        self.signal = AbortSignal()

    def abort(self, reason: Any = UNDEFINED) -> None:
        self.signal._abort(DOMException("This operation was aborted", "AbortError") if reason is UNDEFINED else reason)


def onAbort(signal: Any, listener: Callable[[], None]) -> Callable[[], None]:
    """``signal.addEventListener("abort", listener, {once: true})`` for any signal misaka passes around
    (its controllers have no listeners): a watcher task calls ``listener`` once the signal aborts.
    Returns the matching ``removeEventListener``."""
    if signal is None:
        return lambda: None
    watcher = asyncio.ensure_future(wait_for_abort(signal))

    def fire(task: asyncio.Future[None]) -> None:
        if not task.cancelled() and task.exception() is None:
            listener()

    watcher.add_done_callback(fire)

    def remove() -> None:
        watcher.remove_done_callback(fire)
        watcher.cancel()

    return remove


def addAbortListener(signal: Any, listener: Callable[[], None]) -> Callable[[], None]:
    """``signal.addEventListener("abort", listener, {once: true})`` for any signal: a DOM-style signal (this module's
    ``AbortSignal``) gets the listener itself and calls it synchronously when it aborts; any other shape is watched by
    ``onAbort``. Returns the matching ``removeEventListener``."""
    add = getattr(signal, "addEventListener", None)
    if callable(add):
        add("abort", listener, once=True)
        return lambda: signal.removeEventListener("abort", listener)
    return onAbort(signal, listener)


def throwIfAborted(signal: Any) -> None:
    """``signal.throwIfAborted()`` for any signal: an aborted signal raises its ``reason``; one that carries no reason
    (misaka's flag-style controllers) raises the ``AbortError`` a reasonless ``controller.abort()`` would have given."""
    method = getattr(signal, "throwIfAborted", None)
    if callable(method):
        method()
        return
    if signal_aborted(signal):
        raise _abortError(signal)


def abortReason(signal: Any) -> Any:
    """``signal.reason`` for any signal; misaka's own carry none."""
    return getattr(signal, "reason", None)


def isAborted(signal: Any) -> bool:
    """``signal?.aborted``."""
    return signal_aborted(signal)


async def raceAbort[T](operation: Awaitable[T], signal: Any) -> T:
    """Await ``operation`` unless ``signal`` aborts first; an abort cancels the operation and raises
    the signal's reason, as an aborted fetch rejects with it."""
    task = asyncio.ensure_future(operation)
    if signal is None:
        return await task
    if signal_aborted(signal):
        task.cancel()
        raise _abortError(signal)
    aborting = asyncio.ensure_future(wait_for_abort(signal))
    try:
        await asyncio.wait({task, aborting}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        task.cancel()
        raise
    finally:
        aborting.cancel()
    if task.done():
        return task.result()
    task.cancel()
    raise _abortError(signal)


def _abortFollower(combined: AbortSignal, source: Any) -> None:
    combined._abort(abortReason(source))


def _abortError(signal: Any) -> BaseException:
    reason = getattr(signal, "reason", None)
    return reason if isinstance(reason, BaseException) else DOMException("This operation was aborted", "AbortError")


__all__ = [
    "UNDEFINED",
    "AbortController",
    "AbortSignal",
    "DOMException",
    "Interval",
    "RangeError",
    "Settled",
    "Timeout",
    "Utf8Decoder",
    "abortReason",
    "addAbortListener",
    "allSettled",
    "awaitValue",
    "bufferFromBase64",
    "clearInterval",
    "clearTimeout",
    "eager",
    "errorMessage",
    "formatNumber",
    "fromUtf16Units",
    "isAborted",
    "jsCall",
    "jsCollapseWhitespace",
    "jsIndex",
    "jsIsInteger",
    "jsIsSafeInteger",
    "jsLength",
    "jsNumber",
    "jsRound",
    "jsSlice",
    "jsString",
    "jsTrim",
    "jsTrimEnd",
    "jsTrimStart",
    "jsonParse",
    "jsonStringify",
    "liveKeys",
    "nullishCoalesce",
    "onAbort",
    "pathResolve",
    "promiseAll",
    "queueMicrotask",
    "raceAbort",
    "rejectedPromise",
    "resolvedPromise",
    "setInterval",
    "setTimeout",
    "spawn",
    "startTask",
    "throwIfAborted",
    "toFixed",
    "toPrecision",
    "toUtf16Units",
    "toWellFormed",
]
