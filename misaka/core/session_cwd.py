"""Session cwd validation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(slots=True)
class SessionCwdIssue:
    sessionCwd: str
    fallbackCwd: str
    sessionFile: str | None = None


class SessionCwdSource(Protocol):
    def getCwd(self) -> str: ...

    def getSessionFile(self) -> str | None: ...


def get_missing_session_cwd_issue(
    session_manager: SessionCwdSource,
    fallback_cwd: str,
) -> SessionCwdIssue | None:
    session_file = session_manager.getSessionFile()
    if not session_file:
        return None

    session_cwd = session_manager.getCwd()
    if not session_cwd or Path(session_cwd).exists():
        return None

    return SessionCwdIssue(
        sessionFile=session_file,
        sessionCwd=session_cwd,
        fallbackCwd=fallback_cwd,
    )


def format_missing_session_cwd_error(issue: SessionCwdIssue) -> str:
    session_file = f"\nSession file: {issue.sessionFile}" if issue.sessionFile else ""
    return (
        f"Stored session working directory does not exist: {issue.sessionCwd}"
        f"{session_file}\nCurrent working directory: {issue.fallbackCwd}"
    )


def format_missing_session_cwd_prompt(issue: SessionCwdIssue) -> str:
    return (
        "cwd from session file does not exist\n"
        f"{issue.sessionCwd}\n\n"
        "continue in current cwd\n"
        f"{issue.fallbackCwd}"
    )


class MissingSessionCwdError(Exception):
    def __init__(self, issue: SessionCwdIssue) -> None:
        super().__init__(format_missing_session_cwd_error(issue))
        self.name = "MissingSessionCwdError"
        self.issue = issue


def assert_session_cwd_exists(session_manager: SessionCwdSource, fallback_cwd: str) -> None:
    issue = get_missing_session_cwd_issue(session_manager, fallback_cwd)
    if issue is not None:
        raise MissingSessionCwdError(issue)


getMissingSessionCwdIssue = get_missing_session_cwd_issue
formatMissingSessionCwdError = format_missing_session_cwd_error
formatMissingSessionCwdPrompt = format_missing_session_cwd_prompt
assertSessionCwdExists = assert_session_cwd_exists

__all__ = [
    "MissingSessionCwdError",
    "SessionCwdIssue",
    "assertSessionCwdExists",
    "formatMissingSessionCwdError",
    "formatMissingSessionCwdPrompt",
    "getMissingSessionCwdIssue",
]
