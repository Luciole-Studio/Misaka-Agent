"""Fuzzy matching helpers for command and file completion."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class FuzzyMatch:
    matches: bool
    score: float


def fuzzyMatch(query: str, text: str) -> FuzzyMatch:
    query_lower = query.lower()
    text_lower = text.lower()

    def match_query(normalized_query: str) -> FuzzyMatch:
        if len(normalized_query) == 0:
            return FuzzyMatch(matches=True, score=0)

        if len(normalized_query) > len(text_lower):
            return FuzzyMatch(matches=False, score=0)

        query_index = 0
        score = 0.0
        last_match_index = -1
        consecutive_matches = 0

        for index, char in enumerate(text_lower):
            if query_index >= len(normalized_query):
                break
            if char != normalized_query[query_index]:
                continue

            is_word_boundary = index == 0 or bool(re.match(r"[\s\-_./:]", text_lower[index - 1]))

            if last_match_index == index - 1:
                consecutive_matches += 1
                score -= consecutive_matches * 5
            else:
                consecutive_matches = 0
                if last_match_index >= 0:
                    score += (index - last_match_index - 1) * 2

            if is_word_boundary:
                score -= 10

            score += index * 0.1
            last_match_index = index
            query_index += 1

        if query_index < len(normalized_query):
            return FuzzyMatch(matches=False, score=0)

        if normalized_query == text_lower:
            score -= 100

        return FuzzyMatch(matches=True, score=score)

    primary_match = match_query(query_lower)
    if primary_match.matches:
        return primary_match

    alpha_numeric_match = re.fullmatch(r"(?P<letters>[a-z]+)(?P<digits>[0-9]+)", query_lower)
    numeric_alpha_match = re.fullmatch(r"(?P<digits>[0-9]+)(?P<letters>[a-z]+)", query_lower)
    swapped_query = ""
    if alpha_numeric_match is not None:
        swapped_query = f"{alpha_numeric_match.group('digits')}{alpha_numeric_match.group('letters')}"
    elif numeric_alpha_match is not None:
        swapped_query = f"{numeric_alpha_match.group('letters')}{numeric_alpha_match.group('digits')}"

    if not swapped_query:
        return primary_match

    swapped_match = match_query(swapped_query)
    if not swapped_match.matches:
        return primary_match

    return FuzzyMatch(matches=True, score=swapped_match.score + 5)


def fuzzyFilter[T](items: list[T], query: str, get_text: Callable[[T], str]) -> list[T]:
    if not query.strip():
        return items

    tokens = [token for token in re.split(r"[\s/]+", query.strip()) if token]
    if not tokens:
        return items

    results: list[tuple[T, float]] = []
    for item in items:
        text = get_text(item)
        total_score = 0.0
        all_match = True
        for token in tokens:
            match = fuzzyMatch(token, text)
            if not match.matches:
                all_match = False
                break
            total_score += match.score
        if all_match:
            results.append((item, total_score))

    results.sort(key=lambda pair: pair[1])
    return [item for item, _score in results]


__all__ = ["FuzzyMatch", "fuzzyFilter", "fuzzyMatch"]
