"""Translated from pi's ``coding-agent/test/session-selector-search.test.ts`` (v1.1.0)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from misaka.core.session_manager import SessionInfo
from misaka.modes.interactive.components.session_selector_search import (
    filterAndSortSessions,
)


def makeSession(id: str, modified: str, allMessagesText: str, **overrides: Any) -> SessionInfo:
    return SessionInfo(
        path=f"/tmp/{id}.jsonl",
        id=id,
        cwd=overrides.get("cwd", ""),
        name=overrides.get("name"),
        created=overrides.get("created", datetime.fromtimestamp(0, UTC)),
        modified=datetime.fromisoformat(modified),
        messageCount=overrides.get("messageCount", 1),
        firstMessage=overrides.get("firstMessage", "(no messages)"),
        allMessagesText=allMessagesText,
    )


def ids(sessions: list[SessionInfo]) -> list[str]:
    return [session.id for session in sessions]


def test_filters_by_quoted_phrase_with_whitespace_normalization():
    sessions = [
        makeSession("a", "2026-01-01T00:00:00.000Z", "node\n\n   cve was discussed"),
        makeSession("b", "2026-01-02T00:00:00.000Z", "node something else"),
    ]

    result = filterAndSortSessions(sessions, '"node cve"', "recent")
    assert ids(result) == ["a"]


def test_filters_by_regex_and_is_case_insensitive():
    sessions = [
        makeSession("a", "2026-01-02T00:00:00.000Z", "Brave is great"),
        makeSession("b", "2026-01-03T00:00:00.000Z", "bravery is not the same"),
    ]

    result = filterAndSortSessions(sessions, "re:\\bbrave\\b", "recent")
    assert ids(result) == ["a"]


def test_recent_sort_preserves_input_order():
    sessions = [
        makeSession("newer", "2026-01-03T00:00:00.000Z", "brave"),
        makeSession("older", "2026-01-01T00:00:00.000Z", "brave"),
        makeSession("nomatch", "2026-01-04T00:00:00.000Z", "something else"),
    ]

    result = filterAndSortSessions(sessions, '"brave"', "recent")
    assert ids(result) == ["newer", "older"]


def test_relevance_sort_orders_by_score_and_tie_breaks_by_modified_desc():
    sessions = [
        makeSession("late", "2026-01-03T00:00:00.000Z", "xxxx brave"),
        makeSession("early", "2026-01-01T00:00:00.000Z", "brave xxxx"),
    ]

    result1 = filterAndSortSessions(sessions, '"brave"', "relevance")
    assert ids(result1) == ["early", "late"]

    tieSessions = [
        makeSession("newer", "2026-01-03T00:00:00.000Z", "brave"),
        makeSession("older", "2026-01-01T00:00:00.000Z", "brave"),
    ]

    result2 = filterAndSortSessions(tieSessions, '"brave"', "relevance")
    assert ids(result2) == ["newer", "older"]


def test_returns_empty_list_for_invalid_regex():
    sessions = [makeSession("a", "2026-01-01T00:00:00.000Z", "brave")]

    result = filterAndSortSessions(sessions, "re:(", "recent")
    assert result == []


nameFilterSessions = [
    makeSession("named1", "2026-01-03T00:00:00.000Z", "blueberry", name="My Project"),
    makeSession("named2", "2026-01-02T00:00:00.000Z", "blueberry", name="Another Named"),
    makeSession("other1", "2026-01-04T00:00:00.000Z", "blueberry"),
    makeSession("other2", "2026-01-01T00:00:00.000Z", "blueberry"),
]


def test_name_filter_returns_all_sessions_when_name_filter_is_all():
    result = filterAndSortSessions(nameFilterSessions, "", "recent", "all")
    assert ids(result) == ["named1", "named2", "other1", "other2"]


def test_name_filter_returns_only_named_sessions_when_name_filter_is_named():
    result = filterAndSortSessions(nameFilterSessions, "", "recent", "named")
    assert ids(result) == ["named1", "named2"]


def test_name_filter_applies_name_filter_before_search_query():
    result = filterAndSortSessions(nameFilterSessions, "blueberry", "recent", "named")
    assert ids(result) == ["named1", "named2"]


def test_name_filter_excludes_whitespace_only_names_from_named_filter():
    sessionsWithWhitespace = [
        makeSession("whitespace", "2026-01-01T00:00:00.000Z", "test", name="   "),
        makeSession("empty", "2026-01-02T00:00:00.000Z", "test", name=""),
        makeSession("named", "2026-01-03T00:00:00.000Z", "test", name="Real Name"),
    ]

    result = filterAndSortSessions(sessionsWithWhitespace, "", "recent", "named")
    assert ids(result) == ["named"]
