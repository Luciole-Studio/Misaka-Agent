"""``misaka.utils.jsdiff`` against the npm ``diff`` package (8.0.4).

``tests/fixtures/jsdiff_cases.json`` holds 117 inputs with the ``diffLines`` parts and the ``createTwoFilesPatch`` text the
real package produced for them (recorded with Node; 1509 generated cases matched when the port was written).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from misaka.utils.jsdiff import (
    FILE_HEADERS_ONLY,
    createTwoFilesPatch,
    diffLines,
    structuredPatch,
    tokenize,
)

CASES = json.loads((Path(__file__).parent / "fixtures" / "jsdiff_cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CASES, ids=[f"case{index}" for index in range(len(CASES))])
def test_matches_the_recorded_output_of_the_npm_package(case):
    assert diffLines(case["a"], case["b"]) == case["parts"]
    options = {"context": case["context"], "headerOptions": FILE_HEADERS_ONLY}
    assert createTwoFilesPatch("f.txt", "f.txt", case["a"], case["b"], None, None, options) == case["patch"]


def test_tokenizes_lines_with_their_line_breaks():
    assert tokenize("a\nb\r\nc") == ["a\n", "b\r\n", "c"]
    assert tokenize("a\n") == ["a\n"]
    assert tokenize("") == []


def test_reports_a_change_with_counts_and_values():
    assert diffLines("one\ntwo\nthree\n", "one\n2\nthree\n") == [
        {"count": 1, "added": False, "removed": False, "value": "one\n"},
        {"count": 1, "added": False, "removed": True, "value": "two\n"},
        {"count": 1, "added": True, "removed": False, "value": "2\n"},
        {"count": 1, "added": False, "removed": False, "value": "three\n"},
    ]


def test_marks_a_missing_final_newline():
    patch = createTwoFilesPatch("f", "f", "a\nb", "a\nb\n", None, None, {"headerOptions": FILE_HEADERS_ONLY})
    assert patch == "--- f\n+++ f\n@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+b\n"


def test_gives_the_hunk_of_an_empty_file_a_start_one_lower():
    patch = createTwoFilesPatch("f", "f", "", "x\n", None, None, {"headerOptions": FILE_HEADERS_ONLY})
    assert patch == "--- f\n+++ f\n@@ -0,0 +1,1 @@\n+x\n"


def test_refuses_newline_tokens_in_a_patch():
    with pytest.raises(RuntimeError, match="newlineIsToken"):
        structuredPatch("f", "f", "a", "b", None, None, {"newlineIsToken": True})
