"""Kill an isolated AgentSession process at known tool and transcript boundaries."""

from __future__ import annotations

import asyncio
import json
import os
import site
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.providers.faux import fauxAssistantMessage, fauxToolCall
from misaka.ai.types import TextContent
from misaka.core.session_manager import SessionManager
from misaka.utils.values import read_field
from tests.suite_harness import createHarness

_REPO = Path(__file__).resolve().parents[1]
_RUN = "r_interrupted_fixture"
_CALL = "call-crash|fc_1"


async def _work(directory: Path, boundary: str) -> None:
    from misaka.core.runtime_trace import trace_scope

    manager = SessionManager.create(str(directory), str(directory / "sessions"))

    def park() -> None:
        detail = {"pid": os.getpid(), "created": psutil.Process().create_time(),
                  "session": manager.getSessionFile()}
        pending = directory / "gate.pending"
        pending.write_text(json.dumps(detail), encoding="utf-8")
        pending.replace(directory / "gate.json")
        threading.Event().wait()

    async def save(_call_id, _args, *_rest):
        with (directory / "effect.txt").open("a", encoding="utf-8", newline="") as handle:
            handle.write("once\n")
            handle.flush()
            os.fsync(handle.fileno())
        if boundary == "effect":
            park()
        return AgentToolResult(content=[TextContent(text="interruption-fixture-output")], details={})

    def stage(event) -> None:
        event_type = read_field(event, "type")
        message = read_field(event, "message")
        if ((boundary == "before" and event_type == "tool_execution_start")
                or (boundary == "returned" and event_type == "message_end"
                    and read_field(message, "role") == "toolResult")
                or (boundary == "receipt" and event_type == "turn_end")):
            park()

    tool = AgentTool(name="save_fixture", label="Save", description="Append one fixture record",
                     parameters={"type": "object", "properties": {}}, execute=save)
    with trace_scope(_RUN, False):
        async with createHarness(tools=[tool], sessionManager=manager) as harness:
            responses = [
                fauxAssistantMessage([fauxToolCall("save_fixture", {}, {"id": _CALL})], {"stopReason": "toolUse"})
                for _ in range(2 if boundary == "reused" else 1)
            ]
            harness.setResponses([*responses, fauxAssistantMessage("Done")])
            harness.session.subscribe(stage)
            await harness.session.prompt("interruption-fixture-user-text")


def _interpreter():
    # On Windows the virtualenv launcher can spawn a different PID. Launch the base interpreter
    # with this environment's packages available so Popen owns the process that parks at the gate.
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(_REPO), *site.getsitepackages()])
    env["PYTHONNOUSERSITE"] = "1"
    return (sys._base_executable if os.name == "nt" else sys.executable), env


def _wait_for_gate(child: subprocess.Popen, gate: Path) -> dict:
    deadline = time.monotonic() + 45
    while not gate.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert gate.exists(), f"Tool never reached its gate; child exit code {child.poll()}"
    return json.loads(gate.read_text(encoding="utf-8"))


def _trace_json(python: str, env: dict[str, str]) -> str:
    result = subprocess.run([python, "-m", "misaka", "trace", "show", _RUN, "--json"], cwd=_REPO, env=env,
                            capture_output=True, encoding="utf-8", timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("boundary", ["before", "effect", "returned", "receipt"])
def test_running_trace_distinguishes_a_tool_effect_from_its_saved_receipt(tmp_path, boundary):
    python, env = _interpreter()
    log = tmp_path / "worker.log"
    with log.open("w", encoding="utf-8") as handle:
        child = subprocess.Popen([python, "-m", "tests.test_runtime_trace_interruption", str(tmp_path), boundary], cwd=_REPO, env=env,
                                 stdin=subprocess.DEVNULL, stdout=handle, stderr=handle)
        try:
            created = psutil.Process(child.pid).create_time()
            try:
                gate = _wait_for_gate(child, tmp_path / "gate.json")
            except AssertionError as error:
                pytest.fail(f"{error}\n{log.read_text(encoding='utf-8')}")
            assert (gate["pid"], gate["created"]) == (child.pid, created)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)

    effect = tmp_path / "effect.txt"
    assert effect.exists() == (boundary != "before")
    if effect.exists():
        assert effect.read_bytes() == b"once\n"
    entries = [json.loads(line) for line in Path(gate["session"]).read_text(encoding="utf-8").splitlines()]
    receipts = [entry["id"] for entry in entries if entry.get("message", {}).get("toolCallId") == _CALL]
    assert len(receipts) == int(boundary == "receipt")

    output = _trace_json(python, env)
    report = json.loads(output)
    assert "interruption-fixture-user-text" not in output
    assert "interruption-fixture-output" not in output
    events = {row["event"] for row in report["events"]}
    assert "run_started" in events and "run_finished" not in events
    matching = [op for op in report["operations"] if op["call_id"] == _CALL]
    if boundary == "before":
        assert not matching
    else:
        assert len(matching) == 1, report["events"]
        [operation] = matching
        assert operation["state"] == {"effect": "unknown", "returned": "returned", "receipt": "receipt_saved"}[boundary]
        if receipts:
            assert operation["entry_id"] == receipts[0]


def test_running_trace_keeps_reused_provider_call_ids_separate(tmp_path):
    python, env = _interpreter()
    subprocess.run([python, "-m", "tests.test_runtime_trace_interruption", str(tmp_path), "reused"], cwd=_REPO, env=env,
                   stdin=subprocess.DEVNULL, timeout=45, check=True)
    assert (tmp_path / "effect.txt").read_bytes() == b"once\nonce\n"

    report = json.loads(_trace_json(python, env))
    assert len(report["operations"]) == 2, report["events"]
    first, second = report["operations"]
    assert first["call_id"] == second["call_id"] == _CALL
    assert first["execution_id"] == second["execution_id"]
    assert first["request_id"] and second["request_id"] and first["request_id"] != second["request_id"]
    assert first["state"] == second["state"] == "receipt_saved"
    assert first["entry_id"] != second["entry_id"]


async def test_trace_classifies_caller_cancellation_during_a_tool():
    from misaka.cli.trace import report
    from misaka.core.runtime_trace import trace_scope

    entered = asyncio.Event()

    async def block_after_entry(*_args):
        entered.set()
        await asyncio.Event().wait()

    tool = AgentTool(name="blocked_fixture", label="Block", description="Wait for test cancellation",
                     parameters={"type": "object", "properties": {}}, execute=block_after_entry)
    with trace_scope(_RUN, False):
        async with createHarness(tools=[tool]) as harness:
            harness.setResponses([
                fauxAssistantMessage([fauxToolCall("blocked_fixture", {}, {"id": _CALL})], {"stopReason": "toolUse"}),
            ])
            prompt = asyncio.create_task(harness.session.prompt("Exercise caller cancellation"))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
            finally:
                prompt.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await prompt

    trace = report(_RUN)
    [finish] = [row for row in trace["events"] if row["event"] == "run_finished"]
    assert finish["status"] == "aborted"
    [operation] = trace["operations"]
    assert operation["state"] == "unknown"


if __name__ == "__main__":
    asyncio.run(_work(Path(sys.argv[1]), sys.argv[2]))
