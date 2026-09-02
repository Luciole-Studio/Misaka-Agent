"""Interactive footer renderer for session status and context usage."""

from __future__ import annotations

import os
import re
from typing import Any

from misaka.core.experimental import are_experimental_features_enabled
from misaka.core.usage_totals import addUsageToTotals, createUsageTotals
from misaka.ui.tui import truncateToWidth, visibleWidth
from misaka.ui.tui.interactive.theme.theme import theme
from misaka.utils.values import read_field


def collapse_home(path: str) -> str:
    """Replace the home prefix with ``~``, by path component (pi footer.ts formatCwdForFooter).

    A plain ``startswith(home)`` is a string test, not a path test: with HOME=/home/ab it turns
    /home/abc/proj into "~c/proj", and a HOME with a trailing slash eats the separator
    ("~x" instead of "~/x").
    """
    home = os.path.expanduser("~")
    if not path or not home:
        return path
    try:
        relative = os.path.relpath(path, home)
    except ValueError:      # different drives on Windows; nothing to collapse
        return path
    if relative == os.curdir:
        return "~"
    if relative == os.pardir or relative.startswith(os.pardir + os.sep) or os.path.isabs(relative):
        return path
    return "~" + os.sep + relative


def sanitize_status_text(text: str) -> str:
    return re.sub(r" +", " ", re.sub(r"[\r\n\t]", " ", text)).strip()


def format_tokens(count: int) -> str:
    if count < 1000:
        return str(count)
    if count < 10000:
        return f"{count / 1000:.1f}k"
    if count < 1000000:
        return f"{round(count / 1000)}k"
    if count < 10000000:
        return f"{count / 1000000:.1f}M"
    return f"{round(count / 1000000)}M"


class FooterComponent:
    def __init__(self, session: Any, footerData: Any) -> None:
        self.autoCompactEnabled = True
        self.session = session
        self.footerData = footerData

    def setSession(self, session: Any) -> None:
        self.session = session

    def setAutoCompactEnabled(self, enabled: bool) -> None:
        self.autoCompactEnabled = enabled

    def invalidate(self) -> None:
        return None

    def dispose(self) -> None:
        return None

    def render(self, width: int) -> list[str]:
        state = self.session.state
        usage_totals = createUsageTotals()
        latest_cache_hit_rate: float | None = None

        # pi footer.ts:92-105 counts three kinds of entry, not one: assistant messages,
        # tool results that carry their own usage (subagent/summariser tools report back
        # this way), and compaction / branch-summary entries that carry usage.
        for entry in self.session.sessionManager.getEntries():
            entry_type = read_field(entry, "type")
            if entry_type == "message":
                message = read_field(entry, "message")
                role = read_field(message, "role")
                if role == "assistant":
                    usage = read_field(message, "usage") or {}
                    addUsageToTotals(usage_totals, usage)
                    prompt_tokens = (
                        int(read_field(usage, "input", 0) or 0)
                        + int(read_field(usage, "cacheRead", 0) or 0)
                        + int(read_field(usage, "cacheWrite", 0) or 0)
                    )
                    cache_read = int(read_field(usage, "cacheRead", 0) or 0)
                    latest_cache_hit_rate = (cache_read / prompt_tokens) * 100 if prompt_tokens > 0 else None
                elif role == "toolResult" and read_field(message, "usage"):
                    addUsageToTotals(usage_totals, read_field(message, "usage"))
            elif entry_type in ("branch_summary", "compaction") and read_field(entry, "usage"):
                addUsageToTotals(usage_totals, read_field(entry, "usage"))

        context_usage = self.session.getContextUsage()
        model = read_field(state, "model")
        context_window = int(read_field(context_usage, "contextWindow", read_field(model, "contextWindow", 0)) or 0)
        context_percent_value = float(read_field(context_usage, "percent", 0) or 0)
        context_percent = f"{context_percent_value:.1f}" if read_field(context_usage, "percent") is not None else "?"

        pwd = collapse_home(self.session.sessionManager.getCwd())

        branch = self.footerData.getGitBranch()
        if branch:
            pwd = f"{pwd} ({branch})"

        session_name = self.session.sessionManager.getSessionName()
        if session_name:
            pwd = f"{pwd} • {session_name}"

        stats_parts: list[str] = []
        if usage_totals.input:
            stats_parts.append(f"↑{format_tokens(usage_totals.input)}")
        if usage_totals.output:
            stats_parts.append(f"↓{format_tokens(usage_totals.output)}")
        if usage_totals.cacheRead:
            stats_parts.append(f"R{format_tokens(usage_totals.cacheRead)}")
        if usage_totals.cacheWrite:
            stats_parts.append(f"W{format_tokens(usage_totals.cacheWrite)}")
        if (usage_totals.cacheRead or usage_totals.cacheWrite) and latest_cache_hit_rate is not None:
            stats_parts.append(f"CH{latest_cache_hit_rate:.1f}%")

        # Kimi Coding is subscription-backed despite using API-key authentication.
        using_subscription = bool(
            model is not None
            and (
                read_field(model, "provider") == "kimi-coding"
                or self.session.modelRegistry.isUsingSubscription(model)
            )
        )
        if usage_totals.cost or using_subscription:
            stats_parts.append(f"${usage_totals.cost:.3f}{' (sub)' if using_subscription else ''}")

        auto_indicator = " (auto)" if self.autoCompactEnabled else ""
        if context_percent == "?":
            context_percent_display = f"?/{format_tokens(context_window)}{auto_indicator}"
        else:
            context_percent_display = f"{context_percent}%/{format_tokens(context_window)}{auto_indicator}"
        if context_percent_value > 90:
            context_percent_str = theme.fg("error", context_percent_display)
        elif context_percent_value > 70:
            context_percent_str = theme.fg("warning", context_percent_display)
        else:
            context_percent_str = context_percent_display
        stats_parts.append(context_percent_str)
        if are_experimental_features_enabled():
            stats_parts.append(theme.fg("dim", "•") + " " + theme.bold(theme.fg("warning", "xp")))

        stats_left = " ".join(stats_parts)
        model_name = read_field(model, "id", "no-model") or "no-model"
        stats_left_width = visibleWidth(stats_left)
        if stats_left_width > width:
            stats_left = truncateToWidth(stats_left, width, "...")
            stats_left_width = visibleWidth(stats_left)

        min_padding = 2
        right_side_without_provider = model_name
        if bool(read_field(model, "reasoning", False)):
            thinking_level = read_field(state, "thinkingLevel", "off") or "off"
            if thinking_level == "off":
                right_side_without_provider = f"{model_name} • thinking off"
            else:
                right_side_without_provider = f"{model_name} • {thinking_level}"

        right_side = right_side_without_provider
        if self.footerData.getAvailableProviderCount() > 1 and model is not None:
            right_side = f"({read_field(model, 'provider')}) {right_side_without_provider}"
            if stats_left_width + min_padding + visibleWidth(right_side) > width:
                right_side = right_side_without_provider

        right_side_width = visibleWidth(right_side)
        total_needed = stats_left_width + min_padding + right_side_width
        if total_needed <= width:
            padding = " " * (width - stats_left_width - right_side_width)
            stats_line = stats_left + padding + right_side
        else:
            available_for_right = width - stats_left_width - min_padding
            if available_for_right > 0:
                truncated_right = truncateToWidth(right_side, available_for_right, "")
                truncated_right_width = visibleWidth(truncated_right)
                padding = " " * max(0, width - stats_left_width - truncated_right_width)
                stats_line = stats_left + padding + truncated_right
            else:
                stats_line = stats_left

        dim_stats_left = theme.fg("dim", stats_left)
        remainder = stats_line[len(stats_left) :]
        dim_remainder = theme.fg("dim", remainder)

        pwd_line = truncateToWidth(theme.fg("dim", pwd), width, theme.fg("dim", "..."))
        lines = [pwd_line, dim_stats_left + dim_remainder]

        extension_statuses = self.footerData.getExtensionStatuses()
        if len(extension_statuses) > 0:
            sorted_statuses = [
                sanitize_status_text(text)
                for _key, text in sorted(extension_statuses.items(), key=lambda item: item[0])
            ]
            status_line = " ".join(sorted_statuses)
            lines.append(truncateToWidth(status_line, width, theme.fg("dim", "...")))

        return lines


__all__ = ["FooterComponent"]
