"""Translated from pi-tui's ``fuzzy.ts`` (v1.1.0).

Fuzzy matching utilities.
Matches if all query characters appear in order (not necessarily consecutive).
Lower score = better match.

MISAKA fork: positions and lengths count UTF-16 code units, as upstream's do (the strings are matched in their UTF-16
form), so scores agree for text with characters outside the BMP.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from misaka.utils import js_regexp
from misaka.utils.js import jsTrim, toUtf16Units
from misaka.utils.js_regexp import RegExp

_WORD_BOUNDARY = RegExp(r"[\s\-_./:]")
_ALPHA_NUMERIC = RegExp(r"^(?<letters>[a-z]+)(?<digits>[0-9]+)$")
_NUMERIC_ALPHA = RegExp(r"^(?<digits>[0-9]+)(?<letters>[a-z]+)$")
_TOKEN_SEPARATOR = RegExp(r"[\s/]+")


@dataclass(slots=True)
class FuzzyMatch:
    matches: bool
    score: float


def fuzzyMatch(query: str, text: str) -> FuzzyMatch:
    queryLower = toUtf16Units(query.lower())
    textLower = toUtf16Units(text.lower())

    def matchQuery(normalizedQuery: str) -> FuzzyMatch:
        if len(normalizedQuery) == 0:
            return FuzzyMatch(matches=True, score=0)

        if len(normalizedQuery) > len(textLower):
            return FuzzyMatch(matches=False, score=0)

        queryIndex = 0
        score: float = 0
        lastMatchIndex = -1
        consecutiveMatches = 0

        while queryIndex < len(normalizedQuery):
            i = textLower.find(normalizedQuery[queryIndex], lastMatchIndex + 1)
            if i == -1:
                break

            isWordBoundary = i == 0 or _WORD_BOUNDARY.test(textLower[i - 1])

            # Reward consecutive matches
            if lastMatchIndex == i - 1:
                consecutiveMatches += 1
                score -= consecutiveMatches * 5
            else:
                consecutiveMatches = 0
                # Penalize gaps
                if lastMatchIndex >= 0:
                    score += (i - lastMatchIndex - 1) * 2

            # Reward word boundary matches
            if isWordBoundary:
                score -= 10

            # Slight penalty for later matches
            score += i * 0.1

            lastMatchIndex = i
            queryIndex += 1

        if queryIndex < len(normalizedQuery):
            return FuzzyMatch(matches=False, score=0)

        if normalizedQuery == textLower:
            score -= 100

        return FuzzyMatch(matches=True, score=score)

    primaryMatch = matchQuery(queryLower)
    if primaryMatch.matches:
        return primaryMatch

    alphaNumericMatch = js_regexp.match(queryLower, _ALPHA_NUMERIC)
    numericAlphaMatch = js_regexp.match(queryLower, _NUMERIC_ALPHA)
    alphaNumericGroups = getattr(alphaNumericMatch, "groups", None) or {}
    numericAlphaGroups = getattr(numericAlphaMatch, "groups", None) or {}
    swappedQuery = (
        f"{alphaNumericGroups.get('digits') or ''}{alphaNumericGroups.get('letters') or ''}"
        if alphaNumericMatch
        else f"{numericAlphaGroups.get('letters') or ''}{numericAlphaGroups.get('digits') or ''}"
        if numericAlphaMatch
        else ""
    )

    if not swappedQuery:
        return primaryMatch

    swappedMatch = matchQuery(swappedQuery)
    if not swappedMatch.matches:
        return primaryMatch

    return FuzzyMatch(matches=True, score=swappedMatch.score + 5)


def fuzzyFilter[T](items: list[T], query: str, getText: Callable[[T], str]) -> list[T]:
    """Filter and sort items by fuzzy match quality (best matches first).
    Supports whitespace- and slash-separated tokens: all tokens must match.
    """
    if not jsTrim(query):
        return items

    tokens = [t for t in js_regexp.split(jsTrim(query), _TOKEN_SEPARATOR) if len(t) > 0]

    if len(tokens) == 0:
        return items

    results: list[tuple[T, float]] = []

    for item in items:
        text = getText(item)
        totalScore: float = 0
        allMatch = True

        for token in tokens:
            match = fuzzyMatch(token, text)
            if match.matches:
                totalScore += match.score
            else:
                allMatch = False
                break

        if allMatch:
            results.append((item, totalScore))

    results.sort(key=lambda r: r[1])
    return [item for item, _totalScore in results]


__all__ = ["FuzzyMatch", "fuzzyFilter", "fuzzyMatch"]
