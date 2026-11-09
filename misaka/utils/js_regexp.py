"""MISAKA fork: JavaScript's ``RegExp`` and the ``String.prototype`` methods that take one, over the ``regex`` module.

Code ported line for line from TypeScript keeps its regex literals as JavaScript sources, so a pattern reads the same
on both sides and code that builds patterns from other patterns' ``source`` (marked's ``edit()``) works unchanged.
The source is translated to an equivalent Python pattern once, where the two dialects differ:

- ``$`` without the ``m`` flag is the end of the input (Python's ``$`` also matches before a final newline); with
  ``m``, ``^`` and ``$`` match at every ECMAScript line terminator, not only ``\\n``.
- ``.`` without the ``s`` flag excludes every line terminator (``\\n``, ``\\r``, U+2028, U+2029).
- ``\\s``/``\\S`` are the ECMAScript whitespace set; ``\\w``, ``\\d`` and ``\\b`` are ASCII (spelled out: the ``regex``
  module's ASCII flag would also confine ``\\p{...}`` to ASCII).
- ``(?<name>...)`` and ``\\k<name>`` are named groups; ``\\p{...}`` property classes need the ``regex`` module.
- A ``{`` that does not start a quantifier is a literal, as in JavaScript's non-unicode mode.
- A backreference (``\\N``, ``\\k<name>``) to a group that has not taken part in the match (later in the pattern, in
  another alternative, not matched, or still open: a group's capture is set when it closes) matches the empty string,
  as in JavaScript (``(?(N)\\N)``; inside its own group, nothing). Without ``u``/``v``, Annex B's readings: ``\\N``
  beyond the group count is a legacy octal escape (``\\8``/``\\9`` themselves), ``\\0`` followed by digits too,
  and ``\\k`` is the letter in a pattern without named groups.

Not emulated: JavaScript clears the captures inside a quantified atom at each iteration, and rejects an iteration
of a ``*``/``?``/``{0,}`` quantifier that matches the empty string (its groups then stay ``undefined``); here a group
keeps an earlier iteration's capture (``/(?:(a)|b)+/`` on ``"ab"`` gives ``"a"`` for group 1 where JavaScript gives
``undefined``), and a backreference to it sees that capture.

Strings are Python strings: indices and lengths count code points where JavaScript counts UTF-16 code units, which
only differs for characters outside the Basic Multilingual Plane. ``g`` and ``y`` keep ``lastIndex`` as JavaScript
does. A group that did not take part in a match reads as ``None`` (JavaScript's ``undefined``).
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator
from typing import Any

import regex

from misaka.utils.js import jsCall, jsString

# ECMAScript WhiteSpace and LineTerminator, as a character class body.
_WHITESPACE = "\\t\\n\\x0b\\x0c\\r \\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff"
_LINE_TERMINATORS = "\\n\\r\\u2028\\u2029"
_SIMPLE_ESCAPES = {"n": "\\n", "r": "\\r", "t": "\\t", "f": "\\x0c", "v": "\\x0b"}
_WORD = "A-Za-z0-9_"
# `\d` and `\w` inside a character class.
_CLASS_ESCAPES = {"d": "0-9", "w": _WORD}
# Class escapes that stand for a set, so a `-` beside them cannot form a range.
_CLASS_ESCAPE_ATOMS = frozenset({"\\d", "\\D", "\\w", "\\W", "\\s", "\\S"})
_ESCAPES = {
    "d": "[0-9]",
    "D": "[^0-9]",
    "w": f"[{_WORD}]",
    "W": f"[^{_WORD}]",
    "b": f"(?:(?<=[{_WORD}])(?![{_WORD}])|(?<![{_WORD}])(?=[{_WORD}]))",
    "B": f"(?:(?<=[{_WORD}])(?=[{_WORD}])|(?<![{_WORD}])(?![{_WORD}]))",
}


def _isDigits(text: str) -> bool:
    r"""ECMAScript's DecimalDigits: ASCII ``0``-``9`` only, as ``\d`` reads them. ``str.isdigit`` also takes ``²``,
    ``٣`` and every other Unicode digit, which JavaScript reads as plain characters (identity escapes)."""
    return bool(text) and all(char in "0123456789" for char in text)


def _readQuantifier(source: str, index: int) -> int:
    """The end of a ``{n}``, ``{n,}`` or ``{n,m}`` quantifier starting at ``index``, or -1 when it is not one."""
    end = source.find("}", index)
    if end < 0:
        return -1
    body = source[index + 1 : end]
    low, comma, high = body.partition(",")
    if not _isDigits(low) or (comma and high and not _isDigits(high)):
        return -1
    return end + 1


def _escape(source: str, index: int, inClass: bool, unicode: bool) -> tuple[int, str]:
    """Translate the escape at ``index`` (the backslash); returns the index after it and the Python text."""
    char = source[index + 1] if index + 1 < len(source) else ""
    after = index + 2
    if inClass and char in _CLASS_ESCAPES:
        return after, _CLASS_ESCAPES[char]
    if inClass and char in ("D", "W"):
        raise ValueError(f"\\{char} inside a character class is not supported: {source!r}")
    if not inClass and char in _ESCAPES:
        return after, _ESCAPES[char]
    if char == "s":
        return after, _WHITESPACE if inClass else f"[{_WHITESPACE}]"
    if char == "S":
        if inClass:
            raise ValueError(f"\\S inside a character class is not supported: {source!r}")
        return after, f"[^{_WHITESPACE}]"
    if inClass and char == "b":
        return after, "\\x08"
    if char in ("p", "P") and unicode and source[after : after + 1] == "{":
        end = source.index("}", after)
        return end + 1, source[index : end + 1]
    if char == "k" and source[after : after + 1] == "<" and not inClass:
        end = source.index(">", after)
        return end + 1, f"(?P={source[after + 1 : end]})"
    if char == "u":
        if unicode and source[after : after + 1] == "{":
            end = source.index("}", after)
            return end + 1, f"\\U{int(source[after + 1 : end], 16):08x}"
        hexDigits = source[after : after + 4]
        if len(hexDigits) == 4 and all(digit in "0123456789abcdefABCDEF" for digit in hexDigits):
            return after + 4, "\\u" + hexDigits
        return after, "u"
    if char == "x":
        hexDigits = source[after : after + 2]
        if len(hexDigits) == 2 and all(digit in "0123456789abcdefABCDEF" for digit in hexDigits):
            return after + 2, "\\x" + hexDigits
        return after, "x"
    if char == "c" and source[after : after + 1].isalpha():
        return after + 1, f"\\x{ord(source[after]) % 32:02x}"
    if char == "0" and not _isDigits(source[after : after + 1]):
        return after, "\\x00"
    if _isDigits(char):
        if inClass:
            raise ValueError(f"a backreference inside a character class is not supported: {source!r}")
        end = after
        while end < len(source) and _isDigits(source[end]):
            end += 1
        return end, "\\" + source[index + 1 : end]
    if char in _SIMPLE_ESCAPES:
        return after, _SIMPLE_ESCAPES[char]
    if char.isalnum() or char == "_":
        # An identity escape of a letter is the letter itself (non-unicode mode).
        return after, regex.escape(char)
    if char == "/":
        return after, "/"
    return after, "\\" + char


def _characterClass(source: str, index: int, unicode: bool) -> tuple[int, str]:
    """Translate the class starting at ``index`` (the ``[``); returns the index after it and the Python text."""
    position = index + 1
    negated = source[position : position + 1] == "^"
    if negated:
        position += 1
    body: list[str] = []
    raw: list[str] = []
    while position < len(source) and source[position] != "]":
        char = source[position]
        if char == "\\":
            end, piece = _escape(source, position, True, unicode) if source[position + 1 : position + 2] != "S" else (
                position + 2,
                "\\S",
            )
            raw.append(source[position:end])
            body.append(piece)
            position = end
            continue
        raw.append(char)
        if char == "-" and (
            (raw[-2:-1] and raw[-2] in _CLASS_ESCAPE_ATOMS)
            or (source[position + 1 : position + 2] == "\\" and source[position + 2 : position + 3] in "dDwWsS")
        ):
            # Annex B: a `-` next to a class escape such as `\s` is a literal hyphen, not a range.
            body.append("\\-")
        else:
            body.append("\\[" if char == "[" else char)
        position += 1
    if position >= len(source):
        raise ValueError(f"unterminated character class: {source!r}")
    if "\\S" in body:
        if sorted(raw) != ["\\S", "\\s"] or negated:
            raise ValueError(f"\\S inside a character class is not supported: {source!r}")
        return position + 1, "[\\s\\S]"
    if not body:
        # `[]` matches nothing and `[^]` matches any character.
        return position + 1, "(?s:.)" if negated else "(?!)"
    return position + 1, "[" + ("^" if negated else "") + "".join(body) + "]"


def _characterClassV(source: str, index: int) -> tuple[int, str]:
    """A ``v``-mode class starting at ``index``: nested classes and the ``--``/``&&`` operators, as ``regex``'s V1
    sets spell them. Returns the index after it and the Python text."""
    position = index + 1
    negated = source[position : position + 1] == "^"
    if negated:
        position += 1
    body: list[str] = []
    while position < len(source) and source[position] != "]":
        char = source[position]
        if char == "[":
            position, nested = _characterClassV(source, position)
            body.append(nested)
        elif source.startswith("--", position) or source.startswith("&&", position):
            body.append(source[position : position + 2])
            position += 2
        elif char == "\\":
            escaped = source[position + 1 : position + 2]
            if escaped in ("S", "W", "D"):
                # A complemented escape is a nested negated set here.
                body.append({"S": f"[^{_WHITESPACE}]", "W": f"[^{_WORD}]", "D": "[^0-9]"}[escaped])
                position += 2
            else:
                position, piece = _escape(source, position, True, True)
                body.append(piece)
        else:
            body.append(char)
            position += 1
    if position >= len(source):
        raise ValueError(f"unterminated character class: {source!r}")
    return position + 1, "[" + ("^" if negated else "") + "".join(body) + "]"


def _skipClass(source: str, index: int, vMode: bool) -> int:
    """The index after the character class starting at ``index`` (the ``[``), for ``_captures``."""
    position = index + 1
    while position < len(source) and source[position] != "]":
        if source[position] == "\\":
            position += 2
        elif source[position] == "[" and vMode:
            position = _skipClass(source, position, vMode)
        else:
            position += 1
    return position + 1


def _captures(source: str, vMode: bool) -> list[str | None]:
    """The capturing groups of a pattern, in number order: each one's name, or ``None`` (``CountLeftCapturingParens``
    and the group names, which decide what ``\\N`` and ``\\k<name>`` mean)."""
    groups: list[str | None] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
        elif char == "[":
            index = _skipClass(source, index, vMode)
        elif char == "(":
            if source.startswith("(?<", index) and source[index + 3 : index + 4] not in ("=", "!"):
                end = source.find(">", index)
                groups.append(source[index + 3 : end] if end > 0 else "")
            elif not source.startswith("(?", index):
                groups.append(None)
            index += 1
        else:
            index += 1
    return groups


def _legacyOctal(source: str, index: int) -> tuple[int, str]:
    """Annex B's ``LegacyOctalEscapeSequence`` or ``IdentityEscape`` for a ``\\`` and digits that name no group
    (non-unicode mode): up to three octal digits (two after ``4``-``7``), or an ``8``/``9`` as itself."""
    first = source[index + 1]
    if first in "89":
        return index + 2, first
    limit = 3 if first in "0123" else 2
    end = index + 1
    while end < len(source) and end - index - 1 < limit and source[end] in "01234567":
        end += 1
    return end, f"\\x{int(source[index + 1 : end], 8):02x}"


def _backreference(number: int, name: str | None, opened: list[int | None]) -> str:
    """A backreference with JavaScript's meaning: to a group that has not taken part in the match (later in the
    pattern, in another alternative, not matched) it matches the empty string, and inside its own group, whose capture
    is set only when it closes, too."""
    if number in opened:
        return "(?:)"
    reference = f"(?P={name})" if name else f"\\{number}"
    return f"(?({number}){reference})"


def _translate(source: str, flags: str) -> str:
    multiline = "m" in flags
    dotAll = "s" in flags
    unicode = "u" in flags or "v" in flags
    vMode = "v" in flags
    groups = _captures(source, vMode)
    names = {name: number for number, name in enumerate(groups, 1) if name}
    # The groups open at this point of the source (their numbers; None for a group that does not capture).
    opened: list[int | None] = []
    seen = 0
    out: list[str] = []
    index = 0
    while index < len(source):
        char = source[index]
        following = source[index + 1 : index + 2]
        if char == "\\" and _isDigits(following) and following != "0":
            end = index + 1
            while end < len(source) and _isDigits(source[end]):
                end += 1
            number = int(source[index + 1 : end])
            if number <= len(groups):
                out.append(_backreference(number, None, opened))
                index = end
            elif unicode:
                raise ValueError(f"invalid escape (no group {number}): {source!r}")
            else:
                index, piece = _legacyOctal(source, index)
                out.append(piece)
        elif char == "\\" and following == "0" and _isDigits(source[index + 2 : index + 3]) and not unicode:
            index, piece = _legacyOctal(source, index)
            out.append(piece)
        elif char == "\\" and following == "k":
            if not names and not unicode:
                # Annex B: without named groups, `\k` is an identity escape.
                out.append("k")
                index += 2
                continue
            end = source.find(">", index)
            name = source[index + 3 : end] if source[index + 2 : index + 3] == "<" and end > 0 else None
            if name not in names:
                raise ValueError(f"invalid named reference: {source!r}")
            out.append(_backreference(names[name], name, opened))
            index = end + 1
        elif char == "\\":
            index, piece = _escape(source, index, False, unicode)
            out.append(piece)
        elif char == "[":
            index, piece = _characterClassV(source, index) if vMode else _characterClass(source, index, unicode)
            out.append(piece)
        elif char == "(":
            if source.startswith("(?<", index) and source[index + 3 : index + 4] not in ("=", "!"):
                seen += 1
                opened.append(seen)
                out.append("(?P<")
                index += 3
            else:
                if not source.startswith("(?", index):
                    seen += 1
                    opened.append(seen)
                else:
                    opened.append(None)
                out.append("(")
                index += 1
        elif char == ")":
            if opened:
                opened.pop()
            out.append(")")
            index += 1
        elif char == ".":
            out.append("(?s:.)" if dotAll else f"[^{_LINE_TERMINATORS}]")
            index += 1
        elif char == "$":
            out.append(f"(?=[{_LINE_TERMINATORS}]|\\Z)" if multiline else "\\Z")
            index += 1
        elif char == "^":
            out.append(f"(?:(?<=[{_LINE_TERMINATORS}])|\\A)" if multiline else "\\A")
            index += 1
        elif char == "{":
            end = _readQuantifier(source, index)
            if end < 0:
                out.append("\\{")
                index += 1
            else:
                out.append(source[index:end])
                index = end
        elif char == "}":
            out.append("\\}")
            index += 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


@functools.lru_cache(maxsize=4096)
def _compile(source: str, flags: str) -> Any:
    pythonFlags = 0
    if "i" in flags:
        pythonFlags |= regex.IGNORECASE
    if "v" in flags:
        # Nested classes and set operations.
        pythonFlags |= regex.V1
    return regex.compile(_translate(source, flags), pythonFlags)


class RegExpExecArray(list[Any]):
    """What ``RegExp.prototype.exec`` returns: the match and its groups (``None`` for a group that did not
    participate), with ``index``, ``input`` and ``groups``."""

    # A JavaScript match array's `index` property, which shadows `list.index` on purpose.
    index: int  # type: ignore[assignment]
    input: str
    groups: dict[str, Any] | None

    def __init__(self, match: Any, string: str) -> None:
        super().__init__([match.group(0), *match.groups()])
        self.index = match.start()
        self.input = string
        names = match.groupdict()
        self.groups = dict(names) if names else None

    def __getitem__(self, key: Any) -> Any:
        # An index past the groups reads as `undefined`, as on a JavaScript array.
        if isinstance(key, int) and not 0 <= key < len(self):
            return None
        return super().__getitem__(key)


class RegExp:
    """A JavaScript regular expression: ``RegExp(source, flags)``, or ``RegExp(other)`` to copy one."""

    source: str
    flags: str
    lastIndex: int

    def __init__(self, pattern: str | RegExp, flags: str | None = None) -> None:
        if isinstance(pattern, RegExp):
            source = pattern.source
            flags = pattern.flags if flags is None else flags
        else:
            source = pattern
        self.source = source
        self.flags = "".join(sorted(flags or ""))
        self.lastIndex = 0
        self._pattern = _compile(source, self.flags)

    @property
    def global_(self) -> bool:
        return "g" in self.flags

    @property
    def sticky(self) -> bool:
        return "y" in self.flags

    @property
    def ignoreCase(self) -> bool:
        return "i" in self.flags

    @property
    def multiline(self) -> bool:
        return "m" in self.flags

    @property
    def unicode(self) -> bool:
        return "u" in self.flags

    def exec(self, string: str) -> RegExpExecArray | None:
        if not (self.global_ or self.sticky):
            match = self._pattern.search(string)
            return RegExpExecArray(match, string) if match is not None else None
        if self.lastIndex > len(string):
            self.lastIndex = 0
            return None
        match = (
            self._pattern.match(string, self.lastIndex)
            if self.sticky
            else self._pattern.search(string, self.lastIndex)
        )
        if match is None:
            self.lastIndex = 0
            return None
        self.lastIndex = match.end()
        return RegExpExecArray(match, string)

    def test(self, string: str) -> bool:
        return self.exec(string) is not None

    def __repr__(self) -> str:
        return f"/{self.source}/{self.flags}"


def _expandReplacement(replacement: str, match: RegExpExecArray, string: str) -> str:
    """``GetSubstitution``: the ``$$``, ``$&``, ``$```, ``$'``, ``$n``/``$nn`` and ``$<name>`` patterns."""
    out: list[str] = []
    index = 0
    groupCount = len(match) - 1
    while index < len(replacement):
        char = replacement[index]
        if char != "$" or index + 1 >= len(replacement):
            out.append(char)
            index += 1
            continue
        following = replacement[index + 1]
        if following == "$":
            out.append("$")
            index += 2
        elif following == "&":
            out.append(match[0])
            index += 2
        elif following == "`":
            out.append(string[: match.index])
            index += 2
        elif following == "'":
            out.append(string[match.index + len(match[0]) :])
            index += 2
        elif _isDigits(following):
            twoDigits = replacement[index + 1 : index + 3]
            if len(twoDigits) == 2 and _isDigits(twoDigits) and 1 <= int(twoDigits) <= groupCount:
                out.append(match[int(twoDigits)] or "")
                index += 3
            elif 1 <= int(following) <= groupCount:
                out.append(match[int(following)] or "")
                index += 2
            else:
                out.append("$")
                index += 1
        elif following == "<" and match.groups is not None:
            end = replacement.find(">", index + 2)
            if end < 0:
                out.append("$")
                index += 1
            else:
                out.append(match.groups.get(replacement[index + 2 : end]) or "")
                index = end + 1
        else:
            out.append("$")
            index += 1
    return "".join(out)


def _substitute(match: RegExpExecArray, string: str, replacement: str | Callable[..., Any]) -> str:
    if callable(replacement):
        args: list[Any] = [*match, match.index, string]
        if match.groups is not None:
            args.append(match.groups)
        return jsString(jsCall(replacement, *args))
    return _expandReplacement(replacement, match, string)


def _advance(string: str, index: int) -> int:
    return index + 1


def _allMatches(pattern: RegExp, string: str) -> Iterator[RegExpExecArray]:
    """The matches a global pattern finds from the start, stepping past empty matches as JavaScript does."""
    pattern.lastIndex = 0
    while True:
        match = pattern.exec(string)
        if match is None:
            return
        if match[0] == "":
            pattern.lastIndex = _advance(string, pattern.lastIndex)
        yield match


def replace(string: str, pattern: RegExp | str, replacement: str | Callable[..., Any]) -> str:
    """``string.replace(pattern, replacement)``: every match for a global pattern, else the first."""
    if isinstance(pattern, str):
        found = string.find(pattern)
        if found < 0:
            return string
        literal = _LiteralMatch(pattern, found, string)
        return string[:found] + _substitute(literal, string, replacement) + string[found + len(pattern) :]
    if pattern.global_:
        # As @@replace does: every match is found before any replacer runs, so a replacer may use the same pattern.
        matches = list(_allMatches(pattern, string))
        out: list[str] = []
        last = 0
        for match in matches:
            out.append(string[last : match.index])
            out.append(_substitute(match, string, replacement))
            last = match.index + len(match[0])
        out.append(string[last:])
        return "".join(out)
    first = pattern.exec(string)
    if first is None:
        return string
    return string[: first.index] + _substitute(first, string, replacement) + string[first.index + len(first[0]) :]


class _LiteralMatch(RegExpExecArray):
    def __init__(self, text: str, index: int, string: str) -> None:
        list.__init__(self, [text])
        self.index = index
        self.input = string
        self.groups = None


def match(string: str, pattern: RegExp) -> RegExpExecArray | list[str] | None:
    """``string.match(pattern)``: the exec result, or every match's text for a global pattern."""
    if not pattern.global_:
        return pattern.exec(string)
    found = [item[0] for item in _allMatches(pattern, string)]
    return found or None


def matchAll(string: str, pattern: RegExp) -> Iterator[RegExpExecArray]:
    """``string.matchAll(pattern)``: a global pattern's matches, on a copy so ``lastIndex`` is not disturbed."""
    if not pattern.global_:
        raise TypeError("String.prototype.matchAll called with a non-global RegExp argument")
    copy = RegExp(pattern)
    copy.lastIndex = pattern.lastIndex
    while True:
        found = copy.exec(string)
        if found is None:
            return
        if found[0] == "":
            copy.lastIndex = _advance(string, copy.lastIndex)
        yield found


def search(string: str, pattern: RegExp) -> int:
    """``string.search(pattern)``: the index of the first match, or -1."""
    found = pattern._pattern.search(string)
    return found.start() if found is not None else -1


def split(string: str, separator: RegExp | str, limit: int | None = None) -> list[str]:
    """``string.split(separator, limit)``, including the groups a pattern captures."""
    if limit == 0:
        return []
    if isinstance(separator, str):
        parts = string.split(separator) if separator else list(string)
        return parts if limit is None else parts[:limit]
    # The specification's @@split: a sticky match at each position q; an empty match at the last cut moves on.
    out: list[Any] = []
    size = len(string)
    if size == 0:
        return [] if separator._pattern.match(string, 0) is not None else [string]
    last = 0
    position = 0
    while position < size:
        # A search finds the first position from here where the sticky match succeeds, as stepping does.
        found = separator._pattern.search(string, position)
        if found is None or found.start() >= size:
            break
        position = found.start()
        end = min(found.end(), size)
        if end == last:
            position += 1
            continue
        out.append(string[last:position])
        if limit is not None and len(out) == limit:
            return out
        last = end
        for group in found.groups():
            out.append(group)
            if limit is not None and len(out) == limit:
                return out
        position = last
    out.append(string[last:size])
    return out


__all__ = ["RegExp", "RegExpExecArray", "match", "matchAll", "replace", "search", "split"]
