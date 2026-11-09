"""A phase reply that spends the whole output cap gets one nudged retry, not a paused run.

2026-09-18 (B2): the root plan turn ran at effort max, thought for all 32 000 output tokens,
never called ``misaka_research_assign``, and the driver paused the run with "request
length" -- which the model then read as "the request was too long" and shrank the plan."""
import pytest

from misaka.ai.utils._output_limit import hit_output_limit, output_limit_error
from misaka.core.research import planner


def test_output_limit_error_names_the_cause_and_the_missing_call():
    text = output_limit_error({"stopReason": "length", "usage": {"output": 32000}})
    assert "output token limit" in text
    assert "32000" in text
    assert "no tool call was made" in text
    assert hit_output_limit(text)
    assert not hit_output_limit("request length")
    assert not hit_output_limit(None)


def _run_command(monkeypatch, replies):
    """Drive ``planner._command`` with scripted ``_call`` replies; returns (calls, outcome)."""
    calls = []
    accepted = {"value": None}

    def fake_call(worker, cfg, prompt, **kwargs):
        calls.append((prompt, kwargs.get("continue_session")))
        reply = replies[min(len(calls), len(replies)) - 1]
        if reply == "accepted":
            accepted["value"] = {"payload": {"status": "ready"}, "session_file": "root.jsonl"}
            return None, "recorded", None
        return None, "", reply

    monkeypatch.setattr(planner, "_call", fake_call)
    monkeypatch.setattr(planner.runs, "action", lambda con, run_id, node_id, key: accepted["value"])
    monkeypatch.setattr(planner.commands, "tool", lambda *args, **kwargs: object())
    monkeypatch.setattr(planner, "findMostRecentSession", lambda directory: None)
    run = {"id": "r1", "workspace": "/tmp", "root_session": "root.jsonl"}
    node = {"id": "b1", "depth": 0}
    try:
        outcome = planner._command(None, run, {}, None, node, "plan prompt", key="plan",
                                   name="misaka_research_assign", description="d", model=None,
                                   validate=lambda value: value, session_dir="/tmp")
    except RuntimeError as error:
        outcome = error
    return calls, outcome


def test_an_output_limit_stop_gets_one_nudged_turn_in_the_same_session(monkeypatch):
    limit = output_limit_error({"usage": {"output": 32000}})
    calls, outcome = _run_command(monkeypatch, [limit, "accepted"])
    assert not isinstance(outcome, RuntimeError)
    assert outcome[1] == "recorded"
    assert len(calls) == 2
    nudge, continued = calls[1]
    assert continued is True
    assert "misaka_research_assign" in nudge
    assert "output token limit" in nudge
    assert "do not redo it" in nudge


def test_a_second_miss_pauses_the_run_with_the_readable_cause(monkeypatch):
    limit = output_limit_error({"usage": {"output": 32000}})
    calls, outcome = _run_command(monkeypatch, [limit, limit])
    assert isinstance(outcome, RuntimeError)
    assert len(calls) == 2
    assert "did not call misaka_research_assign for plan" in str(outcome)
    assert "output token limit" in str(outcome)
    assert "request length" not in str(outcome)


def test_other_failures_are_not_retried(monkeypatch):
    calls, outcome = _run_command(monkeypatch, ["request aborted"])
    assert isinstance(outcome, RuntimeError)
    assert len(calls) == 1
    assert "request aborted" in str(outcome)


def test_a_reply_that_ends_in_prose_gets_one_nudged_turn(monkeypatch):
    """2026-09-28: a dispose turn wrote the revised conclusion as its reply and never called
    ``misaka_research_dispose``; with no retry the node failed and took the whole run with it."""
    calls, outcome = _run_command(monkeypatch, [None, "accepted"])
    assert not isinstance(outcome, RuntimeError)
    assert outcome[1] == "recorded"
    assert len(calls) == 2
    nudge, continued = calls[1]
    assert continued is True
    assert "ended without calling `misaka_research_assign`" in nudge
    assert "output token limit" not in nudge


def test_a_second_prose_reply_fails_as_before(monkeypatch):
    calls, outcome = _run_command(monkeypatch, [None, None])
    assert isinstance(outcome, RuntimeError)
    assert len(calls) == 2
    assert "no command accepted" in str(outcome)


def test_reconcile_nudges_a_reply_that_ends_in_prose(monkeypatch):
    calls = []
    recorded = {"value": None}

    def fake_call(worker, cfg, prompt, **kwargs):
        calls.append((prompt, kwargs.get("continue_session")))
        if len(calls) == 2:
            recorded["value"] = {"round": 1}
        return None, "", None

    monkeypatch.setattr(planner, "_call", fake_call)
    monkeypatch.setattr(planner.runs, "reconcile", lambda con, run_id, round: recorded["value"])
    monkeypatch.setattr(planner.runs, "root", lambda con, run_id: {"id": "b0", "depth": 0})
    monkeypatch.setattr(planner, "_lo_session", lambda run, node: "/tmp")
    monkeypatch.setattr(planner.commands, "reconcile_tool", lambda *args, **kwargs: object())
    monkeypatch.setattr(planner, "findMostRecentSession", lambda directory: None)
    run = {"id": "r1", "workspace": "/tmp", "root_session": "root.jsonl", "limits_json": "{}",
           "question": "why is the sky blue"}
    outcome = planner.reconcile(None, run, {}, None, round=1, material={}, validate=lambda value: value)
    assert outcome == {"round": 1}
    assert planner.user_instructions(run) in calls[0][0]      # B122: what the user allowed opens
    assert len(calls) == 2
    assert "ended without calling `misaka_research_reconcile`" in calls[1][0]
    assert calls[1][1] is True


@pytest.mark.parametrize("message", [
    {"stopReason": "length"},
    {"stopReason": "length", "usage": None},
])
def test_output_limit_error_tolerates_missing_usage(message):
    assert "(0 tokens)" in output_limit_error(message)
