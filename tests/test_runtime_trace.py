"""Record the same work in both modes and inspect it through the public CLI."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from misaka.agent.request_budget import context_token_upper_bound
from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.api import _meter
from misaka.ai.api._meter import metered
from misaka.ai.api.pi_messages import _serializeContext
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.ai.types import Context, DoneEvent, SimpleStreamOptions, TextContent
from misaka.ai.utils.event_stream import AssistantMessageEventStream
from misaka.cli.app import main
from misaka.cli.trace import report
from misaka.config import home
from misaka.core.runtime_trace import SessionTrace, trace_scope
from misaka.core.session_manager import NewSessionOptions, SessionManager
from misaka.core.tools import createBashTool
from misaka.utils.values import read_field
from tests.suite_harness import createHarness


def _read_call(index):
    return fauxToolCall("read", {"path": "source.txt"}, {"id": f"read-{index}"})


@pytest.mark.parametrize("diagnostics", [False, True])
async def test_sales_trace_aggregates_only_builtin_reads(tmp_path, capsys, diagnostics):
    session_manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    with trace_scope(None, diagnostics):
        async with createHarness(sessionManager=session_manager) as h:
            workspace = Path(h.tempDir)
            (workspace / "source.txt").write_text("north 2000\nsouth 2000\n", encoding="utf-8")
            h.setResponses([
                fauxAssistantMessage([_read_call(index) for index in range(20)], {"stopReason": "toolUse"}),
                fauxAssistantMessage([fauxToolCall("write", {"path": "sales.json", "content": '{"total":4000}'},
                                                  {"id": "publish"})], {"stopReason": "toolUse"}),
                fauxAssistantMessage("done"),
            ])
            await h.session.prompt("SYNTHETIC_PROMPT run the sales task")
            assert json.loads((workspace / "sales.json").read_text(encoding="utf-8")) == {"total": 4000}

    run_id = session_manager.getSessionId()
    assert main(["trace", "show", run_id, "--json"]) == 0
    output = capsys.readouterr().out
    data = json.loads(output)
    assert "SYNTHETIC_PROMPT" not in output and "source.txt" not in output and "sales.json" not in output
    events = data["events"]
    assert events[0]["event"] == "run_started" and events[-1]["event"] == "run_finished"
    assert data["usage"]["requests"] == 3 and data["usage"]["reported"] == 3
    assert [op["state"] for op in data["operations"] if op["call_id"] == "publish"] == ["receipt_saved"]
    assert sum(row["event"] == "tool_entered" and row["tool"] == "read" for row in events) == (20 if diagnostics else 0)
    assert [row["count"] for row in events if row["event"] == "tool_summary"] == ([] if diagnostics else [20])
    assert len({row["request_id"] for row in events if row["event"] == "model_receipt_saved"}) == 3
    assert main(["trace", "last"]) == 0
    assert run_id in capsys.readouterr().out


async def test_same_name_external_read_and_rejected_arguments_are_recorded(tmp_path):
    async def execute(*_args):
        return AgentToolResult(content=[TextContent(text="SECRET_RESULT")], details={})

    tool = AgentTool(name="read", label="External tool", description="External fixture",
                    parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                    execute=execute)
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    async with createHarness(tools=[tool], sessionManager=manager) as h:
        h.setResponses([
            fauxAssistantMessage([_read_call(1), fauxToolCall("read", {}, {"id": "bad-args"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage("done"),
        ])
        await h.session.prompt("Two calls")
    result = report(manager.getSessionId())
    states = {item["call_id"]: item["state"] for item in result["operations"]}
    assert states == {"read-1": "receipt_saved", "bad-args": "not_executed"}
    assert "SECRET_RESULT" not in json.dumps(result)


async def test_truncated_tool_call_has_a_not_executed_receipt(tmp_path):
    def execute(*_args):
        pytest.fail("The model's truncated tool call must not be executed")

    tool = AgentTool(name="fixture", label="Fixture", description="Synthetic operation",
                    parameters={"type": "object"}, execute=execute)
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    async with createHarness(tools=[tool], sessionManager=manager) as fixture:
        fixture.setResponses([
            fauxAssistantMessage([fauxToolCall(tool.name, {}, {"id": "partial"})], {"stopReason": "length"}),
            fauxAssistantMessage("done"),
        ])
        await fixture.session.prompt("Synthetic input")
    data = report(manager.getSessionId())
    [operation] = data["operations"]
    assert operation["call_id"] == "partial" and operation["state"] == "not_executed"
    assert operation["receipt_status"] == "error" and operation["entry_id"]
    [rejected] = [row for row in data["events"] if row["event"] == "tool_rejected"]
    assert rejected["reason"] == "truncated_response"


def test_logging_failure_is_visible_and_recovers(monkeypatch, caplog):
    import misaka.core.runtime_trace as trace

    original = trace.os.fsync

    def fail(_descriptor):
        raise OSError("injected trace sync failure")

    recorder = SessionTrace("failure-fixture")
    with monkeypatch.context() as patch:
        patch.setattr(trace.os, "fsync", fail)
        recorder.observe("tool_entered", {"call_id": "call", "tool": "write", "nested": False, "read_only": False})
    assert trace.os.fsync is original
    assert "incomplete" in caplog.text
    recorder.observe("tool_returned", {"call_id": "call", "status": "ok", "duration_ms": 3})
    snapshot = report("failure-fixture")
    assert snapshot["recording"] == "incomplete"
    assert any(row["event"] == "tool_returned" and row["call_id"] == "call" for row in snapshot["events"])


def test_trace_cli_reports_historical_gaps_without_creating_files(capsys):
    run_id = "r_old"
    path = home.path("runtime_traces") / run_id
    assert not path.exists()
    assert main(["trace", "show", run_id, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["recording"] == "unavailable" and data["board_incomplete"] is False
    assert not path.exists() and not home.path("db").exists()


async def test_research_scope_records_model_calls_outside_agent_loop():
    async with createHarness() as fixture:
        model = fixture.faux.getModel()

        def send(_options):
            stream = AssistantMessageEventStream()
            message = fauxAssistantMessage("synthetic", {"stopReason": "stop"})
            stream.push(DoneEvent(reason="stop", message=message))
            return stream

        with trace_scope("r_auxiliary", False):
            await metered(model, Context(messages=[]), None, send).result()
            await asyncio.sleep(0)  # Run the trace observer's completion callback.
    events = report("r_auxiliary")["events"]
    assert [event["event"] for event in events] == ["model_started", "model_finished"]
    assert events[0]["request_id"] == events[1]["request_id"]
    assert all(event["auxiliary"] for event in events)


async def test_research_budget_trace_shows_admission_and_content_free_denial(tmp_path, monkeypatch, capsys):
    from misaka.core.platform import budget, metering, tasks
    from misaka.core.research import runs

    monkeypatch.setattr(_meter, "_request_meter", metering.meter)
    path = str(home.path("db"))
    with closing(tasks.connect(path)) as con:
        runs.init(con)
        run_id = runs.create(con, workspace=tmp_path, question="Private budget question?")["id"]
        assert budget.settle_request(con, None, run_id, 1, 1_000)
        held = budget.lease_request(con, 10_000, run_id, 1, need=1_000, want=3_000)
        assert held["tokens"] == 3_000

    sent = []

    def send(options):
        sent.append(options.maxTokens)
        stream = AssistantMessageEventStream()
        message = fauxAssistantMessage("Private reply", {"stopReason": "stop"})
        stream.push(DoneEvent(reason="stop", message=message))
        stream.end(message)
        return stream

    context = Context(systemPrompt="Private system prompt", messages=[])
    bound = context_token_upper_bound(context)
    async with createHarness() as fixture:
        model = fixture.faux.getModel().model_copy(update={"maxTokens": 8_000})
        with trace_scope(run_id, False):
            with budget.usage_context(path, run_id, 1, 10_000):
                message = await metered(model, context, None, send).result()
                assert message.stopReason == "stop"
            with budget.usage_context(path, run_id, 1, 5_000):
                message = await metered(model, context, None, send).result()
                assert message.stopReason == "error"

    assert sent == [6_000 - bound]
    assert main(["trace", "show", run_id, "--json"]) == 0
    output = capsys.readouterr().out
    assert all(text not in output for text in ("Private", held["lease"]))
    data = json.loads(output)
    assert data["usage"]["requests"] == 2 and data["usage"]["budget_refused"] == 1
    admitted, refused = [row for row in data["events"] if row["event"].startswith("budget_")]
    assert admitted["event"] == "budget_admitted" and admitted["status"] == "granted"
    assert refused["event"] == "budget_refused" and refused["status"] == "exhausted"
    for event in (admitted, refused):
        assert event["budget_input_bound"] == bound
        assert event["budget_minimum_output"] == 1_024
        assert event["budget_thinking_allowance"] == 0
        assert event["budget_need"] == bound + 1_024
        assert event["budget_desired"] == bound + 8_000
        assert event["budget_remaining"] == event["budget_cap"] - event["budget_used"]
        assert event["budget_held"] == 3_000 and event["budget_scope"] == "board"
    assert admitted["budget_granted"] == 6_000 and admitted["budget_leased"] is True
    assert refused["budget_granted"] == 0 and refused["budget_leased"] is False
    assert main(["trace", "show", run_id]) == 0
    assert f"budget_input_bound={bound}" in capsys.readouterr().out


async def test_research_budget_trace_records_abort_while_other_requests_hold_room(tmp_path, monkeypatch):
    from misaka.core.platform import budget, metering, tasks
    from misaka.core.research import runs

    monkeypatch.setattr(_meter, "_request_meter", metering.meter)
    path = str(home.path("db"))
    with closing(tasks.connect(path)) as con:
        runs.init(con)
        run_id = runs.create(con, workspace=tmp_path, question="Budget wait?")["id"]
        assert budget.lease_request(con, 10_000, run_id, 1, need=1_000, want=9_000)["tokens"] == 9_000

    def send(_options):
        pytest.fail("The wait was aborted before sending")

    options = SimpleStreamOptions(signal=SimpleNamespace(aborted=True))
    async with createHarness() as fixture:
        with trace_scope(run_id, True), budget.usage_context(path, run_id, 1, 10_000):
            message = await metered(fixture.faux.getModel(), Context(messages=[]), options, send).result()
            assert message.stopReason == "aborted"
    data = report(run_id)
    wait, aborted = [row for row in data["events"] if row["event"].startswith("budget_")]
    assert (wait["event"], wait["status"]) == ("budget_wait", "wait")
    assert (aborted["event"], aborted["status"]) == ("budget_aborted", "aborted")
    assert wait["request_id"] == aborted["request_id"]
    assert wait["budget_held"] == aborted["budget_held"] == 9_000
    assert wait["budget_remaining"] == aborted["budget_remaining"] == 10_000
    assert data["usage"]["budget_refused"] == 0


def test_interrupted_diagnostics_retains_seen_http_attempts():
    with trace_scope(None, True):
        trace = SessionTrace("interrupted-http")
        trace.observe("model_started", {"request_id": "interrupted"})
        trace.observe("http_observable", {"request_id": "interrupted"})
        trace.observe("http_attempt", {"request_id": "interrupted", "attempt": 1})
        trace.observe("model_started", {"request_id": "aborted"})
        trace.observe("http_observable", {"request_id": "aborted"})
        trace.observe("model_finished", {"request_id": "aborted", "status": "aborted"})
    summary = report("interrupted-http")["usage"]
    assert summary["requests"] == 2
    assert summary["http_attempts"] == 1 and summary["requests_with_http_counts"] == 2
    assert summary["unfinished_http_counts"] == 1 and summary["missing"] == 2


def test_local_request_identity_does_not_leave_in_pi_messages_payload():
    message = fauxAssistantMessage("synthetic").model_copy(update={"requestId": "private-trace-id"})
    payload = _serializeContext(Context(messages=[message]))
    assert "requestId" not in payload["messages"][0]


async def test_shell_timeout_is_recorded_without_command_text(tmp_path):
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    async with createHarness(tools=[createBashTool(str(tmp_path))], sessionManager=manager) as fixture:
        fixture.setResponses([
            fauxAssistantMessage([fauxToolCall("bash", {"command": "sleep 3 # fixture marker", "timeout": 0.1},
                                               {"id": "tool-timeout"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage("done"),
        ])
        await fixture.session.prompt("Run timeout fixture")
    result = report(manager.getSessionId())
    [event] = [event for event in result["events"] if event["event"] == "tool_timeout"]
    assert event["status"] == "timeout" and event["call_id"] == "tool-timeout"
    assert result["operations"][0]["receipt_status"] == "error"
    assert "fixture marker" not in json.dumps(result)


async def test_parallel_trace_distinguishes_executor_order_from_receipt_order(tmp_path):
    second_returning = asyncio.Event()

    async def execute(call_id, _args, *_rest):
        if call_id == "first":
            await asyncio.wait_for(second_returning.wait(), timeout=5)
        else:
            second_returning.set()
        return AgentToolResult(content=[TextContent(text="ok")], details={})

    tool = AgentTool(name="external", label="Fixture", description="Synthetic parallel operation",
                    parameters={"type": "object"}, execute=execute)
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    async with createHarness(tools=[tool], sessionManager=manager) as fixture:
        fixture.setResponses([
            fauxAssistantMessage([fauxToolCall("external", {}, {"id": call_id}) for call_id in ("first", "second")],
                                 {"stopReason": "toolUse"}),
            fauxAssistantMessage("done"),
        ])
        await fixture.session.prompt("Run both operations")
    events = report(manager.getSessionId())["events"]
    assert [event["call_id"] for event in events if event["event"] == "tool_returned"] == ["second", "first"]
    assert [event["call_id"] for event in events if event["event"] == "tool_receipt_saved"] == ["first", "second"]


@pytest.mark.parametrize("diagnostics", [False, True])
async def test_nested_tool_end_cannot_overwrite_an_executor_result(diagnostics):
    from tests.test_tool_orchestration import orchestrator_extension

    with trace_scope(None, diagnostics):
        async with createHarness(extensionFactories=[orchestrator_extension],
                                 initialActiveToolNames=["run_tools", "helper", "echo"]) as fixture:
            fixture.setResponses([
                fauxAssistantMessage([fauxToolCall("run_tools", {}, {"id": "outer"})], {"stopReason": "toolUse"}),
                fauxAssistantMessage("done"),
            ])
            await fixture.session.prompt("Run registered tool")
            data = report(fixture.session.sessionId)

    states = {op["call_id"]: (op["state"], op["nested"]) for op in data["operations"]}
    assert states["outer/1"] == states["outer/2"] == ("returned", True)
    assert states["outer/3"] == ("not_executed", True)
    assert [row["event"] for row in data["events"] if row.get("call_id") == "outer/2"] == [
        "tool_entered", "tool_returned",
    ]


@pytest.mark.parametrize("failure", [False, True])
async def test_executor_result_is_recorded_while_progress_delivery_waits(failure):
    from misaka.agent.agent_loop import RunToolCallOptions, runToolCall
    from misaka.agent.types import AgentContext
    from misaka.ai.runtime_observation import observation_sink

    delivered = asyncio.Event()
    release = asyncio.Event()

    async def hold_update(_result):
        delivered.set()
        await release.wait()

    async def execute(_id, _args, _signal, update):
        result = AgentToolResult(content=[], details={})
        update(result)
        if failure:
            raise ValueError("executor failure")
        return result

    tool = AgentTool(name="write_fixture", label="Write", description="Synthetic operation",
                    parameters={"type": "object"}, execute=execute)
    trace = SessionTrace("pending-progress")
    with observation_sink(trace.observe):
        task = asyncio.create_task(runToolCall(fauxToolCall(tool.name, {}, {"id": "write"}), RunToolCallOptions(
            tools=[tool], context=AgentContext(tools=[tool], messages=[]),
            assistantMessage=fauxAssistantMessage("synthetic"), onUpdate=hold_update,
        )))
        try:
            await asyncio.wait_for(delivered.wait(), 3)
            [operation] = report("pending-progress")["operations"]
            assert operation["state"] == "returned"
            assert operation["status"] == ("error" if failure else "ok")
            assert "entry_id" not in operation
        finally:
            release.set()
            await asyncio.wait_for(task, 3)


async def test_memory_session_with_path_does_not_claim_disk_receipts(tmp_path):
    path = tmp_path / "explicit.jsonl"
    manager = SessionManager(str(tmp_path), str(tmp_path), str(path), False)
    async with createHarness(sessionManager=manager) as fixture:
        fixture.setResponses([fauxAssistantMessage("done")])
        await fixture.session.prompt("Synthetic input")
    assert not path.exists()
    assert not any(row["event"] == "model_receipt_saved" for row in report(manager.getSessionId())["events"])


@pytest.mark.parametrize("aborted", [False, True], ids=["exception", "abort"])
async def test_runtime_failure_after_a_successful_model_response(monkeypatch, aborted):
    async with createHarness(settings={"retry": {"enabled": False}}) as fixture:
        async def fail_finish_turn(*_args):
            if aborted:
                fixture.session.agent.abort()
            raise RuntimeError("synthetic boundary failure")

        monkeypatch.setattr(fixture.session.agent, "finishTurn", fail_finish_turn)
        fixture.setResponses([fauxAssistantMessage("done")])
        await fixture.session.prompt("Exercise the turn boundary")
        assert fixture.session.agent.state.errorMessage == "synthetic boundary failure"
        data = report(fixture.session.sessionId)

    [request] = [row for row in data["events"] if row["event"] == "model_finished"]
    assert request["status"] == "stop"
    assert data["usage"]["requests"] == data["usage"]["reported"] == 1
    [finished] = [row for row in data["events"] if row["event"] == "run_finished"]
    expected = ("aborted", None) if aborted else ("error", "run_error")
    assert (finished["status"], finished.get("reason")) == expected
    assert "synthetic boundary failure" not in json.dumps(data)


async def test_model_timeout_retains_its_specific_run_status(monkeypatch):
    async def time_out(*_args):
        raise TimeoutError("synthetic model timeout")

    async with createHarness(settings={"retry": {"enabled": False}}) as fixture:
        monkeypatch.setattr(fixture.session.agent, "streamFunction", time_out)
        await fixture.session.prompt("Exercise model timeout")
        data = report(fixture.session.sessionId)

    [request] = [row for row in data["events"] if row["event"] == "model_finished"]
    [finished] = [row for row in data["events"] if row["event"] == "run_finished"]
    assert request["status"] == finished["status"] == "timeout"


async def test_run_finishes_with_the_recovered_provider_outcome():
    settings = {"retry": {"enabled": True, "maxRetries": 1, "baseDelayMs": 0}}
    async with createHarness(settings=settings) as fixture:
        fixture.setResponses([
            fauxAssistantMessage("", {"stopReason": "error", "errorMessage": "overloaded_error"}),
            fauxAssistantMessage("recovered"),
        ])
        await fixture.session.prompt("Exercise retry recovery")
        data = report(fixture.session.sessionId)

    assert [row["status"] for row in data["events"] if row["event"] == "model_finished"] == ["error", "stop"]
    [finished] = [row for row in data["events"] if row["event"] == "run_finished"]
    assert finished["status"] == "stop" and finished.get("reason") is None


async def test_transcript_failure_retains_executor_result_and_has_its_own_stop_reason(tmp_path, monkeypatch):
    manager = SessionManager.create(str(tmp_path), str(tmp_path / "sessions"))
    persist = manager._persist
    artifact = tmp_path / "effects.txt"

    def fail_on_result(entry):
        if entry.get("message", {}).get("role") == "toolResult":
            raise OSError("synthetic transcript write timeout")
        return persist(entry)

    async def execute(*_args):
        with artifact.open("a", encoding="utf-8") as stream:
            stream.write("effect\n")
        return AgentToolResult(content=[], details={})

    monkeypatch.setattr(manager, "_persist", fail_on_result)
    tool = AgentTool(name="write_fixture", label="Write", description="Synthetic operation",
                    parameters={"type": "object"}, execute=execute)
    async with createHarness(sessionManager=manager, tools=[tool],
                             settings={"retry": {"enabled": True, "maxRetries": 2, "baseDelayMs": 0}}) as fixture:
        fixture.setResponses([
            fauxAssistantMessage([fauxToolCall(tool.name, {}, {"id": "write"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage([fauxToolCall(tool.name, {}, {"id": "duplicate"})], {"stopReason": "toolUse"}),
        ])
        await fixture.session.prompt("Synthetic input")
        assert fixture.faux.state.callCount == 1
        assert [read_field(event, "willRetry") for event in fixture.eventsOfType("agent_end")] == [False]
        assert "synthetic transcript write timeout" in fixture.session.agent.state.errorMessage
    assert artifact.read_text(encoding="utf-8") == "effect\n"
    data = report(manager.getSessionId())
    [finished] = [row for row in data["events"] if row["event"] == "run_finished"]
    assert (finished["status"], finished["reason"]) == ("error", "transcript_persist_error")
    [operation] = data["operations"]
    assert operation["state"] == "returned"
    assert "entry_id" not in operation
    assert "synthetic transcript write timeout" not in json.dumps(data)


@pytest.mark.parametrize("run_id", ["review.b2", "A" * 270, "CON"])
def test_run_ids_follow_the_session_contract_and_remain_discoverable(run_id, capsys):
    manager = SessionManager.inMemory(options=NewSessionOptions(id=run_id))
    SessionTrace(manager.getSessionId())
    assert main(["trace", "show", run_id, "--json"]) == 0
    [event] = json.loads(capsys.readouterr().out)["events"]
    assert event["event"] == "run_started" and event["run_id"] == event["session_id"] == run_id
    assert main(["trace", "last"]) == 0
    assert f"Runtime trace {run_id} (present)" in capsys.readouterr().out


async def test_trace_keeps_long_receipt_session_id_for_saved_window_lookup(tmp_path, capsys):
    from misaka.core import session_catalog
    from misaka.core.research import usage

    session_id = "s" * 179
    run_id = "r_long_session"
    header = SessionManager.inMemory(options=NewSessionOptions(id=session_id)).getHeader()
    window = tmp_path / "saved.jsonl"
    window.write_text(json.dumps(header) + "\n", encoding="utf-8")
    manager = SessionManager.open(str(window))
    with trace_scope(run_id, False):
        async with createHarness(sessionManager=manager) as fixture:
            fixture.setResponses([fauxAssistantMessage("done")])
            await fixture.session.prompt("Record this response in the saved window")

    assert SessionManager.open(str(window)).getSessionId() == session_id
    catalog = session_catalog._index_dir()
    catalog.mkdir(parents=True, exist_ok=True)
    session_catalog._write(catalog / "saved.json", {"id": session_id, "path": str(window)})
    assert main(["trace", "show", run_id, "--json"]) == 0
    events = json.loads(capsys.readouterr().out)["events"]
    [receipt] = [row for row in events if row["event"] == "model_receipt_saved"]
    assert receipt["session_id"] == session_id
    assert usage._trace_windows(events, set()) == ({str(window.resolve())}, False)


def test_old_trace_and_valid_rows_survive_bad_rows(capsys):
    run_id = "Old-Trace"
    folder = home.path("runtime_traces") / run_id
    folder.mkdir(parents=True)
    baseline = {"run_id": run_id, "version": 1, "at_ns": 1, "event": "run_started"}
    broken = [
        {**baseline, "event": "tool_returned", "call_id": ["bad"]},
        {**baseline, "event": "model_finished", "request_id": "broken", "usage": {"input": "bad"}},
        {**baseline, "version": 200},
    ]
    (folder / "original.jsonl").write_text("\n".join(map(json.dumps, [baseline, *broken])) + "\n{",
                                          encoding="utf-8")
    assert main(["trace", "show", run_id, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["recording"] == "incomplete"
    assert data["events"] == [baseline]
    assert main(["trace", "last"]) == 0
    assert f"Runtime trace {run_id} (incomplete)" in capsys.readouterr().out


@pytest.mark.parametrize("unreadable", ["root", "directory", "directory_metadata", "segment_metadata"])
def test_trace_cli_reports_unreadable_paths_and_keeps_readable_records(monkeypatch, capsys, unreadable):
    SessionTrace("readable")
    SessionTrace("unreadable")
    root = home.path("runtime_traces")
    denied = root if unreadable == "root" else root / "unreadable"
    if unreadable == "segment_metadata":
        denied, = denied.glob("*.jsonl")
    target, method = (Path, "is_symlink") if unreadable.endswith("_metadata") else (os, "scandir")
    original = getattr(target, method)

    def read(path):
        if Path(path) == denied:
            raise PermissionError("trace path unreadable")
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(target, method, read)
        if unreadable != "root":
            assert main(["trace", "show", "unreadable", "--json"]) == 0
            hidden = json.loads(capsys.readouterr().out)
            assert hidden["recording"] == "incomplete" and hidden["events"] == []
        assert main(["trace", "last"]) == 0
    out = capsys.readouterr().out
    assert "trace inventory incomplete" in out
    if unreadable == "root":
        assert "No readable runtime trace records found." in out
    else:
        assert "Runtime trace readable (present)" in out


@pytest.mark.skipif(os.name != "nt", reason="Windows legacy directory aliases")
def test_trace_last_uses_the_last_matching_record_in_a_windows_alias(capsys):
    root = home.path("runtime_traces")
    folder = root / "OLD-ALIAS"
    folder.mkdir(parents=True)
    if not (root / "old-alias").is_dir():
        pytest.skip("Trace directory has case-sensitive lookup")
    ids = (folder.name, "old-alias", "unrelated")
    rows = [{"run_id": run_id, "version": 1, "at_ns": i, "event": "run_started"}
            for i, run_id in enumerate(ids)]
    (folder / "legacy.jsonl").write_text("\n".join(map(json.dumps, rows)) + "\n{", encoding="utf-8")

    assert main(["trace", "last"]) == 0
    out = capsys.readouterr().out
    assert "trace inventory incomplete" in out
    assert "Runtime trace old-alias (incomplete)" in out
    for row in rows[:2]:
        assert main(["trace", "show", row["run_id"], "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["events"] == [row]


@pytest.mark.parametrize("failure", ["permission", "read_lock"])
def test_trace_reports_unreadable_board_without_hiding_jsonl(monkeypatch, capsys, failure):
    board = home.path("db")
    board.parent.mkdir(parents=True)
    run_id = "r_partial"
    SessionTrace(run_id)
    with closing(sqlite3.connect(board, isolation_level=None)) as writer:
        writer.execute("CREATE TABLE research_runs (id TEXT PRIMARY KEY)")
        if failure == "read_lock":
            writer.execute("BEGIN EXCLUSIVE")
        else:
            stat = Path.stat

            def denied(path, *args, **kwargs):
                if path == board:
                    raise PermissionError("injected Board path error")
                return stat(path, *args, **kwargs)

            monkeypatch.setattr(Path, "stat", denied)
        assert main(["trace", "show", run_id, "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["recording"] == "present" and data["board_incomplete"] is True
        assert data["events"][0]["event"] == "run_started"
        assert data["board_events"] == [] and data["budget"] is None
        assert main(["trace", "show", run_id]) == 0
        assert "Research Board evidence incomplete" in capsys.readouterr().out


@pytest.mark.parametrize("damaged_payload", [None, "{", "[]"])
def test_board_only_research_trace_shows_submission_and_settlement_separately(tmp_path, capsys, damaged_payload):
    from misaka.core.platform import tasks
    from misaka.core.research import runs

    board = home.path("db")
    with closing(tasks.connect(str(board))) as con:
        runs.init(con)
        run = runs.create(con, workspace=tmp_path, question="Private board question?")
        run_id = run["id"]
        node = runs.root(con, run_id)
        card = tasks.create_task(con, "Private card name", assignee="10032", workspace=str(tmp_path))
        runs.link_task(con, run_id, card, kind="research", node=node)
        digest = "e" * 64
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        con.execute("PRAGMA wal_autocheckpoint=0")
        assert con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        checkpointed = board.read_bytes()
        tasks.add_event(con, card, "submitted", {"artifact_digests": {"private.md": digest},
                                                 "summary": "Private summary"}, generation=1)
        assert tasks.add_event(con, card, "budget_usage", {"totalTokens": 128}, generation=1)
        assert main(["trace", "show", run_id, "--json"]) == 0
        live = json.loads(capsys.readouterr().out)
        assert live["board_incomplete"] is False and live["budget"]["recorded_tokens"] == 128
        assert board.read_bytes() == checkpointed
        [submission] = live["board_events"]
        assert submission["event"] == "submitted" and submission["card_id"] == card
        assert submission["generation"] == 1 and submission["artifact_hashes"] == [digest]
        tasks.add_event(con, card, "research_v2_settled", {"submitted_event": submission["board_event_id"]},
                        generation=1)
        failed = tasks.create_task(con, "Stopped card", assignee="10032", workspace=str(tmp_path))
        runs.link_task(con, run_id, failed, kind="research", node=node)
        assert tasks.add_event(con, failed, "gave_up", {"failure_kind": "provider_terminal"}, generation=1)
        if damaged_payload is not None:
            con.execute("UPDATE events SET payload=? WHERE id=?", (damaged_payload, submission["board_event_id"]))
        other = tasks.create_task(con, "Other card", assignee="10032", workspace=str(tmp_path))
        tasks.add_event(con, other, "submitted", {"artifact_digests": {"unrelated.md": "a" * 64}}, generation=1)

    assert main(["trace", "show", run_id, "--json"]) == 0
    output = capsys.readouterr().out
    data = json.loads(output)
    assert data["recording"] == "unavailable"
    assert data["events"] == []
    assert data["board_incomplete"] is (damaged_payload is not None)
    assert data["budget"] == {"recorded_tokens": 128, "reserved_tokens": 0, "expired_reservation_tokens": 0}
    same_submission, settled, stopped = data["board_events"]
    hashes = [digest] if damaged_payload is None else []
    assert same_submission == {**submission, "artifact_hashes": hashes}
    assert settled["event"] == "research_v2_settled" and settled["card_id"] == card
    assert settled["submission_event_id"] == submission["board_event_id"]
    assert settled["artifact_hashes"] == hashes
    assert (stopped["event"], stopped["card_id"], stopped["reason"]) == ("gave_up", failed, "provider_terminal")
    assert "Private" not in output and "private.md" not in output and other not in output
    assert main(["trace", "show", run_id]) == 0
    text = capsys.readouterr().out
    assert "submitted" in text and "research_v2_settled" in text
    assert (digest in text) is (damaged_payload is None)
    assert ("Research Board evidence incomplete" in text) is (damaged_payload is not None)


@pytest.mark.parametrize("folder_name", ["latest", "Newest.Hashed"])
def test_trace_last_continues_past_a_corrupt_latest_segment(capsys, folder_name):
    import time

    from misaka.core.runtime_trace import trace_folders

    SessionTrace("older")
    newer_dir = home.path("runtime_traces") / trace_folders(folder_name)[0]
    newer_dir.mkdir(parents=True)
    corrupt = newer_dir / "999999-corrupt.jsonl"
    corrupt.write_text("{broken\n", encoding="utf-8")
    newer = time.time_ns() + 10_000_000_000
    os.utime(corrupt, ns=(newer, newer))

    assert main(["trace", "last"]) == 0
    out = capsys.readouterr().out
    assert "trace inventory incomplete" in out
    assert "Runtime trace older (present)" in out


async def test_after_tool_call_error_updates_default_mode_readonly_trace_status():
    from misaka.agent.agent_loop import RunToolCallOptions, runToolCall
    from misaka.agent.types import AgentContext
    from misaka.ai.runtime_observation import observation_sink

    async def execute(*_args):
        return AgentToolResult(content=[TextContent(text="ok")], details={})

    async def fail_after(*_args):
        return {"isError": True}

    read_tool = AgentTool(name="read", label="Read", description="Synthetic read",
                          parameters={"type": "object"}, execute=execute)
    read_tool.traceReadOnly = True
    trace = SessionTrace("after-hook-readonly")
    with observation_sink(trace.observe):
        # Nested built-in read-only tool turned into error by afterToolCall
        outcome = await runToolCall(
            fauxToolCall("read", {}, {"id": "nested-read"}),
            RunToolCallOptions(
                tools=[read_tool],
                context=AgentContext(tools=[read_tool], messages=[]),
                assistantMessage=fauxAssistantMessage("synthetic"),
                afterToolCall=fail_after,
            ),
        )
        assert outcome.isError is True
        # Top-level built-in read-only tool turned into error by afterToolCall
        trace.observe("tool_entered", {"call_id": "top-read", "tool": "read", "read_only": True, "nested": False})
        trace.observe("tool_returned", {"call_id": "top-read", "duration_ms": 1, "status": "ok"})
        trace.event({"type": "tool_execution_end", "toolCallId": "top-read", "isError": True})
        trace.event({"type": "turn_end"})

    data = report("after-hook-readonly")
    assert not any(row["event"] == "tool_summary" for row in data["events"])
    failed = {row["call_id"]: row["status"] for row in data["events"] if row["event"] == "tool_failed"}
    assert failed == {"nested-read": "error", "top-read": "error"}
