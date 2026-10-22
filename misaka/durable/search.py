"""BM25 tool search for code mode's ``searchTools()`` and the ``tool_search`` tool, a copy of the coding agent's tool
search ranking: durable keeps its own copy of what it needs from the coding agent.

Translated from pi's ``packages/durable/src/search.ts`` (v1.3.0). A document is ``{"name", "text"}``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import TypedDict


class SearchDocument(TypedDict):
    name: str
    text: str


STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "with",
    }
)

_PLURAL_ES = re.compile(r"(ches|shes|sses|xes|zes)$")
_LOWER_UPPER = re.compile(r"([a-z0-9])([A-Z])")
_UPPER_RUN = re.compile(r"([A-Z]+)([A-Z][a-z])")
_SEPARATORS = re.compile(r"[^a-z0-9]+")


def stem(term: str) -> str:
    """Naive singular form, so ``issues`` matches ``issue`` and ``searches`` matches ``search``."""
    if len(term) > 4 and term.endswith("ies"):
        return f"{term[:-3]}y"
    if len(term) > 4 and _PLURAL_ES.search(term) is not None:
        return term[:-2]
    if len(term) > 3 and term.endswith("s") and not term.endswith("ss"):
        return term[:-1]
    return term


def tokenize(text: str) -> list[str]:
    """Lowercase terms, split at camelCase boundaries and non-alphanumerics, without stop words."""
    spaced = _UPPER_RUN.sub(r"\1 \2", _LOWER_UPPER.sub(r"\1 \2", text)).lower()
    return [stem(term) for term in _SEPARATORS.split(spaced) if len(term) > 0 and term not in STOP_WORDS]


def rankDocuments(query: str, documents: Sequence[SearchDocument], limit: int) -> list[str]:
    """Okapi BM25 (k1 1.2, b 0.75): the names of the best matches, best first; ties keep document order."""
    k1 = 1.2
    b = 0.75
    queryTerms = list(dict.fromkeys(tokenize(query)))
    if len(queryTerms) == 0 or len(documents) == 0 or limit <= 0:
        return []
    termCounts: list[dict[str, int]] = []
    for document in documents:
        counts: dict[str, int] = {}
        for term in tokenize(document["text"]):
            counts[term] = counts.get(term, 0) + 1
        termCounts.append(counts)
    lengths = [sum(counts.values()) for counts in termCounts]
    averageLength = sum(lengths) / len(documents) or 1
    idf: dict[str, float] = {}
    for term in queryTerms:
        frequency = sum(1 for counts in termCounts if term in counts)
        idf[term] = math.log(1 + (len(documents) - frequency + 0.5) / (frequency + 0.5))
    matches: list[tuple[str, float]] = []
    for index, document in enumerate(documents):
        score = 0.0
        for term in queryTerms:
            count = termCounts[index].get(term)
            if not count:
                continue
            norm = k1 * (1 - b + (b * lengths[index]) / averageLength)
            score += idf.get(term, 0) * ((count * (k1 + 1)) / (count + norm))
        if score > 0:
            matches.append((document["name"], score))
    matches.sort(key=lambda match: -match[1])
    # ``.slice(0, limit)`` of a positive limit: NaN takes none, infinity all, a fraction its integer part.
    end = 0 if math.isnan(limit) else len(matches) if math.isinf(limit) else int(limit)
    return [name for name, _score in matches[:end]]


__all__ = ["STOP_WORDS", "SearchDocument", "rankDocuments", "stem", "tokenize"]
