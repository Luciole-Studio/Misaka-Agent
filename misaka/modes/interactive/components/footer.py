"""Translated from pi's ``modes/interactive/components/footer.ts`` (v1.1.0).

MISAKA forks: ``formatCwdForFooter`` uses ``os.path`` (``relpath`` raises across Windows drives, where Node returns an
absolute path; both mean "not inside home"); session entries and the model are read with ``read_field``.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from typing import Any

from misaka.core.experimental import areExperimentalFeaturesEnabled
from misaka.core.extensions.types import ContextUsage
from misaka.core.usage_totals import UsageTotals, addUsageToTotals, createUsageTotals
from misaka.modes.interactive.theme.theme import theme
from misaka.ui.tui import truncateToWidth, visibleWidth
from misaka.utils import js_regexp
from misaka.utils.collation import localeCompare
from misaka.utils.js import jsRound, jsString, jsTrim, toFixed
from misaka.utils.js_regexp import RegExp
from misaka.utils.values import read_field

_CONTROL_WHITESPACE = RegExp(r"[\r\n\t]", "g")
_SPACES = RegExp(" +", "g")


def sanitizeStatusText(text: str) -> str:
    """Sanitize text for display in a single-line status.
    Removes newlines, tabs, carriage returns, and other control characters.
    """
    # Replace newlines, tabs, carriage returns with space, then collapse multiple spaces
    return jsTrim(js_regexp.replace(js_regexp.replace(text, _CONTROL_WHITESPACE, " "), _SPACES, " "))


def formatTokens(count: float) -> str:
    """Format token counts for compact footer display."""
    if count < 1000:
        return jsString(count)
    if count < 10000:
        return f"{toFixed(count / 1000, 1)}k"
    if count < 1000000:
        return f"{jsRound(count / 1000)}k"
    if count < 10000000:
        return f"{toFixed(count / 1000000, 1)}M"
    return f"{jsRound(count / 1000000)}M"


# MISAKA: misaka/core/subagent imports this name, and that package is not edited.
format_tokens = formatTokens


def formatCwdForFooter(cwd: str, home: str | None) -> str:
    if not home:
        return cwd

    resolvedCwd = os.path.abspath(cwd)
    resolvedHome = os.path.abspath(home)
    try:
        relativeToHome = os.path.relpath(resolvedCwd, resolvedHome)
    except ValueError:
        return cwd
    if relativeToHome == os.curdir:
        relativeToHome = ""
    isInsideHome = relativeToHome == "" or (
        relativeToHome != ".." and not relativeToHome.startswith(f"..{os.sep}") and not os.path.isabs(relativeToHome)
    )

    if not isInsideHome:
        return cwd
    return "~" if relativeToHome == "" else f"~{os.sep}{relativeToHome}"


@dataclass(slots=True)
class SessionStats:
    session: Any
    sessionId: str
    leafId: str | None
    entryCount: int
    limitsModel: Any
    usageTotals: UsageTotals
    latestCacheHitRate: float | None
    contextUsage: ContextUsage | None


class FooterComponent:
    """Footer component that shows pwd, token stats, and context usage.
    Computes token/context stats from session, gets git branch and extension statuses from provider.
    """

    def __init__(self, session: Any, footerData: Any) -> None:
        self.autoCompactEnabled = True
        self.sessionStats: SessionStats | None = None
        self.session = session
        self.footerData = footerData

    def setSession(self, session: Any) -> None:
        self.session = session

    def setAutoCompactEnabled(self, enabled: bool) -> None:
        self.autoCompactEnabled = enabled

    def invalidate(self) -> None:
        """No-op: git branch caching now handled by provider.
        Kept for compatibility with existing call sites in interactive-mode.
        """
        # No-op: git branch is cached/invalidated by provider

    def dispose(self) -> None:
        """Clean up resources.
        Git watcher cleanup now handled by provider.
        """
        # Git watcher cleanup handled by provider

    def getSessionStats(self) -> SessionStats:
        """Usage totals and context usage scan the whole session, and the footer renders on every frame.
        Entries are append-only and every append moves the leaf, so the results only change with the
        session, leaf, entry count, or the model whose context window applies.
        """
        sessionManager = self.session.sessionManager
        entryCount = sessionManager.getEntryCount()
        sessionId = sessionManager.getSessionId()
        leafId = sessionManager.getLeafId()
        routed = read_field(self.session, "routedModel")
        limitsModel = routed.model if routed is not None else read_field(self.session, "model")
        cached = self.sessionStats
        if (
            cached
            and cached.session is self.session
            and cached.sessionId == sessionId
            and cached.leafId == leafId
            and cached.entryCount == entryCount
            and cached.limitsModel is limitsModel
        ):
            return cached

        # Calculate cumulative usage from ALL session entries (not just post-compaction messages)
        usageTotals = createUsageTotals()
        latestCacheHitRate: float | None = None

        for entry in sessionManager.getEntries():
            entryType = read_field(entry, "type")
            message = read_field(entry, "message")
            if entryType == "usage":
                addUsageToTotals(usageTotals, read_field(entry, "usage"))
            elif entryType == "message" and read_field(message, "role") == "assistant":
                usage = read_field(message, "usage")
                addUsageToTotals(usageTotals, usage)

                latestPromptTokens = (
                    read_field(usage, "input", 0) + read_field(usage, "cacheRead", 0) + read_field(usage, "cacheWrite", 0)
                )
                latestCacheHitRate = (
                    (read_field(usage, "cacheRead", 0) / latestPromptTokens) * 100 if latestPromptTokens > 0 else None
                )
            elif entryType == "message" and read_field(message, "role") == "toolResult" and read_field(message, "usage"):
                addUsageToTotals(usageTotals, read_field(message, "usage"))
            elif (entryType == "branch_summary" or entryType == "compaction") and read_field(entry, "usage"):
                addUsageToTotals(usageTotals, read_field(entry, "usage"))

        # Calculate context usage from session (handles compaction correctly).
        # After compaction, tokens are unknown until the next LLM response.
        contextUsage = self.session.getContextUsage()
        self.sessionStats = SessionStats(
            session=self.session,
            sessionId=sessionId,
            leafId=leafId,
            entryCount=entryCount,
            limitsModel=limitsModel,
            usageTotals=usageTotals,
            latestCacheHitRate=latestCacheHitRate,
            contextUsage=contextUsage,
        )
        return self.sessionStats

    def render(self, width: int) -> list[str]:
        state = self.session.state
        stats = self.getSessionStats()
        usageTotals, latestCacheHitRate, contextUsage = stats.usageTotals, stats.latestCacheHitRate, stats.contextUsage
        model = read_field(state, "model")
        contextWindow = (
            contextUsage["contextWindow"]
            if contextUsage is not None and contextUsage.get("contextWindow") is not None
            else read_field(model, "contextWindow", 0)
        )
        percent = contextUsage.get("percent") if contextUsage is not None else None
        contextPercentValue = percent if percent is not None else 0
        # `contextUsage?.percent !== null`: a missing context usage counts as 0%, a null percent as unknown.
        contextPercent = toFixed(contextPercentValue, 1) if contextUsage is None or percent is not None else "?"

        # Replace home directory with ~
        pwd = formatCwdForFooter(
            self.session.sessionManager.getCwd(), os.environ.get("HOME") or os.environ.get("USERPROFILE")
        )

        # Add git branch if available
        branch = self.footerData.getGitBranch()
        if branch:
            pwd = f"{pwd} ({branch})"

        # Add session name if set
        sessionName = self.session.sessionManager.getSessionName()
        if sessionName:
            pwd = f"{pwd} • {sessionName}"

        # Build stats line
        statsParts: list[str] = []
        if usageTotals.input:
            statsParts.append(f"↑{formatTokens(usageTotals.input)}")
        if usageTotals.output:
            statsParts.append(f"↓{formatTokens(usageTotals.output)}")
        if usageTotals.cacheRead:
            statsParts.append(f"R{formatTokens(usageTotals.cacheRead)}")
        if usageTotals.cacheWrite:
            statsParts.append(f"W{formatTokens(usageTotals.cacheWrite)}")
        if (usageTotals.cacheRead > 0 or usageTotals.cacheWrite > 0) and latestCacheHitRate is not None:
            statsParts.append(f"CH{toFixed(latestCacheHitRate, 1)}%")

        # Kimi Coding is subscription-backed despite using API-key authentication.
        usingSubscription = (
            read_field(model, "provider") == "kimi-coding"
            or self.session.modelRuntime.isUsingSubscription(read_field(model, "provider"))
            if model
            else False
        )
        if usageTotals.cost or usingSubscription:
            costStr = f"${toFixed(usageTotals.cost, 3)}{' (sub)' if usingSubscription else ''}"
            statsParts.append(costStr)

        # Colorize context percentage based on usage
        autoIndicator = " (auto)" if self.autoCompactEnabled else ""
        contextPercentDisplay = (
            f"?/{formatTokens(contextWindow)}{autoIndicator}"
            if contextPercent == "?"
            else f"{contextPercent}%/{formatTokens(contextWindow)}{autoIndicator}"
        )
        if contextPercentValue > 90:
            contextPercentStr = theme.fg("error", contextPercentDisplay)
        elif contextPercentValue > 70:
            contextPercentStr = theme.fg("warning", contextPercentDisplay)
        else:
            contextPercentStr = contextPercentDisplay
        statsParts.append(contextPercentStr)
        if areExperimentalFeaturesEnabled():
            statsParts.append(f"{theme.fg('dim', '•')} {theme.bold(theme.fg('warning', 'xp'))}")

        statsLeft = " ".join(statsParts)

        # Add model name on the right side, plus thinking level if model supports it
        modelName = read_field(model, "id") or "no-model"

        statsLeftWidth = visibleWidth(statsLeft)

        # If statsLeft is too wide, truncate it
        if statsLeftWidth > width:
            statsLeft = truncateToWidth(statsLeft, width, "...")
            statsLeftWidth = visibleWidth(statsLeft)

        # Calculate available space for padding (minimum 2 spaces between stats and model)
        minPadding = 2

        # Add thinking level indicator if model supports reasoning
        rightSideWithoutProvider = modelName
        if read_field(model, "reasoning"):
            thinkingLevel = read_field(state, "thinkingLevel") or "off"
            rightSideWithoutProvider = (
                f"{modelName} • thinking off" if thinkingLevel == "off" else f"{modelName} • {thinkingLevel}"
            )
        # A virtual model routes each request; show where the latest response went.
        routed = self.session.routedModel
        if routed:
            level = f" • {routed.thinkingLevel}" if routed.thinkingLevel else ""
            rightSideWithoutProvider += f" → {routed.model.id}{level}"

        # Prepend the provider in parentheses if there are multiple providers and there's enough room
        rightSide = rightSideWithoutProvider
        if self.footerData.getAvailableProviderCount() > 1 and model:
            rightSide = f"({read_field(model, 'provider')}) {rightSideWithoutProvider}"
            if statsLeftWidth + minPadding + visibleWidth(rightSide) > width:
                # Too wide, fall back
                rightSide = rightSideWithoutProvider

        rightSideWidth = visibleWidth(rightSide)
        totalNeeded = statsLeftWidth + minPadding + rightSideWidth

        if totalNeeded <= width:
            # Both fit - add padding to right-align model
            padding = " " * (width - statsLeftWidth - rightSideWidth)
            statsLine = statsLeft + padding + rightSide
        else:
            # Need to truncate right side
            availableForRight = width - statsLeftWidth - minPadding
            if availableForRight > 0:
                truncatedRight = truncateToWidth(rightSide, availableForRight, "")
                truncatedRightWidth = visibleWidth(truncatedRight)
                padding = " " * max(0, width - statsLeftWidth - truncatedRightWidth)
                statsLine = statsLeft + padding + truncatedRight
            else:
                # Not enough space for right side at all
                statsLine = statsLeft

        # Apply dim to each part separately. statsLeft may contain color codes (for context %)
        # that end with a reset, which would clear an outer dim wrapper. So we dim the parts
        # before and after the colored section independently.
        dimStatsLeft = theme.fg("dim", statsLeft)
        remainder = statsLine[len(statsLeft) :]  # padding + rightSide
        dimRemainder = theme.fg("dim", remainder)

        pwdLine = truncateToWidth(theme.fg("dim", pwd), width, theme.fg("dim", "..."))
        lines = [pwdLine, dimStatsLeft + dimRemainder]

        # Add extension statuses on a single line, sorted by key alphabetically
        extensionStatuses = self.footerData.getExtensionStatuses()
        if len(extensionStatuses) > 0:
            def byKey(a: tuple[str, str], b: tuple[str, str]) -> int:
                return localeCompare(a[0], b[0])

            sortedStatuses = [
                sanitizeStatusText(text)
                for _key, text in sorted(extensionStatuses.items(), key=functools.cmp_to_key(byKey))
            ]
            statusLine = " ".join(sortedStatuses)
            # Truncate to terminal width with dim ellipsis for consistency with footer style
            lines.append(truncateToWidth(statusLine, width, theme.fg("dim", "...")))

        return lines

