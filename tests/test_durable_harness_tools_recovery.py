"""pi durable's ``test/harness-tools-recovery.test.ts``."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage, fauxText, fauxToolCall
from misaka.durable import (
    GenerationTask,
    LiveDoc,
    ToolResultEntry,
    ToolTask,
    defineTool,
)
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from misaka.durable.tools import createBashTool
from tests.chat_support import allEntries, chatSetup, openChat, waitFor
from tests.durable_support import AnyOf, context, toMatchObject
from tests.harness_support import addHooks, addTool
from tests.task_support import aborted, deferred


@pytest.fixture
def sqlitePath() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="pi-durable-tools-")
    yield os.path.join(directory, "session.sqlite")
    shutil.rmtree(directory, ignore_errors=True)


async def open(path: str, setup: Any, env: Any = None) -> Any:
    opened = await openChat(await openNodeSqliteStorage(path), setup, {} if env is None else {"env": env})
    opened.harness.resume()
    return opened


def tool(name: str, execute: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return defineTool(
        {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
            "execute": execute,
            **(extra or {}),
        }
    )


def call(name: str, id: str = "c1") -> Any:
    return fauxAssistantMessage([fauxToolCall(name, {}, {"id": id})], {"stopReason": "toolUse"})


DONE = fauxAssistantMessage([fauxText("done")])


def results(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry["model"][0] for entry in entries if ToolResultEntry.is_(entry)]


def text(message: dict[str, Any] | None) -> str:
    return "|".join(item["text"] if item["type"] == "text" else "" for item in (message or {}).get("content", []))


async def liveSlot(harness: Any, conversationId: int) -> dict[str, Any] | None:
    live = await harness.snapshot(LiveDoc, conversationId, context)
    tools = None if live is None else live.get("tools")
    return tools[0] if tools else None  # type: ignore[no-any-return]


async def toolTaskId(harness: Any) -> int:
    found: dict[str, Any] = {}

    async def check() -> bool:
        slot = await liveSlot(harness, 1)
        found["id"] = None if slot is None else slot.get("taskId")
        return found["id"] is not None

    await waitFor(check)
    return found["id"]  # type: ignore[no-any-return]


def blockingTool(name: str, extra: dict[str, Any] | None = None) -> SimpleNamespace:
    """A tool that writes output, then blocks until its invocation is cancelled the first ``blocking`` times it runs.
    ``started`` resolves once the output is durable."""
    started = deferred()
    state = {"runs": 0, "blocking": 1}

    async def execute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        state["runs"] += 1
        api.output(f"run {state['runs']}\n")
        await api.details({"run": state["runs"]}, callContext)
        if state["runs"] <= state["blocking"]:
            started.resolve()
            await aborted(callContext.abortSignal)
        return {}

    return SimpleNamespace(registration=tool(name, execute, extra), started=started, state=state)


async def test_answers_an_unsafe_tool_interrupted_after_intent_with_its_durable_partial_output(sqlitePath):
    setup = chatSetup()
    blocking = blockingTool("work")
    addTool(setup.registry, blocking.registration)
    setup.faux.setResponses([call("work"), DONE])
    opened = await open(sqlitePath, setup)
    id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
    await blocking.started.promise
    taskId = await toolTaskId(opened.harness)
    await opened.harness.close(context)

    opened = await open(sqlitePath, setup)
    record = await opened.harness.getTask(taskId, context)
    toMatchObject(record["state"], {"checkpoint": {"phase": "execute", "arguments": {}, "replay": "unsafe"}})
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    assert blocking.state["runs"] == 1
    [result] = results(await allEntries(opened.root))
    toMatchObject(result, {"isError": True, "details": {"run": 1}})
    assert text(result) == "run 1\n|<harness>\n[error] Tool work was interrupted and may have partially run\n</harness>"
    assert await opened.harness.snapshot(LiveDoc, opened.root.id, context) == {}
    await opened.harness.close(context)


@pytest.mark.parametrize(
    ("stored", "current", "reruns"),
    [("safe", "safe", True), ("safe", "unsafe", False), ("unsafe", "safe", False)],
)
async def test_reruns_a_tool_only_when_both_the_stored_and_the_current_replay_policy_are_safe(
    sqlitePath, stored, current, reruns
):
    setup = chatSetup()
    blocking = blockingTool("work", {"replay": stored})
    registration = addTool(setup.registry, blocking.registration)
    setup.faux.setResponses([call("work"), DONE])
    opened = await open(sqlitePath, setup)
    id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
    await blocking.started.promise
    await opened.harness.close(context)

    registration.dispose()
    addTool(setup.registry, {**blocking.registration, "replay": current})
    opened = await open(sqlitePath, setup)
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    [result] = results(await allEntries(opened.root))
    assert blocking.state["runs"] == (2 if reruns else 1)
    assert result["isError"] is (not reruns)
    if reruns:
        assert text(result) == "run 2\n"
    await opened.harness.close(context)


@pytest.mark.parametrize("change", ["cwd", "deselect"])
async def test_reruns_a_safe_tool_with_the_environment_of_the_conversations_cwd_at_rerun_and_not_once_it_is_deselected(
    sqlitePath, change
):
    setup = chatSetup()
    cwds: list[str | None] = []
    started = deferred()

    async def execute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        cwds.append(None if api.env is None else api.env.cwd)
        if len(cwds) == 1:
            started.resolve()
            await aborted(callContext.abortSignal)
        return {}

    addTool(setup.registry, tool("work", execute, {"replay": "safe"}))
    setup.faux.setResponses([call("work"), DONE])

    def env(target: dict[str, Any], callContext: Any) -> Any:
        return NodeExecutionEnv({"cwd": target.get("cwd", "/")})

    async def reopen() -> Any:
        opened = await openChat(await openNodeSqliteStorage(sqlitePath), setup, {"env": env})
        opened.harness.resume()
        return opened

    opened = await reopen()
    await opened.root.configure({"cwd": "/one"}, context)
    id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
    await started.promise
    reconfigured: dict[str, Any] = {"cwd": "/two"} if change == "cwd" else {"extensions": []}
    await opened.root.configure(reconfigured, context)
    await opened.harness.close(context)

    opened = await reopen()
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    [result] = results(await allEntries(opened.root))
    if change == "cwd":
        assert cwds == ["/one", "/two"]
        assert result["isError"] is False
    else:
        # A tool that no longer resolves is treated as unsafe: interrupted, not rerun.
        assert cwds == ["/one"]
        assert "was interrupted" in text(result)
    await opened.harness.close(context)


async def test_reruns_before_tool_when_interrupted_before_intent_and_executes_once(sqlitePath):
    setup = chatSetup()
    runs = 0

    async def execute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        nonlocal runs
        runs += 1
        return {"output": []}

    addTool(setup.registry, tool("work", execute))
    reached = deferred()
    asked = 0
    decisions: list[str] = []

    async def beforeTool(_call: Any, api: Any, callContext: Any) -> None:
        nonlocal asked
        asked += 1
        # A durable first-writer-wins decision survives the rerun.
        decisions.append(await api.memo("test:decision", f"attempt {asked}", callContext))
        if asked == 1:
            reached.resolve()
            await aborted(callContext.abortSignal)

    addHooks(setup.registry, ToolTask, {"beforeTool": beforeTool})
    setup.faux.setResponses([call("work"), DONE])
    opened = await open(sqlitePath, setup)
    id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
    await reached.promise
    await opened.harness.close(context)

    opened = await open(sqlitePath, setup)
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    assert [asked, runs] == [2, 1]
    assert decisions == ["attempt 1", "attempt 1"]
    await opened.harness.close(context)


async def test_reruns_the_generation_tools_phase_interrupted_before_its_commit(sqlitePath):
    setup = chatSetup()

    async def execute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": []}

    addTool(setup.registry, tool("work", execute))
    reached = deferred()
    observed = 0

    async def afterTools(_assistant: Any, _results: Any, _api: Any, callContext: Any) -> None:
        nonlocal observed
        observed += 1
        if observed == 1:
            reached.resolve()
            await aborted(callContext.abortSignal)

    addHooks(setup.registry, GenerationTask, {"afterTools": afterTools})
    setup.faux.setResponses([call("work"), DONE])
    opened = await open(sqlitePath, setup)
    id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
    await reached.promise
    await opened.harness.close(context)

    opened = await open(sqlitePath, setup)
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    assert observed == 2
    assert [entry["kind"] for entry in await allEntries(opened.root)] == [
        "pi.user",
        "pi.system",
        "pi.assistant",
        "pi.tool-result",
        "pi.assistant",
    ]
    await opened.harness.close(context)


async def test_answers_an_aborted_tool_with_its_partial_output_and_continues_the_run(sqlitePath):
    setup = chatSetup()
    blocking = blockingTool("work")
    addTool(setup.registry, blocking.registration)
    setup.faux.setResponses([call("work"), DONE])
    opened = await open(sqlitePath, setup)
    submission = await opened.root.submit({"type": "input", "content": "go"}, context)
    await blocking.started.promise
    taskId = await toolTaskId(opened.harness)
    assert await opened.harness.abortTask(taskId, context) == "marked"
    settled = await opened.harness.waitForTask(taskId, context)
    toMatchObject(settled["state"]["outcome"], {"status": "aborted", "result": {"entryId": AnyOf(int)}})
    assert (await submission.wait(context))["status"] == "done"
    [result] = results(await allEntries(opened.root))
    assert text(result) == "run 1\n|<harness>\n[error] Tool work was aborted\n</harness>"
    await opened.harness.close(context)


async def test_lets_context_derivation_answer_a_faulted_tool_and_continues_the_run(sqlitePath):
    setup = chatSetup()

    # A result that is not strict JSON makes the result commit throw, so the scheduler faults the task.
    async def bad(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": [], "details": {"fn": lambda: 1}}

    addTool(setup.registry, tool("bad", bad))
    requests: list[str] = []

    def second(request: Any, *_: Any) -> Any:
        result = next((message for message in request.messages if message.role == "toolResult"), None)
        requests.append(
            "none" if result is None else "|".join(item.text if item.type == "text" else "" for item in result.content)
        )
        return DONE

    setup.faux.setResponses([call("bad"), second])
    opened = await open(sqlitePath, setup)
    settled = await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
    assert settled["status"] == "done"
    assert results(await allEntries(opened.root)) == []
    assert requests == ["Tool result unavailable: history ends before this call completed."]
    tasks = await opened.harness.commit(lambda tx: tx.scanTasks({"conversationId": opened.root.id}, 20), context)
    faulted = next(task for task in tasks["items"] if task["kind"] == "pi.tool")
    toMatchObject(faulted["state"], {"status": "terminal", "outcome": {"status": "faulted"}})
    await opened.harness.close(context)


async def test_answers_a_real_bash_command_interrupted_by_close_and_reopen_then_finishes_the_run(sqlitePath):
    env = NodeExecutionEnv({"cwd": os.path.dirname(sqlitePath)})
    setup = chatSetup()
    addTool(setup.registry, createBashTool())
    setup.faux.setResponses(
        [
            fauxAssistantMessage([fauxToolCall("bash", {"command": "echo started; sleep 30"}, {"id": "b"})], {"stopReason": "toolUse"}),
            DONE,
        ]
    )
    opened = await open(sqlitePath, setup, env)
    id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id

    async def started() -> bool:
        slot = await liveSlot(opened.harness, opened.root.id)
        return slot is not None and slot.get("output") == "started\n"

    await waitFor(started)
    await opened.harness.close(context)

    opened = await open(sqlitePath, setup, env)
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    [result] = results(await allEntries(opened.root))
    assert text(result) == "started\n|<harness>\n[error] Tool bash was interrupted and may have partially run\n</harness>"
    await opened.harness.close(context)


async def test_clears_the_interrupted_attempts_progress_before_a_safe_rerun(sqlitePath):
    setup = chatSetup()
    started = [deferred(), deferred()]
    runs = 0

    async def execute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        nonlocal runs
        run = runs
        runs += 1
        if run == 0:
            api.diagnostic({"severity": "info", "message": "first a"})
            api.diagnostic({"severity": "info", "message": "first b"})
            await api.details({"run": 1, "extra": True}, callContext)
        else:
            api.diagnostic({"severity": "info", "message": "second"})
            await api.details({"run": 2}, callContext)
        started[run].resolve()
        await aborted(callContext.abortSignal)
        return {}

    addTool(setup.registry, tool("work", execute, {"replay": "safe"}))
    setup.faux.setResponses([call("work"), DONE])
    opened = await open(sqlitePath, setup)
    await opened.root.submit({"type": "input", "content": "go"}, context)
    await started[0].promise
    await opened.harness.close(context)

    opened = await open(sqlitePath, setup)
    await started[1].promise
    slot = await liveSlot(opened.harness, opened.root.id)
    assert slot is not None
    assert slot["diagnostics"] == [{"severity": "info", "message": "second"}]
    assert slot["details"] == {"run": 2}
    await opened.harness.close(context)
