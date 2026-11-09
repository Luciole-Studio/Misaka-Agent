"""The Python port of marked 18.0.11's lexer (``misaka.ui.tui.vendor.marked``) reproduces marked's own tokens.

``tests/fixtures/marked_18_0_11_tokens.json.gz`` holds the tokens real marked 18.0.11 produced under Node for every
CommonMark 0.31.2 spec example and 1,500 fuzzed documents (emphasis runs, emoji and CJK next to delimiters, links,
tables, lists, HTML, line endings), in the gfm, commonmark, pedantic and breaks configurations. ``None`` and an absent
property are the same here, as a renderer reading the tokens cannot tell them apart.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from misaka.ui.tui.vendor.marked import JSObject, Lexer, Marked, Tokenizer
from misaka.utils import js_regexp
from misaka.utils.js_regexp import RegExp

_FIXTURE = Path(__file__).parent / "fixtures" / "marked_18_0_11_tokens.json.gz"
_CONFIGS = {"gfm": {}, "commonmark": {"gfm": False}, "pedantic": {"pedantic": True}, "breaks": {"breaks": True}}
_UNDEFINED = "__undefined__"


def _normalize(value: Any) -> Any:
    if isinstance(value, JSObject):
        value = value.toDict()
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items() if item is not None and item != _UNDEFINED}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def _cases() -> list[dict[str, Any]]:
    return json.loads(gzip.decompress(_FIXTURE.read_bytes()).decode("utf-8"))["cases"]


@pytest.mark.parametrize("config", list(_CONFIGS))
def test_lexes_like_marked(config):
    marked = Marked()
    marked.setOptions(_CONFIGS[config])
    mismatches = [
        case["src"]
        for case in _cases()
        if _normalize(list(marked.lexer(case["src"]))) != _normalize(case[config])
    ]
    assert mismatches == []


def test_a_tokenizer_override_and_an_inline_extension_run_as_upstream():
    class NoEmphasis(Tokenizer):
        def emStrong(self, src, maskedSrc, prevChar=""):
            return None

    marked = Marked()
    marked.setOptions({"tokenizer": NoEmphasis()})
    marked.use({
        "extensions": [
            {
                "name": "mention",
                "level": "inline",
                "start": lambda src: src.find("@"),
                "tokenizer": lambda src: JSObject(type="mention", raw=src[:4], text=src[1:4]) if src.startswith("@") else None,
            }
        ]
    })
    tokens = marked.lexer("hi *x* @bob")[0].tokens
    assert [(token.type, token.raw) for token in tokens] == [("text", "hi *x* "), ("mention", "@bob")]


def test_a_loose_task_item_puts_its_checkbox_inside_the_first_paragraph():
    # Node: [[["paragraph"],["checkbox","text"]],[["paragraph"],["checkbox","text"]]]. The paragraph's inline tokens
    # are still an empty array when the checkbox is placed, and an empty array is truthy in JavaScript.
    items = Marked().lexer("- [ ] a\n\n- [x] b")[0].items
    assert [([t.type for t in item.tokens], [t.type for t in item.tokens[0].tokens]) for item in items] == [
        (["paragraph"], ["checkbox", "text"]),
        (["paragraph"], ["checkbox", "text"]),
    ]


def test_lex_is_a_static_and_an_instance_method():
    assert [token.type for token in Lexer.lex("# a")] == ["heading"]
    assert [token.type for token in Lexer().lex("# a")] == ["heading"]


def test_regexp_keeps_javascript_semantics():
    assert not RegExp("^a$").test("a\n")
    assert RegExp("^b$", "m").test("a\rb")
    assert RegExp(r"[\p{L}\p{N}]", "u").test("п")
    assert RegExp(r"\s").test("﻿") and not RegExp(r"\s").test("\x1c")
    # `é` is not a word character in JavaScript, so there is a word boundary before the `_`.
    assert not RegExp(r"\w").test("é") and RegExp(r"\b_").test("é_")
    assert not RegExp(".").test(" ")
    assert RegExp(r"(?<a>x)\k<a>").exec("xx").groups == {"a": "x"}
    assert RegExp("a{,2}").test("a{,2}")
    assert RegExp("(a)|(b)").exec("b")[1] is None and RegExp("(a)").exec("a")[5] is None
    assert js_regexp.replace("abc", RegExp("(b)"), "[$1$&$`$']") == "a[bbac]c"
    assert js_regexp.replace("aXbXc", RegExp("X", "g"), lambda match: "-") == "a-b-c"
    assert js_regexp.split("a1b2c", RegExp(r"(\d)")) == ["a", "1", "b", "2", "c"]
    assert js_regexp.split("abc", RegExp("")) == ["a", "b", "c"]
    sticky = RegExp("a", "y")
    assert sticky.test("aab") and sticky.lastIndex == 1 and sticky.test("aab") and not sticky.test("aab")
