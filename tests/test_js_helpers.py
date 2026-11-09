"""The JavaScript built-ins in ``misaka.utils.js``, checked against what Node prints."""

from __future__ import annotations

import pytest

from misaka.utils.js import (
    UNDEFINED,
    formatNumber,
    fromUtf16Units,
    jsIsInteger,
    jsLength,
    jsonStringify,
    jsRound,
    jsSlice,
    toFixed,
    toPrecision,
    toUtf16Units,
    toWellFormed,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1e-5, "0.00001"),
        (1e-6, "0.000001"),
        (1e-7, "1e-7"),
        (0.1 + 0.2, "0.30000000000000004"),
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        (2**60, "1152921504606847000"),
        (float(2**60), "1152921504606847000"),
        (2**53 + 2, "9007199254740994"),
        (5e-324, "5e-324"),
        (1.7976931348623157e308, "1.7976931348623157e+308"),
        (-0.0, "0"),
        (100.0, "100"),
        (-1.5, "-1.5"),
        (12345.6789, "12345.6789"),
        (float("nan"), "NaN"),
        (float("inf"), "Infinity"),
        (-float("inf"), "-Infinity"),
    ],
)
def test_prints_numbers_as_number_to_string_does(value, expected):
    assert formatNumber(value) == expected


def test_stringifies_like_json_stringify():
    assert jsonStringify({"a": [1, 2.5, None, UNDEFINED, "x\u0001\ud800"], "b": UNDEFINED, "c": {"d": 1e-7}}) == (
        '{"a":[1,2.5,null,null,"x\\u0001\\ud800"],"c":{"d":1e-7}}'
    )
    assert jsonStringify([float("nan"), float("inf"), -0.0, 10**400]) == "[null,null,0,null]"
    assert jsonStringify({"a": [1, {"b": []}], "c": {}}, 2) == '{\n  "a": [\n    1,\n    {\n      "b": []\n    }\n  ],\n  "c": {}\n}'
    assert jsonStringify([1, [2]], "--") == "[\n--1,\n--[\n----2\n--]\n]"
    assert jsonStringify([1], 0) == "[1]"


def test_refuses_what_json_cannot_hold():
    cycle: list = []
    cycle.append(cycle)
    with pytest.raises(ValueError, match="circular"):
        jsonStringify(cycle)
    with pytest.raises(TypeError):
        jsonStringify(UNDEFINED)
    with pytest.raises(TypeError):
        jsonStringify({1, 2})


def test_rounds_like_to_fixed_and_math_round():
    assert [toFixed(x, 1) for x in (0.25, 0.35, 2.5, 1.45, -0.04, 1234.5678, 0.0, -0.0)] == [
        "0.3", "0.3", "2.5", "1.4", "-0.0", "1234.6", "0.0", "0.0",
    ]
    assert [toFixed(x, 2) for x in (0.005, 1.005, 0.015, 2.675, 0.125)] == ["0.01", "1.00", "0.01", "2.67", "0.13"]
    assert [jsRound(x) for x in (2.5, -2.5, 0.49999999999999994, 1.4, 999.5)] == [3, -2, 0, 1, 1000]


def test_prints_to_precision_with_exponents_below_1e_6():
    assert [toPrecision(x, 2) for x in (0.0001234, 0.00099, 1.234e-7, 0.005, 0.0095, 1e-7, 0.0, 123456.0)] == [
        "0.00012", "0.00099", "1.2e-7", "0.0050", "0.0095", "1.0e-7", "0.0", "1.2e+5",
    ]


def test_counts_utf16_code_units():
    assert jsLength("a\U0001f600b") == 4
    assert jsLength("abc") == 3
    assert jsSlice("a\U0001f600b", 1, 2) == "\ud83d"
    assert jsSlice("a\U0001f600b", -1) == "b"
    assert fromUtf16Units(toUtf16Units("a\U0001f600b")) == "a\U0001f600b"
    assert toUtf16Units("😀") == chr(0xD83D) + chr(0xDE00)
    assert toWellFormed("a" + chr(0xD800) + "b") == "a" + chr(0xFFFD) + "b"
    assert toWellFormed("a" + chr(0xD83D) + chr(0xDE00) + "b") == "a" + chr(0x1F600) + "b"


def test_tells_integers_from_other_numbers():
    assert jsIsInteger(1) and jsIsInteger(1.0) and jsIsInteger(-3)
    assert not jsIsInteger(True) and not jsIsInteger(1.5) and not jsIsInteger("1") and not jsIsInteger(float("inf"))


def test_error_message_is_the_message_alone():
    from misaka.utils.js import errorMessage

    group = ExceptionGroup("Startup and cleanup failed", [RuntimeError("a"), RuntimeError("b")])
    assert str(group) == "Startup and cleanup failed (2 sub-exceptions)"
    assert errorMessage(group) == "Startup and cleanup failed"
    assert errorMessage(RuntimeError("boom")) == "boom"
    assert errorMessage(None) == "null"
    assert errorMessage("text") == "text"
    assert errorMessage(3) == "3"


def test_path_resolve_collapses_two_leading_slashes_like_node():
    import os

    from misaka.utils.js import pathResolve

    if os.sep == "/":
        assert pathResolve("//home/u/plugin") == "/home/u/plugin"
        assert pathResolve("///home/u/plugin") == "/home/u/plugin"
        assert pathResolve("/home/u/../plugin") == "/home/plugin"
    assert pathResolve("relative") == os.path.join(os.getcwd(), "relative")


async def test_timers_follow_nodes_delay_rules():
    import asyncio

    import pytest

    from misaka.utils.js import AbortSignal, RangeError, setTimeout

    fired: list[str] = []
    # A delay past the 32-bit range, as one below 1 ms, runs after 1 ms.
    setTimeout(lambda: fired.append("overflow"), 2**31)
    await asyncio.sleep(0.05)
    assert fired == ["overflow"]
    with pytest.raises(RangeError, match="must be an integer"):
        AbortSignal.timeout(1.5)
    with pytest.raises(RangeError, match=">= 0 && <= 4294967295"):
        AbortSignal.timeout(-1)
