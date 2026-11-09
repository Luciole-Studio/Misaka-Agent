"""Translated from pi's ``coding-agent/test/interactive-mode-assistant-diagnostics.test.ts`` (v1.1.0)."""

from __future__ import annotations

from types import SimpleNamespace

from misaka.modes.interactive.interactive_mode import InteractiveMode
from misaka.modes.interactive.theme.theme import initTheme
from misaka.ui.tui import Container
from misaka.utils.ansi import stripAnsi

message = {
    "role": "assistant",
    "content": [{"type": "text", "text": "survived"}],
    "api": "anthropic-messages",
    "provider": "anthropic",
    "model": "claude-fable-5-1",
    "usage": {
        "input": 1,
        "output": 1,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 2,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    },
    "stopReason": "stop",
    "timestamp": 1,
    "diagnostics": [
        {
            "type": "anthropic_input_transformations",
            "timestamp": 1,
            "details": {
                "transformations": [
                    {"type": "thinking_dropped", "path": "messages.2.content.0", "reason": "prefix_binding_mismatch"},
                    {"type": "thinking_dropped", "path": "messages.5.content.0", "reason": "prefix_binding_mismatch"},
                    {"type": "thinking_dropped", "path": "messages.8.content.0", "reason": "prefix_binding_mismatch"},
                ]
            },
        }
    ],
}


def noticeContext(shown: bool, branch: list[dict]):
    return SimpleNamespace(
        chatContainer=Container(),
        settingsManager=SimpleNamespace(getShowCacheMissNotices=lambda: shown),
        sessionManager=SimpleNamespace(getBranch=lambda: branch),
    )


def test_shows_anthropic_thinking_drops_when_cache_miss_notices_are_enabled():
    initTheme("dark")
    enabled = noticeContext(True, [])
    InteractiveMode.maybeShowThinkingDropNotice(enabled, message)
    output = stripAnsi("\n".join(enabled.chatContainer.render(120)))
    assert "Anthropic dropped 3 thinking blocks (details in session)" in output

    disabled = noticeContext(False, [])
    InteractiveMode.maybeShowThinkingDropNotice(disabled, message)
    assert len(disabled.chatContainer.children) == 0


def test_does_not_repeat_unchanged_anthropic_thinking_drops():
    initTheme("dark")
    context = noticeContext(True, [{"type": "message", "message": message}])

    InteractiveMode.maybeShowThinkingDropNotice(context, {**message, "timestamp": 2})

    assert len(context.chatContainer.children) == 0
