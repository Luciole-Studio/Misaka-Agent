"""Translated from pi's ``coding-agent/test/footer-width.test.ts`` (v1.1.0).

The session and footer data are ``SimpleNamespace`` fakes standing in for upstream's object literals.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.modes.interactive.components.footer import (
    FooterComponent,
    formatCwdForFooter,
)
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import visibleWidth
from misaka.utils.ansi import stripAnsi


def createSession(
    sessionName: str,
    modelId: str | None = None,
    provider: str | None = None,
    reasoning: bool | None = None,
    thinkingLevel: str | None = None,
    usage: dict[str, Any] | None = None,
    branchUsage: dict[str, Any] | None = None,
    compactionUsage: dict[str, Any] | None = None,
    toolUsage: dict[str, Any] | None = None,
    usingSubscription: bool | None = None,
    routedModel: Any | None = None,
) -> Any:
    entries: list[dict[str, Any]] = []

    if usage is not None:
        entries.append({"type": "message", "message": {"role": "assistant", "usage": usage}})

    if branchUsage is not None:
        entries.append({"type": "branch_summary", "usage": branchUsage})

    if compactionUsage is not None:
        entries.append({"type": "compaction", "usage": compactionUsage})

    if toolUsage is not None:
        entries.append({"type": "message", "message": {"role": "toolResult", "usage": toolUsage}})

    return SimpleNamespace(
        state=SimpleNamespace(
            model=SimpleNamespace(
                id=modelId if modelId is not None else "test-model",
                provider=provider if provider is not None else "test",
                contextWindow=200_000,
                reasoning=reasoning if reasoning is not None else False,
            ),
            thinkingLevel=thinkingLevel if thinkingLevel is not None else "off",
        ),
        sessionManager=SimpleNamespace(
            getEntries=lambda: entries,
            getEntryCount=lambda: len(entries),
            getSessionId=lambda: "test-session",
            getLeafId=lambda: None,
            getSessionName=lambda: sessionName,
            getCwd=lambda: "/tmp/project",
        ),
        getContextUsage=lambda: {"contextWindow": 200_000, "percent": 12.3},
        routedModel=routedModel,
        modelRuntime=SimpleNamespace(isUsingSubscription=lambda _provider: bool(usingSubscription)),
    )


def createFooterData(providerCount: int) -> Any:
    return SimpleNamespace(
        getGitBranch=lambda: "main",
        getExtensionStatuses=dict,
        getAvailableProviderCount=lambda: providerCount,
        onBranchChange=lambda _callback: lambda: None,
    )


def test_does_not_abbreviate_sibling_paths_that_share_the_home_prefix():
    assert formatCwdForFooter("/home/user2", "/home/user") == "/home/user2"


def test_abbreviates_the_home_directory_and_descendants():
    assert formatCwdForFooter("/home/user", "/home/user") == "~"
    assert formatCwdForFooter("/home/user/project", "/home/user") == f"~{os.sep}project"


@pytest.fixture(autouse=True, scope="module")
def default_theme():
    initTheme(None, False)


def test_keeps_all_lines_within_width_for_wide_session_names():
    width = 93
    session = createSession(sessionName="한글" * 30)
    footer = FooterComponent(session, createFooterData(1))

    lines = footer.render(width)
    for line in lines:
        assert visibleWidth(line) <= width


def test_keeps_stats_line_within_width_for_wide_model_and_provider_names():
    width = 60
    session = createSession(
        sessionName="",
        modelId="模" * 30,
        provider="공급자",
        reasoning=True,
        thinkingLevel="high",
        usage={"input": 12_345, "output": 6_789, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 1.234}},
    )
    footer = FooterComponent(session, createFooterData(2))

    lines = footer.render(width)
    for line in lines:
        assert visibleWidth(line) <= width


def test_shows_the_physical_model_a_virtual_model_routed_to():
    session = createSession(
        sessionName="",
        modelId="auto",
        reasoning=True,
        thinkingLevel="high",
        routedModel=SimpleNamespace(model=SimpleNamespace(id="gpt-5.6-luna"), thinkingLevel="medium"),
    )
    footer = FooterComponent(session, createFooterData(1))

    statsLine = stripAnsi(footer.render(120)[1])

    assert "auto • high → gpt-5.6-luna • medium" in statsLine


def test_includes_summary_and_tool_result_usage_in_the_total_cost():
    session = createSession(
        sessionName="",
        usage={"input": 100, "output": 10, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 0.5}},
        branchUsage={"input": 20, "output": 5, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 0.25}},
        compactionUsage={"input": 5, "output": 2, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 0.125}},
        toolUsage={"input": 15, "output": 3, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 0.375}},
    )
    footer = FooterComponent(session, createFooterData(1))

    statsLine = stripAnsi(footer.render(120)[1])
    assert "$1.250" in statsLine


def test_updates_cached_usage_totals_after_an_entry_is_appended():
    usage = {"input": 10, "output": 1, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 0.5}}
    session = createSession(sessionName="", usage=usage)
    footer = FooterComponent(session, createFooterData(1))
    assert "$0.500" in stripAnsi(footer.render(120)[1])

    session.sessionManager.getEntries().append({"type": "message", "message": {"role": "assistant", "usage": usage}})
    assert "$1.000" in stripAnsi(footer.render(120)[1])


def test_shows_the_latest_cache_hit_rate_when_cache_usage_is_present():
    session = createSession(
        sessionName="",
        usage={"input": 100, "output": 10, "cacheRead": 50, "cacheWrite": 50, "cost": {"total": 0.001}},
    )
    footer = FooterComponent(session, createFooterData(1))

    statsLine = stripAnsi(footer.render(120)[1])
    assert "CH25.0%" in statsLine


def test_marks_kimi_coding_costs_as_subscription_estimates():
    session = createSession(
        sessionName="",
        provider="kimi-coding",
        usage={"input": 100, "output": 10, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 1.234}},
    )
    footer = FooterComponent(session, createFooterData(1))

    assert "$1.234 (sub)" in stripAnsi(footer.render(120)[1])


def test_marks_explicitly_identified_subscription_auth():
    session = createSession(sessionName="", provider="anthropic", usingSubscription=True)
    footer = FooterComponent(session, createFooterData(1))

    assert "$0.000 (sub)" in stripAnsi(footer.render(120)[1])


def test_does_not_mark_generic_oauth_sign_in_as_a_subscription():
    session = createSession(
        sessionName="",
        provider="openrouter",
        usage={"input": 100, "output": 10, "cacheRead": 0, "cacheWrite": 0, "cost": {"total": 1.234}},
    )
    footer = FooterComponent(session, createFooterData(1))
    stats = stripAnsi(footer.render(120)[1])

    assert "$1.234" in stats
    assert "(sub)" not in stats
