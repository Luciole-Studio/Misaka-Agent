"""MISAKA fork: ``misaka.utils.v8_json`` gives V8's ``JSON.parse`` messages. The expected texts are Node 24's."""

from __future__ import annotations

import pytest

from misaka.utils.v8_json import JsonSyntaxError, jsonParse, jsonParseErrorMessage


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "Unexpected end of JSON input"),
        ("{", "Expected property name or '}' in JSON at position 1 (line 1 column 2)"),
        ('{"a"}', "Expected ':' after property name in JSON at position 4 (line 1 column 5)"),
        ('{"a":}', 'Unexpected token \'}\', "{"a":}" is not valid JSON'),
        ('{"a":1,}', "Expected double-quoted property name in JSON at position 7 (line 1 column 8)"),
        ("[1 2]", "Expected ',' or ']' after array element in JSON at position 3 (line 1 column 4)"),
        ('{"a":1 "b":2}', "Expected ',' or '}' after property value in JSON at position 7 (line 1 column 8)"),
        ("truex", "Unexpected non-whitespace character after JSON at position 4 (line 1 column 5)"),
        ("01", "Unexpected number in JSON at position 1 (line 1 column 2)"),
        ("-a", "No number after minus sign in JSON at position 1 (line 1 column 2)"),
        ("1.", "Unterminated fractional number in JSON at position 2 (line 1 column 3)"),
        ("1e+", "Exponent part is missing a number in JSON at position 3 (line 1 column 4)"),
        ("NaN", '"NaN" is not valid JSON'),
        ('"abc', "Unterminated string in JSON at position 4 (line 1 column 5)"),
        ('"a\\x"', "Bad escaped character in JSON at position 3 (line 1 column 4)"),
        ('"a\\u12g4"', "Bad Unicode escape in JSON at position 6 (line 1 column 7)"),
        ('"a\u0001b"', "Bad control character in string literal in JSON at position 2 (line 1 column 3)"),
        ('"abc\\', "Unexpected end of JSON input"),
        ("abcdefghijklmnopqrstuvwxyz", 'Unexpected token \'a\', "abcdefghij"... is not valid JSON'),
        ('{"aaaaaaaaaaaaaaaaaaaaaaaa": 1, "b": x}', 'Unexpected token \'x\', ...": 1, "b": x}" is not valid JSON'),
        ('{"a":[1,2,{"b":tru}]}', 'Unexpected token \'}\', ..."2,{"b":tru}]}" is not valid JSON'),
        ('{\r\n  "a": { "b": 1, }\r\n}', "Expected double-quoted property name in JSON at position 20 (line 2 column 18)"),
        ("12\ra", "Unexpected non-whitespace character after JSON at position 3 (line 2 column 1)"),
        ('[1,"\\﻿n"]', 'Unexpected token \'﻿\', "[1,"\\﻿n"]" is not valid JSON'),
        ('{"a": [1, 2]}', None),
    ],
)
def test_messages_read_as_v8_s(text, message):
    assert jsonParseErrorMessage(text) == message


def test_json_parse_raises_v8_s_message_and_parses_json():
    assert jsonParse('{"a": [1, true, null]}') == {"a": [1, True, None]}
    with pytest.raises(JsonSyntaxError, match=r"^Expected ',' or '}' after property value in JSON at position 9"):
        jsonParse('{"a":true')
