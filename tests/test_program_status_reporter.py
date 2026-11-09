"""Translated from pi's ``coding-agent/test/program-status-reporter.test.ts`` (v1.1.0). #10607"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from misaka.ai.types import AssistantMessage
from misaka.modes.interactive.program_status_reporter import ProgramStatusReporter
from misaka.utils.js import jsonStringify


def setup(sessionName: str | None = None) -> Any:
    reports: list[dict[str, Any]] = []
    terminal = SimpleNamespace(setProgramStatus=reports.append)
    session = SimpleNamespace(name=sessionName)
    reporter = ProgramStatusReporter(lambda: terminal, lambda: session.name)

    def send(*events: Any) -> None:
        for event in events:
            reporter.handleEvent(event)

    def last() -> dict[str, Any]:
        status = dict(reports[-1])
        status.pop("app", None)
        return {key: value for key, value in status.items() if value is not None}

    return SimpleNamespace(reporter=reporter, reports=reports, session=session, send=send, last=last)


def assistantEnd(stopReason: str, errorMessage: str | None = None) -> dict[str, Any]:
    message = AssistantMessage.model_validate(
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "secret assistant output"}],
            "api": "anthropic-messages",
            "provider": "anthropic",
            "model": "test",
            "usage": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 0,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
            "stopReason": stopReason,
            "errorMessage": errorMessage,
            "timestamp": 0,
        }
    )
    return {"type": "message_end", "message": message}


settled = {"type": "agent_settled", "aborted": False}


def compactionEnd(reason: str, aborted: bool = False, errorMessage: str | None = None) -> dict[str, Any]:
    return {
        "type": "compaction_end",
        "reason": reason,
        "result": None,
        "aborted": aborted,
        "willRetry": False,
        "errorMessage": errorMessage,
    }


def test_reports_idle_working_during_a_run_and_done_once_it_settles():
    t = setup("Fix login")
    t.reporter.report()
    assert t.reports[-1] == {"state": "idle", "app": "misaka"}

    t.send({"type": "agent_start"})
    assert t.last() == {"state": "working", "message": "Fix login"}

    t.send(assistantEnd("toolUse"), assistantEnd("stop"))
    assert t.last() == {"state": "working", "message": "Fix login"}

    t.send(settled)
    assert t.last() == {"state": "done", "message": "Fix login"}
    assert "secret assistant output" not in jsonStringify(t.reports)


def test_reports_only_the_outcome_of_the_run_retried_errors_final_errors_and_aborts():
    t = setup()
    t.send({"type": "agent_start"}, assistantEnd("error", "overloaded"), assistantEnd("stop"), settled)
    assert t.last() == {"state": "done"}

    t.send({"type": "agent_start"}, assistantEnd("error", "Invalid API key\n{details}"), settled)
    assert t.last() == {"state": "error", "message": "Invalid API key"}

    t.send({"type": "agent_start"}, assistantEnd("aborted"), {"type": "agent_settled", "aborted": True})
    assert t.last() == {"state": "idle"}

    # Aborted after a successful response, for example in an agent_before_settle hook or a retry delay.
    t.send({"type": "agent_start"}, assistantEnd("stop"), {"type": "agent_settled", "aborted": True})
    assert t.last() == {"state": "idle"}


def test_reports_a_failed_recovery_compaction_as_the_runs_error_unless_a_later_response_succeeds():
    t = setup()
    t.send(
        {"type": "agent_start"},
        assistantEnd("length"),
        {"type": "compaction_start", "reason": "overflow"},
        compactionEnd("overflow", errorMessage="Compaction failed\nstack"),
        settled,
    )
    assert t.last() == {"state": "error", "message": "Compaction failed"}

    t.send(
        {"type": "agent_start"},
        {"type": "compaction_start", "reason": "threshold"},
        compactionEnd("threshold", errorMessage="Compaction failed"),
        assistantEnd("stop"),
        settled,
    )
    assert t.last() == {"state": "done"}


def test_reports_compaction_inside_a_run_and_the_result_of_a_manual_compaction():
    t = setup("Session")
    t.send({"type": "agent_start"}, {"type": "compaction_start", "reason": "threshold"})
    assert t.last() == {"state": "working", "message": "Compacting context"}
    t.send(compactionEnd("threshold"))
    assert t.last() == {"state": "working", "message": "Session"}
    t.send(assistantEnd("stop"), settled)

    t.send({"type": "compaction_start", "reason": "manual"}, compactionEnd("manual"))
    assert t.last() == {"state": "done", "message": "Session"}
    t.send({"type": "compaction_start", "reason": "manual"}, compactionEnd("manual", errorMessage="No model"))
    assert t.last() == {"state": "error", "message": "No model"}
    t.send({"type": "compaction_start", "reason": "manual"}, compactionEnd("manual", aborted=True))
    assert t.last() == {"state": "idle"}


def test_reports_the_most_recent_open_dialog_and_the_underlying_state_once_all_close():
    t = setup()
    t.send({"type": "agent_start"})
    t.reporter.setBlocked("extension-selector", {"kind": "permission", "message": "Allow bash?"})
    t.reporter.setBlocked("login", {"kind": "auth", "message": "Log in to Anthropic"})
    assert t.last() == {"state": "blocked", "kind": "auth", "message": "Log in to Anthropic"}

    # The run settles while the selector is still open.
    t.reporter.setBlocked("login", None)
    t.send(assistantEnd("stop"), settled)
    assert t.last() == {"state": "blocked", "kind": "permission", "message": "Allow bash?"}

    # Reopening a source replaces its dialog instead of stacking a second one.
    t.reporter.setBlocked("extension-selector", {"kind": "question", "message": "Pick one"})
    assert t.last() == {"state": "blocked", "kind": "question", "message": "Pick one"}
    t.reporter.setBlocked("extension-selector", None)
    assert t.last() == {"state": "done"}


def test_sends_each_status_once_and_follows_session_name_changes():
    t = setup("Old")
    t.send({"type": "agent_start"}, {"type": "turn_start"}, assistantEnd("toolUse"))
    t.reporter.report()
    assert len(t.reports) == 1

    t.session.name = "New"
    t.send({"type": "session_info_changed", "name": "New"})
    assert t.last() == {"state": "working", "message": "New"}


def test_returns_to_idle_when_the_session_is_replaced():
    t = setup()
    t.send({"type": "agent_start"}, assistantEnd("stop"), settled)
    t.reporter.reset()
    assert t.last() == {"state": "idle"}
