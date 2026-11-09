"""Translated from pi's ``modes/interactive/components/session-selector-search.ts`` (v1.1.0)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from misaka.core.session_manager import SessionInfo
from misaka.ui.tui import fuzzyMatch
from misaka.utils import js_regexp
from misaka.utils.js import errorMessage, jsCollapseWhitespace, jsLength, jsTrim
from misaka.utils.js_regexp import RegExp

type SortMode = Literal["threaded", "recent", "relevance"]

type NameFilter = Literal["all", "named"]


@dataclass(slots=True)
class SearchToken:
    """MISAKA fork: a name for upstream's inline ``{ kind; value }`` token type."""

    kind: Literal["fuzzy", "phrase"]
    value: str


@dataclass(slots=True)
class ParsedSearchQuery:
    mode: Literal["tokens", "regex"]
    tokens: list[SearchToken]
    regex: RegExp | None
    # If set, parsing failed and we should treat query as non-matching.
    error: str | None = None


@dataclass(slots=True)
class MatchResult:
    matches: bool
    # Lower is better; only meaningful when matches === true
    score: float


_WHITESPACE_RUN = RegExp(r"\s+")


def normalizeWhitespaceLower(text: str) -> str:
    return jsTrim(jsCollapseWhitespace(text.lower()))


def getSessionSearchText(session: SessionInfo) -> str:
    return f"{session.id} {session.name if session.name is not None else ''} {session.allMessagesText} {session.cwd}"


def hasSessionName(session: SessionInfo) -> bool:
    return bool(jsTrim(session.name) if session.name is not None else None)


def matchesNameFilter(session: SessionInfo, filter: NameFilter) -> bool:
    if filter == "all":
        return True
    return hasSessionName(session)


def parseSearchQuery(query: str) -> ParsedSearchQuery:
    trimmed = jsTrim(query)
    if not trimmed:
        return ParsedSearchQuery(mode="tokens", tokens=[], regex=None)

    # Regex mode: re:<pattern>
    if trimmed.startswith("re:"):
        pattern = jsTrim(trimmed[3:])
        if not pattern:
            return ParsedSearchQuery(mode="regex", tokens=[], regex=None, error="Empty regex")
        try:
            return ParsedSearchQuery(mode="regex", tokens=[], regex=RegExp(pattern, "i"))
        except Exception as err:  # noqa: BLE001 - upstream `catch (err)` turns any failure into the error text
            msg = errorMessage(err)
            return ParsedSearchQuery(mode="regex", tokens=[], regex=None, error=msg)

    # Token mode with quote support.
    # Example: foo "node cve" bar
    tokens: list[SearchToken] = []
    buf = ""
    inQuote = False
    hadUnclosedQuote = False

    def flush(kind: Literal["fuzzy", "phrase"]) -> None:
        nonlocal buf
        v = jsTrim(buf)
        buf = ""
        if not v:
            return
        tokens.append(SearchToken(kind=kind, value=v))

    for ch in trimmed:
        if ch == '"':
            if inQuote:
                flush("phrase")
                inQuote = False
            else:
                flush("fuzzy")
                inQuote = True
            continue

        if not inQuote and jsTrim(ch) == "":
            flush("fuzzy")
            continue

        buf += ch

    if inQuote:
        hadUnclosedQuote = True

    # If quotes were unbalanced, fall back to plain whitespace tokenization.
    if hadUnclosedQuote:
        return ParsedSearchQuery(
            mode="tokens",
            tokens=[
                SearchToken(kind="fuzzy", value=t)
                for t in (jsTrim(t) for t in js_regexp.split(trimmed, _WHITESPACE_RUN))
                if len(t) > 0
            ],
            regex=None,
        )

    flush("phrase" if inQuote else "fuzzy")

    return ParsedSearchQuery(mode="tokens", tokens=tokens, regex=None)


def matchSession(session: SessionInfo, parsed: ParsedSearchQuery) -> MatchResult:
    text = getSessionSearchText(session)

    if parsed.mode == "regex":
        if not parsed.regex:
            return MatchResult(matches=False, score=0)
        idx = js_regexp.search(text, parsed.regex)
        if idx < 0:
            return MatchResult(matches=False, score=0)
        return MatchResult(matches=True, score=jsLength(text[:idx]) * 0.1)

    if len(parsed.tokens) == 0:
        return MatchResult(matches=True, score=0)

    totalScore: float = 0
    normalizedText: str | None = None

    for token in parsed.tokens:
        if token.kind == "phrase":
            if normalizedText is None:
                normalizedText = normalizeWhitespaceLower(text)
            phrase = normalizeWhitespaceLower(token.value)
            if not phrase:
                continue
            idx = normalizedText.find(phrase)
            if idx < 0:
                return MatchResult(matches=False, score=0)
            totalScore += jsLength(normalizedText[:idx]) * 0.1
            continue

        m = fuzzyMatch(token.value, text)
        if not m.matches:
            return MatchResult(matches=False, score=0)
        totalScore += m.score

    return MatchResult(matches=True, score=totalScore)


def filterAndSortSessions(
    sessions: list[SessionInfo],
    query: str,
    sortMode: SortMode,
    nameFilter: NameFilter = "all",
) -> list[SessionInfo]:
    nameFiltered = (
        sessions if nameFilter == "all" else [session for session in sessions if matchesNameFilter(session, nameFilter)]
    )
    trimmed = jsTrim(query)
    if not trimmed:
        return nameFiltered

    parsed = parseSearchQuery(query)
    if parsed.error:
        return []

    # Recent mode: filter only, keep incoming order.
    if sortMode == "recent":
        filtered: list[SessionInfo] = []
        for s in nameFiltered:
            res = matchSession(s, parsed)
            if res.matches:
                filtered.append(s)
        return filtered

    # Relevance mode: sort by score, tie-break by modified desc.
    scored: list[tuple[SessionInfo, float]] = []
    for s in nameFiltered:
        res = matchSession(s, parsed)
        if not res.matches:
            continue
        scored.append((s, res.score))

    scored.sort(key=lambda r: (r[1], -r[0].modified.timestamp()))

    return [session for session, _score in scored]


__all__ = [
    "MatchResult",
    "NameFilter",
    "ParsedSearchQuery",
    "SearchToken",
    "SortMode",
    "filterAndSortSessions",
    "hasSessionName",
    "matchSession",
    "parseSearchQuery",
]
