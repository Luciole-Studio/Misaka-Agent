"""MISAKA fork: the message V8's ``JSON.parse`` throws for a text that is not JSON.

Python's ``json`` fails on the same texts with its own wording. Where pi shows the ``SyntaxError`` message to the user
(a broken ``mcp.json``), :func:`jsonParseErrorMessage` gives V8's: it walks the text as V8's ``JsonParser`` does
(Node 24) and reports the first error it finds, with positions in UTF-16 code units and the line and column Node adds.
"""

from __future__ import annotations

from typing import Any

from misaka.utils.js import jsonParse as _jsonParse

_WHITESPACE = frozenset((0x20, 0x09, 0x0A, 0x0D))
_DIGITS = frozenset(range(0x30, 0x3A))
_HEX = frozenset((*range(0x30, 0x3A), *range(0x41, 0x47), *range(0x61, 0x67)))
_ESCAPES = frozenset(map(ord, '"\\/bfnrtu'))
_MAX_CONTEXT_CHARACTERS = 10
_MIN_ORIGINAL_SOURCE_LENGTH_FOR_CONTEXT = _MAX_CONTEXT_CHARACTERS * 2 + 1
_SPECIAL_STRINGS = frozenset(("[object Object]", "undefined", "Infinity", "NaN"))

# Tokens, by the first character of what follows (V8's `one_char_json_tokens`).
_EOS, _STRING, _NUMBER, _LBRACE, _RBRACE, _LBRACK, _RBRACK, _COLON, _COMMA, _LITERAL, _ILLEGAL = range(11)


class _ParseError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _token(unit: int | None) -> int:
    if unit is None:
        return _EOS
    if unit == 0x22:
        return _STRING
    if unit == 0x2D or unit in _DIGITS:
        return _NUMBER
    return {0x7B: _LBRACE, 0x7D: _RBRACE, 0x5B: _LBRACK, 0x5D: _RBRACK, 0x3A: _COLON, 0x2C: _COMMA}.get(
        unit, _LITERAL if unit in (0x74, 0x66, 0x6E) else _ILLEGAL
    )


class _Parser:
    def __init__(self, text: str) -> None:
        raw = text.encode("utf-16-le", "surrogatepass")
        self.units = [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]
        self.text = text
        self.cursor = 0

    def peekUnit(self) -> int | None:
        return self.units[self.cursor] if self.cursor < len(self.units) else None

    def skipWhitespace(self) -> int:
        while self.cursor < len(self.units) and self.units[self.cursor] in _WHITESPACE:
            self.cursor += 1
        return _token(self.peekUnit())

    def slice(self, start: int, end: int) -> str:
        units = self.units[max(start, 0) : min(end, len(self.units))]
        return b"".join(unit.to_bytes(2, "little") for unit in units).decode("utf-16-le", "surrogatepass")

    def location(self, pos: int) -> str:
        """``CalculateFileLocation``: CR, LF and CRLF each end a line."""
        line = 1
        lastLineBreak = 0
        cursor = 0
        while cursor < pos:
            if self.units[cursor] == 0x0D and cursor < pos - 1 and self.units[cursor + 1] == 0x0A:
                cursor += 1
            if self.units[cursor] in (0x0D, 0x0A):
                line += 1
                lastLineBreak = cursor + 1
            cursor += 1
        return f"(line {line} column {1 + cursor - lastLineBreak})"

    def fail(self, token: int, message: str | None = None) -> _ParseError:
        """``ReportUnexpectedToken(token, errorMessage)``: the error at the cursor."""
        pos = self.cursor
        at = f"at position {pos} {self.location(pos)}"
        if message is not None:
            return _ParseError(f"{message} {at}")
        if token == _EOS:
            return _ParseError("Unexpected end of JSON input")
        if token == _NUMBER:
            return _ParseError(f"Unexpected number in JSON {at}")
        if token == _STRING:
            return _ParseError(f"Unexpected string in JSON {at}")
        if self.text in _SPECIAL_STRINGS:
            return _ParseError(f'"{self.text}" is not valid JSON')
        character = self.slice(pos, pos + 1)
        length = len(self.units)
        if length < _MIN_ORIGINAL_SOURCE_LENGTH_FOR_CONTEXT:
            return _ParseError(f"Unexpected token '{character}', \"{self.text}\" is not valid JSON")
        if pos < _MAX_CONTEXT_CHARACTERS:
            context = self.slice(0, pos + _MAX_CONTEXT_CHARACTERS)
            return _ParseError(f"Unexpected token '{character}', \"{context}\"... is not valid JSON")
        if pos < length - _MAX_CONTEXT_CHARACTERS:
            context = self.slice(pos - _MAX_CONTEXT_CHARACTERS, pos + _MAX_CONTEXT_CHARACTERS)
            return _ParseError(f"Unexpected token '{character}', ...\"{context}\"... is not valid JSON")
        context = self.slice(pos - _MAX_CONTEXT_CHARACTERS, length)
        return _ParseError(f"Unexpected token '{character}', ...\"{context}\" is not valid JSON")

    def expect(self, token: int, message: str) -> None:
        """``ExpectNext(token, message)``: skip whitespace, then the token or the error."""
        if self.skipWhitespace() != token:
            raise self.fail(_token(self.peekUnit()), message)
        if token == _STRING:
            self.scanString()
        else:
            self.cursor += 1

    def scanString(self) -> None:
        self.cursor += 1
        while True:
            unit = self.peekUnit()
            if unit is None:
                raise self.fail(_EOS, "Unterminated string in JSON")
            if unit == 0x22:
                self.cursor += 1
                return
            if unit < 0x20:
                raise self.fail(_ILLEGAL, "Bad control character in string literal in JSON")
            if unit != 0x5C:
                self.cursor += 1
                continue
            self.cursor += 1
            escape = self.peekUnit()
            if escape is None:
                raise self.fail(_EOS)
            if escape >= 0x80:
                # The escape table covers ASCII only; anything past it is an unexpected token.
                raise self.fail(_ILLEGAL)
            if escape not in _ESCAPES:
                raise self.fail(_token(escape), "Bad escaped character in JSON")
            self.cursor += 1
            if escape == 0x75:
                for _ in range(4):
                    digit = self.peekUnit()
                    if digit is None or digit not in _HEX:
                        raise self.fail(_token(digit), "Bad Unicode escape in JSON")
                    self.cursor += 1

    def digits(self) -> None:
        while self.peekUnit() in _DIGITS:
            self.cursor += 1

    def scanNumber(self) -> None:
        if self.peekUnit() == 0x2D:
            self.cursor += 1
            if self.peekUnit() not in _DIGITS:
                raise self.fail(_token(self.peekUnit()), "No number after minus sign in JSON")
        if self.peekUnit() == 0x30:
            self.cursor += 1
            if self.peekUnit() in _DIGITS:
                raise self.fail(_NUMBER)
        else:
            self.digits()
        if self.peekUnit() == 0x2E:
            self.cursor += 1
            if self.peekUnit() not in _DIGITS:
                raise self.fail(_token(self.peekUnit()), "Unterminated fractional number in JSON")
            self.digits()
        if self.peekUnit() in (0x65, 0x45):
            self.cursor += 1
            if self.peekUnit() in (0x2B, 0x2D):
                self.cursor += 1
            if self.peekUnit() not in _DIGITS:
                raise self.fail(_token(self.peekUnit()), "Exponent part is missing a number in JSON")
            self.digits()

    def scanLiteral(self, literal: str) -> None:
        for expected in map(ord, literal):
            unit = self.peekUnit()
            if unit != expected:
                raise self.fail(_token(unit))
            self.cursor += 1

    def parse(self) -> None:
        # Open containers, innermost last: True for an object, False for an array.
        stack: list[bool] = []
        while True:
            # A value.
            token = self.skipWhitespace()
            unit = self.peekUnit()
            if token == _STRING:
                self.scanString()
            elif token == _NUMBER:
                self.scanNumber()
            elif token == _LBRACE:
                self.cursor += 1
                if self.skipWhitespace() == _RBRACE:
                    self.cursor += 1
                else:
                    self.expect(_STRING, "Expected property name or '}' in JSON")
                    self.expect(_COLON, "Expected ':' after property name in JSON")
                    stack.append(True)
                    continue
            elif token == _LBRACK:
                self.cursor += 1
                if self.skipWhitespace() == _RBRACK:
                    self.cursor += 1
                else:
                    stack.append(False)
                    continue
            elif token == _LITERAL and unit is not None:
                self.scanLiteral({0x74: "true", 0x66: "false", 0x6E: "null"}[unit])
            else:
                raise self.fail(token)
            # Close containers the value completed.
            while stack:
                following = self.skipWhitespace()
                if stack[-1]:
                    if following == _COMMA:
                        self.cursor += 1
                        self.expect(_STRING, "Expected double-quoted property name in JSON")
                        self.expect(_COLON, "Expected ':' after property name in JSON")
                        break
                    if following != _RBRACE:
                        raise self.fail(following, "Expected ',' or '}' after property value in JSON")
                else:
                    if following == _COMMA:
                        self.cursor += 1
                        break
                    if following != _RBRACK:
                        raise self.fail(following, "Expected ',' or ']' after array element in JSON")
                self.cursor += 1
                stack.pop()
            else:
                if self.skipWhitespace() != _EOS:
                    raise self.fail(_token(self.peekUnit()), "Unexpected non-whitespace character after JSON")
                return


class JsonSyntaxError(ValueError):
    """The ``SyntaxError`` ``JSON.parse`` throws, with V8's message."""


def jsonParse(text: str) -> Any:
    """``JSON.parse(text)``: :func:`misaka.utils.js.jsonParse`, whose syntax errors read as V8's."""
    try:
        return _jsonParse(text)
    except ValueError as error:
        message = jsonParseErrorMessage(text)
        if message is None:
            raise
        raise JsonSyntaxError(message) from error


def jsonParseErrorMessage(text: str) -> str | None:
    """The message of the ``SyntaxError`` ``JSON.parse(text)`` throws, or ``None`` when ``text`` is JSON."""
    try:
        _Parser(text).parse()
    except _ParseError as error:
        return error.message
    return None


__all__ = ["JsonSyntaxError", "jsonParse", "jsonParseErrorMessage"]
