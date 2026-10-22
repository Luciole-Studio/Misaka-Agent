"""Regression tests for what the adversarial review of the chord port found."""

from __future__ import annotations

import pytest

from misaka.chord.delta import apply, track
from misaka.chord.json import isJsonValue
from misaka.utils.js import UNDEFINED


def test_the_default_sort_compares_utf16_code_units():
    tracker = track({"a": ["\U0001f600", "！", ""]})
    change = tracker.beginChange()
    change.state["a"].sort()
    prepared = change.prepare()
    # An astral character is a surrogate pair (0xD83D...), which sorts before U+E000 and U+FF01.
    assert prepared.value["a"] == ["\U0001f600", "", "！"]
    assert prepared.ops == [["m", ["a"], [0, 2, 1]]]
    change.abort()


def test_object_deletions_come_out_in_the_order_they_were_made():
    tracker = track({"alpha": 1, "beta": 2, "gamma": 3, "delta": 4, "x": {"k": 1}})
    change = tracker.beginChange()
    for key in ("alpha", "beta", "gamma", "delta", "x"):
        change.state[key] = UNDEFINED
    prepared = change.prepare()
    assert prepared.ops == [["d", [key]] for key in ("alpha", "beta", "gamma", "delta", "x")]
    change.abort()


def test_a_numeric_segment_names_the_key_javascript_would():
    assert apply({"a": {}}, [["s", ["a", 1e21], 1]]) == {"a": {"1e+21": 1}}
    assert apply({"a": {"1e+21": "x"}}, [["a", ["a", 1e21], "y"]]) == {"a": {"1e+21": "xy"}}
    with pytest.raises(Exception, match="unsafe path segment: 1e\\+21"):
        apply({"a": []}, [["s", ["a", 1e21], 1]])


def test_delta_errors_print_values_as_string_does():
    with pytest.raises(Exception, match="unsafe path segment: null"):
        apply({"a": {}}, [["s", ["a", None], 1]])
    with pytest.raises(Exception, match="unsafe path segment: true"):
        apply({"a": {}}, [["s", ["a", True], 1]])
    with pytest.raises(Exception, match="unknown op verb: null"):
        apply({"a": {}}, [[None, ["a"]]])
    with pytest.raises(Exception, match="unknown op verb: 1,2"):
        apply({"a": {}}, [[[1, 2], ["a"], 1]])


def test_a_draft_of_json_is_json():
    tracker = track({"nested": {"rows": [{"n": 1}]}, "text": "a"})
    change = tracker.beginChange()
    change.state["nested"]["rows"].push({"n": 2})
    assert isJsonValue(change.state) is True
    assert isJsonValue(change.state["nested"]["rows"]) is True
    change.abort()
