"""Translated from pi's ``packages/coding-agent/test/ansi-utils.test.ts`` (v1.1.0).

MISAKA fork: Python has no boxed ``Object("x")``; a plain object stands in for that non-string value.
"""

from __future__ import annotations

import re

import pytest

from misaka.utils.ansi import stripAnsi
from misaka.utils.js import UNDEFINED


def referenceAnsiRegex() -> re.Pattern[str]:
    ST = r"(?:\u0007|\u001B\\|\u009C)"
    osc = rf"(?:\u001B\][\s\S]*?{ST})"
    csi = r"[\u001B\u009B][\[\]()#;?]*(?:[0-9]{1,4}(?:[;:][0-9]{0,4})*)?[0-9A-PR-TZcf-nq-uy=><~]"
    return re.compile(f"{osc}|{csi}")


referenceRegex = referenceAnsiRegex()


def referenceStripAnsi(value: str) -> str:
    if "\u001b" not in value and "\u009b" not in value:
        return value
    return referenceRegex.sub("", value)


def getCompatibilityInputs() -> list[str]:
    inputs = [
        "plain",
        "a\x1b[31mred\x1b[0mz",
        "a\x1b]8;;https://example.com\x07link\x1b]8;;\x07z",
        "a\x1b]unterminated",
        "a\x1b]funterminated",
        "a\x1bPabc\x1b\\z",
        "a\x1b^abc\x07z",
        "a\x1b_abc\x9cz",
        "a\x90abc\x9cz",
        "a\x9dabc\x9cz",
        "a\x9b31mred",
        "a\x1b(0x",
        "a\x1b*0x",
        "a\x1b+c",
        "a\x1b/0x",
        "a\x1bcok",
        "a\x1b\\ok",
    ]
    chars = [
        "a",
        "f",
        "0",
        "1",
        ";",
        ":",
        "[",
        "]",
        "(",
        ")",
        "#",
        "?",
        "m",
        "P",
        "_",
        "\\",
        "\x07",
        "\x1b",
        "\x9b",
        "\x9c",
        "\x90",
        "\x9d",
    ]

    for char in chars:
        inputs.append(f"x\x1b{char}y")
        inputs.append(f"x\x9b{char}y")
        for index in range(0, len(chars), 3):
            inputs.append(f"x\x1b{char}{chars[index]}y")

    return inputs


def test_matches_chalk_strip_ansi_for_generated_compatibility_inputs():
    for input in getCompatibilityInputs():
        assert stripAnsi(input) == referenceStripAnsi(input)


def test_throws_the_same_type_error_as_chalk_strip_ansi_for_non_string_values():
    for value, typeName in [(UNDEFINED, "undefined"), (None, "object"), (123, "number"), ({}, "object"), (object(), "object")]:
        message = f"Expected a `string`, got `{typeName}`"
        with pytest.raises(TypeError) as error:
            stripAnsi(value)  # type: ignore[arg-type]
        assert message in str(error.value)


def test_strips_ris_without_leaking_the_final_byte():
    assert stripAnsi("\x1bcdone") == "done"


def test_strips_single_byte_esc_sequences_without_leaking_final_bytes():
    for code in range(ord("g"), ord("m") + 1):
        assert stripAnsi(f"\x1b{chr(code)}ok") == "ok"
    for code in range(ord("r"), ord("t") + 1):
        assert stripAnsi(f"\x1b{chr(code)}ok") == "ok"


def test_strips_common_ansi_sequences_used_in_tool_output():
    input = "a\x1b[31mred\x1b[0m\x1b]8;;https://example.com\x07link\x1b]8;;\x07z"
    assert stripAnsi(input) == "aredlinkz"
