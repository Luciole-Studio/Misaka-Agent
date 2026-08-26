"""Interactive footer renderer for session status and context usage."""

from __future__ import annotations

import os
import re
from typing import Any

from misaka.ui.tui import truncateToWidth, visibleWidth

from misaka.ui.tui.interactive.theme.theme import theme


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


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
        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_write = 0
        total_cost = 0.0

        for entry in self.session.sessionManager.getEntries():
            if _value(entry, "type") != "message":
                continue
            message = _value(entry, "message")
            if _value(message, "role") != "assistant":
                continue
            usage = _value(message, "usage") or {}
            cost = _value(usage, "cost") or {}
            total_input += int(_value(usage, "input", 0) or 0)
            total_output += int(_value(usage, "output", 0) or 0)
            total_cache_read += int(_value(usage, "cacheRead", 0) or 0)
            total_cache_write += int(_value(usage, "cacheWrite", 0) or 0)
            total_cost += float(_value(cost, "total", 0) or 0)

        context_usage = self.session.getContextUsage()
        model = _value(state, "model")
        context_window = int(_value(context_usage, "contextWindow", _value(model, "contextWindow", 0)) or 0)
        context_percent_value = float(_value(context_usage, "percent", 0) or 0)
        context_percent = f"{context_percent_value:.1f}" if _value(context_usage, "percent") is not None else "?"

        pwd = self.session.sessionManager.getCwd()
        home = os.path.expanduser("~")
        if pwd.startswith(home):
            pwd = f"~{pwd[len(home):]}"

        branch = self.footerData.getGitBranch()
        if branch:
            pwd = f"{pwd} ({branch})"

        session_name = self.session.sessionManager.getSessionName()
        if session_name:
            pwd = f"{pwd} • {session_name}"

        stats_parts: list[str] = []
        if total_input:
            stats_parts.append(f"↑{format_tokens(total_input)}")
        if total_output:
            stats_parts.append(f"↓{format_tokens(total_output)}")
        if total_cache_read:
            stats_parts.append(f"R{format_tokens(total_cache_read)}")
        if total_cache_write:
            stats_parts.append(f"W{format_tokens(total_cache_write)}")

        using_subscription = bool(model is not None and self.session.modelRegistry.isUsingOAuth(model))
        if total_cost or using_subscription:
            stats_parts.append(f"${total_cost:.3f}{' (sub)' if using_subscription else ''}")

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

        stats_left = " ".join(stats_parts)
        model_name = _value(model, "id", "no-model") or "no-model"
        stats_left_width = visibleWidth(stats_left)
        if stats_left_width > width:
            stats_left = truncateToWidth(stats_left, width, "...")
            stats_left_width = visibleWidth(stats_left)

        min_padding = 2
        right_side_without_provider = model_name
        if bool(_value(model, "reasoning", False)):
            thinking_level = _value(state, "thinkingLevel", "off") or "off"
            if thinking_level == "off":
                right_side_without_provider = f"{model_name} • thinking off"
            else:
                right_side_without_provider = f"{model_name} • {thinking_level}"

        right_side = right_side_without_provider
        if self.footerData.getAvailableProviderCount() > 1 and model is not None:
            right_side = f"({_value(model, 'provider')}) {right_side_without_provider}"
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


formatTokens = format_tokens
sanitizeStatusText = sanitize_status_text

__all__ = ["FooterComponent"]
