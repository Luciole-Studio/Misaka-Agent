"""``misaka.utils.js_regexp``: backreferences and Annex B escapes as JavaScript reads them.

The expectations were recorded from Node 26 (``new RegExp(pattern, flags).exec(input)``); ``"error"`` is a
SyntaxError there (a ValueError or a ``regex`` error here). VERIFY2 R2-T3: a backreference to a group that has not
taken part in the match matches the empty string.
"""

from __future__ import annotations

from typing import Any

import pytest

from misaka.utils.js_regexp import RegExp

RECORDED: list[tuple[str, str, str, Any]] = [
    ('(\\1)', '', 'x', {'m': ['', ''], 'index': 0}),
    ('(\\1)', 'u', 'x', {'m': ['', ''], 'index': 0}),
    ('\\1(a)', '', 'a', {'m': ['a', 'a'], 'index': 0}),
    ('(a)|\\1b', '', 'b', {'m': ['b', None], 'index': 0}),
    ('(a)|\\1b', '', 'ab', {'m': ['a', 'a'], 'index': 0}),
    ('\\k<n>(?<n>x)', '', 'x', {'m': ['x', 'x'], 'index': 0}),
    ('\\k<n>(?<n>x)', 'u', 'x', {'m': ['x', 'x'], 'index': 0}),
    ('(?<n>x)\\k<n>', '', 'xx', {'m': ['xx', 'x'], 'index': 0}),
    ('(a\\1)', '', 'a', {'m': ['a', 'a'], 'index': 0}),
    ('(a\\1)+', '', 'aa', {'m': ['aa', 'a'], 'index': 0}),
    ('((a)\\2)', '', 'aa', {'m': ['aa', 'aa', 'a'], 'index': 0}),
    ('(a)?b\\1', '', 'b', {'m': ['b', None], 'index': 0}),
    ('(a)?b\\1', '', 'aba', {'m': ['aba', 'a'], 'index': 0}),
    ('^(`+)([^`]|[^`][\\s\\S]*?[^`])\\1(?!`)', '', '`x`', {'m': ['`x`', '`', 'x'], 'index': 0}),
    ('(?:(a)|b)\\1c', '', 'bc', {'m': ['bc', None], 'index': 0}),
    ('(?:(a)|b)\\1c', '', 'aac', {'m': ['aac', 'a'], 'index': 0}),
    ('(x)(?:y|\\1)z', 'i', 'xXz', {'m': ['xXz', 'x'], 'index': 0}),
    ('(\\2)(a)', '', 'a', {'m': ['a', '', 'a'], 'index': 0}),
    ('(?<a>`+)[^`]+\\k<a>(?!`)', '', '``x``', {'m': ['``x``', '``'], 'index': 0}),
    ('(a)|(b)\\1', '', 'b', {'m': ['b', None, 'b'], 'index': 0}),
    ('(?=(a))\\1', '', 'a', {'m': ['a', 'a'], 'index': 0}),
    ('(?!(a))\\1b', '', 'b', {'m': ['b', None], 'index': 0}),
    ('(?<=(a))\\1', '', 'aa', {'m': ['a', 'a'], 'index': 1}),
    ('(?<=\\1(a))b', '', 'aab', {'m': ['b', 'a'], 'index': 2}),
    ('(a)\\10', '', 'a0', None),
    ('\\8', '', '8', {'m': ['8'], 'index': 0}),
    ('\\9a', '', '9a', {'m': ['9a'], 'index': 0}),
    ('(a)\\12', '', 'a\n', {'m': ['a\n', 'a'], 'index': 0}),
    ('(a)\\18', '', 'a\x018', {'m': ['a\x018', 'a'], 'index': 0}),
    ('\\01', '', '\x01', {'m': ['\x01'], 'index': 0}),
    ('\\012', '', '\n', {'m': ['\n'], 'index': 0}),
    ('\\0', '', '\x00', {'m': ['\x00'], 'index': 0}),
    ('\\k', '', 'k', {'m': ['k'], 'index': 0}),
    ('\\k<x>', '', 'k<x>', {'m': ['k<x>'], 'index': 0}),
    ('(a)\\1', '', 'aa', {'m': ['aa', 'a'], 'index': 0}),
    ('\\101', '', 'A', {'m': ['A'], 'index': 0}),
    ('\\377', '', 'ÿ', {'m': ['ÿ'], 'index': 0}),
    ('\\400', '', ' 0', {'m': [' 0'], 'index': 0}),
    ('\\8', 'u', '8', 'error'),
    ('\\k', 'u', 'k', 'error'),
    ('(?<n>a)\\k<m>', '', 'aa', 'error'),
    ('(?<n>a)\\1', '', 'aa', {'m': ['aa', 'a'], 'index': 0}),
    ('(?<n>a)\\k<n>', 'i', 'aA', {'m': ['aA', 'a'], 'index': 0}),
    ('(a)(b)(c)(d)(e)(f)(g)(h)(i)(j)\\10', '', 'abcdefghijj', {'m': ['abcdefghijj', 'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j'], 'index': 0}),
    # DecimalDigits are ASCII: a superscript or Arabic-Indic digit after `\` is an identity escape, never a
    # backreference, an octal escape or a quantifier bound (VERIFY2 R5-N3).
    ('\\²', '', '²', {'m': ['²'], 'index': 0}),
    ('\\٣', '', '٣', {'m': ['٣'], 'index': 0}),
    ('(a)(b)(c)\\٣', '', 'abc٣', {'m': ['abc٣', 'a', 'b', 'c'], 'index': 0}),
    ('(a)(b)(c)\\٣', '', 'abcc', None),
    ('a{²}', '', 'a{²}', {'m': ['a{²}'], 'index': 0}),
    ('\\0٣', '', '\x00٣', {'m': ['\x00٣'], 'index': 0}),
    ('\\1٣(a)', '', '٣a', {'m': ['٣a', 'a'], 'index': 0}),
    ('(a)\\1²', '', 'aa²', {'m': ['aa²', 'a'], 'index': 0}),
]


def _exec(pattern: str, flags: str, string: str) -> Any:
    try:
        match = RegExp(pattern, flags).exec(string)
    except Exception:  # noqa: BLE001 - any refusal of the pattern is JavaScript's SyntaxError
        return "error"
    return None if match is None else {"m": list(match), "index": match.index}


@pytest.mark.parametrize(("pattern", "flags", "string", "expected"), RECORDED)
def test_exec_matches_node(pattern: str, flags: str, string: str, expected: Any):
    assert _exec(pattern, flags, string) == expected


def test_a_quantified_atoms_captures_are_not_cleared_per_iteration():
    """Not emulated (module docstring): JavaScript gives ``["ab", undefined, "b"]`` (group 1 cleared when the second
    iteration matched ``(b)``, so ``\\1`` is empty); here group 1 keeps ``"a"``."""
    assert _exec(r"(?:(a)|(b))+\1", "", "ab") == {"m": ["b", None, "b"], "index": 1}


def test_marked_s_backreferences_keep_their_meaning():
    """Every backreference among the callers' patterns (marked's fences, inline code, strikethrough, html, link
    labels) refers to a group that always took part before it: the conditional changes nothing there."""
    assert RegExp(r"^(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)").exec("``a`` b")[0] == "``a``"
    assert RegExp(r"^(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)").exec("``a` b") is None
    assert RegExp(r"(?<b>`+)[^`]+\k<b>(?!`)").exec("x ``y`` z")[0] == "``y``"
    assert RegExp(r"^(~~)(?=[^\s~])((?:\\.|[^\\])*?(?:\\.|[^\s~\\]))\1(?=[^~]|$)").exec("~~a~~")[2] == "a"



def test_a_dollar_before_a_non_ascii_digit_is_literal_in_a_replacement():
    # Node 26: "abc".replace(/(a)(b)(c)/, "[$٣][$3]") === "[$٣][c]"
    from misaka.utils.js_regexp import replace

    assert replace("abc", RegExp("(a)(b)(c)"), "[$٣][$3]") == "[$٣][c]"
