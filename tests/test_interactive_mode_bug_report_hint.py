"""Translated from pi's ``coding-agent/test/interactive-mode-bug-report-hint.test.ts`` (v1.1.0).

MISAKA fork: MISAKA has no ``/bug``, so an unexpected error only checks whether the install changed
(``maybeShowInstallChangeWarning``); the crash hint offers ``misaka -ne`` alone.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage
from misaka.config import APP_NAME
from misaka.modes.interactive import interactive_mode
from misaka.modes.interactive.interactive_mode import (
    InteractiveMode,
    formatCrashExtensionHint,
)


def errorMessage(text: str):
    return fauxAssistantMessage("", {"stopReason": "error", "errorMessage": text})


def hintContext(installChanged: bool = False):
    return SimpleNamespace(maybeShowInstallChangeWarning=Mock(return_value=installChanged))


def test_identifies_extensions_with_frames_in_a_crash_stack():
    assert formatCrashExtensionHint(["npm:pi-observational-memory"]) == (
        "A stack frame came from loaded extension `npm:pi-observational-memory`, which may be involved. Run "
        f"`{APP_NAME} -ne` to confirm."
    )
    assert formatCrashExtensionHint(None) is None


def test_names_several_extensions():
    assert formatCrashExtensionHint(["a", "b", "c"]) == (
        f"A stack frame came from loaded extensions `a`, `b`, and `c`, which may be involved. Run `{APP_NAME} -ne` to "
        "confirm."
    )


def test_does_not_check_for_retryable_provider_failures():
    context = hintContext()
    failures = [
        "500 Internal Server Error",
        "502 Bad Gateway",
        "503 Service Unavailable",
        "504 Gateway Timeout",
        "429 Too Many Requests",
        "Provider overloaded",
        "Network connection lost",
        "Request timed out",
    ]

    for failure in failures:
        InteractiveMode.maybeSuggestBugReport(context, errorMessage(failure))

    context.maybeShowInstallChangeWarning.assert_not_called()


def test_does_not_check_for_cancellations():
    context = hintContext()

    InteractiveMode.maybeSuggestBugReport(context, errorMessage("This operation was aborted"))
    InteractiveMode.maybeSuggestBugReport(context, errorMessage("Request cancelled"))
    InteractiveMode.maybeSuggestBugReport(context, fauxAssistantMessage("", {"stopReason": "aborted"}))

    context.maybeShowInstallChangeWarning.assert_not_called()


def test_checks_for_an_install_change_after_unexpected_errors():
    context = hintContext()

    InteractiveMode.maybeSuggestBugReport(context, errorMessage("Unexpected internal state"))

    context.maybeShowInstallChangeWarning.assert_called_once_with()


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            {"kind": "updated", "version": "9.9.9"},
            f"{APP_NAME} was updated to 9.9.9 while this session was running",
        ),
        ({"kind": "removed"}, f"The {APP_NAME} installation this session runs from was removed or replaced"),
    ],
)
def test_shows_the_install_change_warning_once(monkeypatch, change, expected):
    monkeypatch.setattr(interactive_mode, "detectInstallChange", lambda: change)
    monkeypatch.setattr(interactive_mode, "formatResumeCommand", lambda _sessionManager: None)
    context = SimpleNamespace(installChangeWarningShown=False, sessionManager=None, showWarning=Mock())

    assert InteractiveMode.maybeShowInstallChangeWarning(context) is True
    assert InteractiveMode.maybeShowInstallChangeWarning(context) is True

    context.showWarning.assert_called_once()
    warning = context.showWarning.call_args.args[0]
    assert warning.startswith(expected)
    assert warning.endswith(f"Features that load code on demand can fail until restart. Restart {APP_NAME}.")


def test_reports_no_change_when_the_install_is_intact(monkeypatch):
    monkeypatch.setattr(interactive_mode, "detectInstallChange", lambda: None)
    context = SimpleNamespace(installChangeWarningShown=False, showWarning=Mock())

    assert InteractiveMode.maybeShowInstallChangeWarning(context) is False
    context.showWarning.assert_not_called()


def test_the_warning_names_the_resume_command(monkeypatch):
    monkeypatch.setattr(interactive_mode, "detectInstallChange", lambda: {"kind": "removed"})
    monkeypatch.setattr(interactive_mode, "formatResumeCommand", lambda _sessionManager: f"{APP_NAME} chat --session s1")
    context = SimpleNamespace(installChangeWarningShown=False, sessionManager=None, showWarning=Mock())

    InteractiveMode.maybeShowInstallChangeWarning(context)

    assert context.showWarning.call_args.args[0].endswith(f"Restart with `{APP_NAME} chat --session s1` to continue this session.")
