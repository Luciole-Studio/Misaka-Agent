"""pi codemode's ``source.test.ts``."""

from __future__ import annotations

import re

import pytest

from misaka.codemode.source import CodemodeSourceError, parseCodemodeSource


def test_returns_plain_code_unchanged():
    assert parseCodemodeSource("text('hi')") == {"code": "text('hi')", "options": {}}
    assert parseCodemodeSource("// just a comment\nreturn 1") == {"code": "// just a comment\nreturn 1", "options": {}}


def test_parses_the_options_line_and_keeps_line_numbers():
    assert parseCodemodeSource('// @options: {"timeout_ms": 10}\nconst a = 1;\ntext(a)') == {
        "code": "\nconst a = 1;\ntext(a)",
        "options": {"timeoutMs": 10},
    }
    assert parseCodemodeSource('  // @options:{"max_output_tokens":0,"timeout_ms":1500}\r\ntext(1)')["options"] == {
        "maxOutputTokens": 0,
        "timeoutMs": 1500,
    }
    assert parseCodemodeSource("// @options: {}\ntext(1)") == {"code": "\ntext(1)", "options": {}}


def test_only_treats_the_first_line_as_an_options_line():
    assert parseCodemodeSource("// @optionsx {}\ntext(1)")["options"] == {}
    assert parseCodemodeSource('text("// @options: {}")')["options"] == {}


# #10631: an options line after the first line used to be ignored, so its timeout never applied.
def test_rejects_an_options_line_that_is_not_the_first_line():
    cases = [
        ('// count things\n// @options: {"timeout_ms": 1000}\nwhile (true) {}', 2),
        ('\n  // @options: {"timeout_ms": 1000}\nwhile (true) {}', 2),
        ('text(1)\r\n// @options: {"timeout_ms": 1}', 2),
        ('// @options: {"timeout_ms": 1}\ntext(1)\n// @options: {"timeout_ms": 2}', 3),
    ]
    for input, line in cases:
        with pytest.raises(
            CodemodeSourceError,
            match=re.escape(f"The // @options: line must be the first line of the input; found one on line {line}"),
        ):
            parseCodemodeSource(input)


def test_rejects_empty_input_and_invalid_options():
    cases = [
        ("", r"Expected JavaScript source text \(non-empty\)"),
        ("  \n", r"Expected JavaScript source text \(non-empty\)"),
        ("// @options:\ntext(1)", "@options must be a JSON object with supported fields"),
        ("// @options: {timeout_ms: 1}\ntext(1)", "@options must be valid JSON with supported fields"),
        ("// @options: [1]\ntext(1)", "@options must be a JSON object with supported fields"),
        ('// @options: {"yield": 1}\ntext(1)', re.escape("@options only supports `max_output_tokens` and `timeout_ms`; got `yield`")),
        ('// @options: {"max_output_tokens": 1.5}\ntext(1)', re.escape("@options field `max_output_tokens` must be a non-negative safe integer")),
        ('// @options: {"timeout_ms": 0}\ntext(1)', "@options field `timeout_ms` must be a positive integer"),
        ('// @options: {"timeout_ms": 1}', "The @options line must be followed by JavaScript source on subsequent lines"),
        ('// @options: {"timeout_ms": 1}\n  \n', "The @options line must be followed by JavaScript source on subsequent lines"),
    ]
    for input, message in cases:
        with pytest.raises(CodemodeSourceError, match=message):
            parseCodemodeSource(input)


# -- misaka regressions ------------------------------------------------------------------------


def test_safe_integer_follows_javascript_not_python():
    # A bool is a number to Python only; a whole float is a safe integer to both.
    with pytest.raises(CodemodeSourceError, match="non-negative safe integer"):
        parseCodemodeSource('// @options: {"max_output_tokens": true}\ntext(1)')
    assert parseCodemodeSource('// @options: {"max_output_tokens": 2.0}\ntext(1)')["options"] == {"maxOutputTokens": 2}
    with pytest.raises(CodemodeSourceError, match="non-negative safe integer"):
        parseCodemodeSource('// @options: {"max_output_tokens": 9007199254740992}\ntext(1)')
