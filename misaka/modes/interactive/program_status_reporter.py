"""Translated from pi's ``modes/interactive/program-status-reporter.ts`` (v1.1.0)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, TypedDict

from misaka.config import APP_NAME
from misaka.ui.tui import ProgramStatus
from misaka.utils import js_regexp
from misaka.utils.js import jsonStringify, jsTrim
from misaka.utils.js_regexp import RegExp
from misaka.utils.values import read_field


class BlockedStatus(TypedDict):
    kind: Literal["permission", "question", "auth"]
    message: str


_LINE_BREAK = RegExp(r"\r?\n")


def firstLine(text: str | None) -> str:
    line = jsTrim(js_regexp.split(text, _LINE_BREAK, 1)[0]) if text is not None else None
    return line or "Error"


class ProgramStatusReporter:
    """Reports interactive-mode state to the terminal (OSC 7501): `working` during agent runs and
    compaction, `blocked` while a dialog waits for the user, then `done`, `error`, or `idle` once the run
    settles. Messages are limited to the session name, dialog titles, and the first line of errors; prompts
    and assistant output are never reported."""

    def __init__(self, getTerminal: Callable[[], Any], getSessionName: Callable[[], str | None]) -> None:
        self.runActive = False
        self.compacting = False
        # Outcome of the current run, reported once it settles.
        self.runResult: ProgramStatus = {"state": "done"}
        # Status while no run is active.
        self.restingStatus: ProgramStatus = {"state": "idle"}
        # Open dialogs by source, in the order they opened. The most recent one is reported.
        self.blocked: dict[str, BlockedStatus] = {}
        self.lastReport: str | None = None

        self.getTerminal = getTerminal
        self.getSessionName = getSessionName

    def handleEvent(self, event: Any) -> None:
        match read_field(event, "type"):
            case "agent_start":
                self.runActive = True
                self.runResult = {"state": "done"}
            case "message_end":
                # The latest response decides the outcome, so a retried error is replaced by its successful retry.
                message = read_field(event, "message")
                if read_field(message, "role") != "assistant":
                    return
                self.runResult = (
                    {"state": "error", "message": firstLine(read_field(message, "errorMessage"))}
                    if read_field(message, "stopReason") == "error"
                    else {"state": "done"}
                )
            case "compaction_start":
                self.compacting = True
            case "compaction_end":
                self.compacting = False
                aborted = read_field(event, "aborted")
                errorMessage = read_field(event, "errorMessage")
                if self.runActive:
                    # A failed recovery compaction ends the run unless a later response succeeds.
                    if aborted:
                        self.runResult = {"state": "idle"}
                    elif errorMessage:
                        self.runResult = {"state": "error", "message": firstLine(errorMessage)}
                elif aborted:
                    self.restingStatus = {"state": "idle"}
                elif read_field(event, "reason") == "manual":
                    self.restingStatus = (
                        {"state": "error", "message": firstLine(errorMessage)} if errorMessage else {"state": "done"}
                    )
            case "agent_settled":
                self.runActive = False
                self.restingStatus = {"state": "idle"} if read_field(event, "aborted") else self.runResult
            case "session_info_changed":
                # The session name is part of working and done reports.
                pass
            case _:
                return
        self.report()

    def setBlocked(self, source: str, status: BlockedStatus | None) -> None:
        """Report `blocked` for a dialog until it is cleared with `undefined`. Reopening a source replaces it."""
        self.blocked.pop(source, None)
        if status:
            self.blocked[source] = status
        self.report()

    def reset(self) -> None:
        """Forget the previous session's run, for example after switching sessions."""
        self.runActive = False
        self.compacting = False
        self.runResult = {"state": "done"}
        self.restingStatus = {"state": "idle"}
        self.report()

    def report(self) -> None:
        status: ProgramStatus = {**self.currentStatus(), "app": APP_NAME}
        key = jsonStringify(status)
        if key == self.lastReport:
            return
        self.lastReport = key
        self.getTerminal().setProgramStatus(status)

    def currentStatus(self) -> ProgramStatus:
        blocked = [*self.blocked.values()][-1] if self.blocked else None
        if blocked:
            return {"state": "blocked", **blocked}
        if self.compacting:
            return {"state": "working", "message": "Compacting context"}
        status: ProgramStatus = {"state": "working"} if self.runActive else self.restingStatus
        if status["state"] == "working" or status["state"] == "done":
            sessionName = self.getSessionName()
            # `{...status, message: undefined}` drops the key from the JSON report.
            return {**status, "message": sessionName} if sessionName is not None else {**status}
        return status


__all__ = ["BlockedStatus", "ProgramStatusReporter", "firstLine"]
