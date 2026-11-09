"""Translated from pi-tui's ``test/fuzzy.test.ts`` (v1.1.0), plus MISAKA cases for the UTF-16 positions."""

from __future__ import annotations

import pytest

from misaka.ui.tui.fuzzy import fuzzyFilter, fuzzyMatch

# --- fuzzyMatch -----------------------------------------------------------------------------------


def test_empty_query_matches_everything_with_score_0():
    result = fuzzyMatch("", "anything")
    assert result.matches is True
    assert result.score == 0


def test_query_longer_than_text_does_not_match():
    result = fuzzyMatch("longquery", "short")
    assert result.matches is False


def test_exact_match_has_good_score():
    result = fuzzyMatch("test", "test")
    assert result.matches is True
    assert result.score < 0  # Should be negative due to consecutive bonuses


def test_characters_must_appear_in_order():
    matchInOrder = fuzzyMatch("abc", "aXbXc")
    assert matchInOrder.matches is True

    matchOutOfOrder = fuzzyMatch("abc", "cba")
    assert matchOutOfOrder.matches is False


def test_case_insensitive_matching():
    result = fuzzyMatch("ABC", "abc")
    assert result.matches is True

    result2 = fuzzyMatch("abc", "ABC")
    assert result2.matches is True


def test_consecutive_matches_score_better_than_scattered_matches():
    consecutive = fuzzyMatch("foo", "foobar")
    scattered = fuzzyMatch("foo", "f_o_o_bar")

    assert consecutive.matches is True
    assert scattered.matches is True
    assert consecutive.score < scattered.score


def test_word_boundary_matches_score_better():
    atBoundary = fuzzyMatch("fb", "foo-bar")
    notAtBoundary = fuzzyMatch("fb", "afbx")

    assert atBoundary.matches is True
    assert notAtBoundary.matches is True
    assert atBoundary.score < notAtBoundary.score


def test_matches_swapped_alpha_numeric_tokens():
    result = fuzzyMatch("codex52", "gpt-5.2-codex")
    assert result.matches is True


# --- fuzzyFilter ----------------------------------------------------------------------------------


def test_empty_query_returns_all_items_unchanged():
    items = ["apple", "banana", "cherry"]
    result = fuzzyFilter(items, "", lambda x: x)
    assert result == items


def test_filters_out_non_matching_items():
    items = ["apple", "banana", "cherry"]
    result = fuzzyFilter(items, "an", lambda x: x)
    assert "banana" in result
    assert "apple" not in result
    assert "cherry" not in result


def test_sorts_results_by_match_quality():
    items = ["a_p_p", "app", "application"]
    result = fuzzyFilter(items, "app", lambda x: x)

    # "app" should be first (exact consecutive match at start)
    assert result[0] == "app"


def test_prioritizes_exact_matches_over_longer_prefix_matches():
    items = ["clone", "cl"]
    result = fuzzyFilter(items, "cl", lambda x: x)

    assert result == ["cl", "clone"]


def test_works_with_custom_gettext_function():
    items = [
        {"name": "foo", "id": 1},
        {"name": "bar", "id": 2},
        {"name": "foobar", "id": 3},
    ]
    result = fuzzyFilter(items, "foo", lambda item: item["name"])

    assert len(result) == 2
    assert "foo" in [r["name"] for r in result]
    assert "foobar" in [r["name"] for r in result]


def test_matches_slash_separated_provider_model_queries_against_reordered_text():
    item = {"id": "gpt-5.5", "provider": "openai-codex"}
    result = fuzzyFilter([item], "openai-codex/gpt-5.5", lambda model: f"{model['id']} {model['provider']}")

    assert result == [item]


# --- MISAKA: positions count UTF-16 code units (values from Node) ---------------------------------


@pytest.mark.parametrize(
    ("query", "text", "score"),
    [("abc", "x😀abc", -13.8), ("😀", "a😀", -4.7), ("ab", "a😀b", -10.7)],
)
def test_scores_count_utf16_code_units(query, text, score):
    result = fuzzyMatch(query, text)
    assert result.matches is True
    assert result.score == pytest.approx(score)


def test_word_boundaries_and_token_splits_use_javascript_whitespace():
    assert fuzzyMatch("b", "a\x85b").score == pytest.approx(0.2)
    assert fuzzyMatch("b", "a﻿b").score == pytest.approx(-9.8)
    assert fuzzyFilter(["ab"], "\x85", lambda x: x) == []
