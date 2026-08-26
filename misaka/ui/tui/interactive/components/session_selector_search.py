"""Search parsing and ranking helpers for interactive session selection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from misaka.ui.tui import fuzzyMatch

from misaka.core.session_manager import SessionInfo

type SortMode = Literal["threaded", "recent", "relevance"]
type NameFilter = Literal["all", "named"]


@dataclass(slots=True)
class SearchToken:
    kind: str
    value: str


@dataclass(slots=True)
class ParsedSearchQuery:
    mode: str
    tokens: list[SearchToken]
    regex: re.Pattern[str] | None
    error: str | None = None


@dataclass(slots=True)
class MatchResult:
    matches: bool
    score: float


def normalize_whitespace_lower(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def get_session_search_text(session: SessionInfo) -> str:
    return f"{session.id} {session.name or ''} {session.allMessagesText} {session.cwd}"


def has_session_name(session: SessionInfo) -> bool:
    return bool((session.name or "").strip())


def matches_name_filter(session: SessionInfo, filter: NameFilter) -> bool:
    return True if filter == "all" else has_session_name(session)


def parse_search_query(query: str) -> ParsedSearchQuery:
    trimmed = query.strip()
    if not trimmed:
        return ParsedSearchQuery(mode="tokens", tokens=[], regex=None)

    if trimmed.startswith("re:"):
        pattern = trimmed[3:].strip()
        if not pattern:
            return ParsedSearchQuery(mode="regex", tokens=[], regex=None, error="Empty regex")
        try:
            return ParsedSearchQuery(mode="regex", tokens=[], regex=re.compile(pattern, re.IGNORECASE))
        except re.error as error:
            return ParsedSearchQuery(mode="regex", tokens=[], regex=None, error=str(error))

    tokens: list[SearchToken] = []
    buffer = ""
    in_quote = False
    had_unclosed_quote = False

    def flush(kind: str) -> None:
        nonlocal buffer
        value = buffer.strip()
        buffer = ""
        if value:
            tokens.append(SearchToken(kind=kind, value=value))

    for char in trimmed:
        if char == '"':
            if in_quote:
                flush("phrase")
                in_quote = False
            else:
                flush("fuzzy")
                in_quote = True
            continue
        if not in_quote and char.isspace():
            flush("fuzzy")
            continue
        buffer += char

    if in_quote:
        had_unclosed_quote = True

    if had_unclosed_quote:
        return ParsedSearchQuery(
            mode="tokens",
            tokens=[SearchToken(kind="fuzzy", value=token) for token in trimmed.split() if token.strip()],
            regex=None,
        )

    flush("phrase" if in_quote else "fuzzy")
    return ParsedSearchQuery(mode="tokens", tokens=tokens, regex=None)


def match_session(session: SessionInfo, parsed: ParsedSearchQuery) -> MatchResult:
    text = get_session_search_text(session)

    if parsed.mode == "regex":
        if parsed.regex is None:
            return MatchResult(matches=False, score=0)
        match = parsed.regex.search(text)
        if match is None:
            return MatchResult(matches=False, score=0)
        return MatchResult(matches=True, score=match.start() * 0.1)

    if not parsed.tokens:
        return MatchResult(matches=True, score=0)

    total_score = 0.0
    normalized_text: str | None = None
    for token in parsed.tokens:
        if token.kind == "phrase":
            if normalized_text is None:
                normalized_text = normalize_whitespace_lower(text)
            phrase = normalize_whitespace_lower(token.value)
            if not phrase:
                continue
            index = normalized_text.find(phrase)
            if index < 0:
                return MatchResult(matches=False, score=0)
            total_score += index * 0.1
            continue

        match = fuzzyMatch(token.value, text)
        if not match.matches:
            return MatchResult(matches=False, score=0)
        total_score += match.score

    return MatchResult(matches=True, score=total_score)


def filter_and_sort_sessions(
    sessions: list[SessionInfo],
    query: str,
    sortMode: SortMode,
    nameFilter: NameFilter = "all",
) -> list[SessionInfo]:
    name_filtered = (
        sessions
        if nameFilter == "all"
        else [session for session in sessions if matches_name_filter(session, nameFilter)]
    )
    if not query.strip():
        return name_filtered

    parsed = parse_search_query(query)
    if parsed.error:
        return []

    if sortMode == "recent":
        return [session for session in name_filtered if match_session(session, parsed).matches]

    scored: list[tuple[SessionInfo, float]] = []
    for session in name_filtered:
        result = match_session(session, parsed)
        if result.matches:
            scored.append((session, result.score))

    scored.sort(key=lambda pair: (pair[1], -pair[0].modified.timestamp()))
    return [session for session, _score in scored]


filterAndSortSessions = filter_and_sort_sessions
hasSessionName = has_session_name
matchSession = match_session
parseSearchQuery = parse_search_query

__all__ = [
    "MatchResult",
    "NameFilter",
    "ParsedSearchQuery",
    "SortMode",
    "filterAndSortSessions",
    "hasSessionName",
    "matchSession",
    "parseSearchQuery",
]
