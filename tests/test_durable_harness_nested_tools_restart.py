"""pi durable's ``test/harness-nested-tools-restart.test.ts``."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage, fauxText, fauxToolCall
from misaka.durable import (
    LiveDoc,
    ToolResultEntry,
    ToolTask,
    defineExtension,
    defineTask,
    defineTool,
    watchEvents,
)
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from tests.chat_support import allEntries, chatSetup, openChat
from tests.durable_support import context, toMatchObject
from tests.harness_support import addHooks, addTool
from tests.task_support import aborted, deferred, eventually

Parameters = {"type": "object", "properties": {"text": {"type": "string"}}}


def tool(name: str, execute: Callable[..., Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return defineTool({"name": name, "description": name, "parameters": Parameters, "execute": execute, **(extra or {})})


def call(name: str, id: str = "c1") -> Any:
    return fauxAssistantMessage([fauxToolCall(name, {}, {"id": id})], {"stopReason": "toolUse"})


DONE = fauxAssistantMessage([fauxText("done")])


@pytest.fixture
def path() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="pi-durable-nested-restart-")
    yield os.path.join(directory, "session.sqlite")
    shutil.rmtree(directory, ignore_errors=True)


async def open(path: str, setup: Any) -> Any:
    """Open the SQLite Harness without starting scheduling."""
    return await openChat(await openNodeSqliteStorage(path), setup)


async def nestedTasks(harness: Any) -> list[dict[str, Any]]:
    page = await harness.commit(lambda tx: tx.scanTasks({"kind": ToolTask["definition"]["name"]}, 100), context)
    return [task for task in page["items"] if task["input"]["kind"] == "nested"]


def results(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry["model"][0] for entry in entries if ToolResultEntry.is_(entry)]


async def collectEnds(harness: Any, conversationId: int) -> SimpleNamespace:
    """Collect tool end events from now on."""
    ends: list[dict[str, Any]] = []
    stream = await watchEvents(harness, conversationId, context)

    async def onBatch(batch: Any, *_: Any) -> None:
        ends.extend(event for event in batch if event["type"] == "tool_execution_end")

    stream.start(onBatch)
    return SimpleNamespace(ends=ends, stop=stream.stop)


def codes(result: dict[str, Any] | None) -> list[str]:
    return [diagnostic["code"] for diagnostic in (result or {}).get("diagnostics") or [] if diagnostic.get("code") is not None]


async def abortedOutcome(_task: Any, runtime: Any, abortContext: Any) -> None:
    await runtime.commit(lambda *_: {"status": "terminal", "outcome": {"status": "aborted"}}, abortContext)


def worker(name: str, phases: list[str] | None, started: Any, aborts: list[Any] | None = None) -> dict[str, Any]:
    """A task that runs until aborted; its abort handler records the abort reason and ends it ``aborted``."""

    async def work(_task: Any, runtime: Any, *_: Any) -> None:
        if phases is not None:
            phases.append("work")
        started.resolve()
        await aborted(runtime.signal)

    async def abort(task: Any, runtime: Any, abortContext: Any) -> None:
        if aborts is not None:
            aborts.append(task.get("abortReason"))
        await abortedOutcome(task, runtime, abortContext)

    return defineTask(
        {"name": name, "version": 1, "initial": lambda input: {"phase": "work"}, "phases": {"work": work}, "abort": abort}
    )


def spawnTools(setup: Any, Worker: dict[str, Any], holder: dict[str, Any]) -> None:
    """``spawn`` creates and awaits a ``Worker`` it owns; ``batch`` runs ``spawn`` as a nested call."""

    async def spawn(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        holder["worker"] = await api.createTask(Worker, None, {"ownership": {"kind": "task", "taskId": api.taskId}}, callContext)
        await api.waitForTask(holder["worker"], callContext)
        return {}

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await api.executeTool("spawn", {}, callContext)
        return {}

    addTool(setup.registry, tool("spawn", spawn))
    addTool(setup.registry, tool("batch", batch))


async def test_abandons_a_replay_unsafe_callers_unfinished_nested_calls_before_they_run_again(path: str):
    setup = chatSetup()
    runs = {"started": 0, "gated": 0}
    running = deferred()
    gated = deferred()

    # Replay-safe, so only the abandonment keeps it from rerunning.
    async def started(_args: Any, api: Any, callContext: Any) -> Any:
        runs["started"] += 1
        api.output("partial\n")
        await api.details({"step": 1}, callContext)
        running.resolve()
        return await aborted(callContext.abortSignal)

    async def gatedTool(*_: Any) -> dict[str, Any]:
        runs["gated"] += 1
        return {}

    addTool(setup.registry, tool("started", started, {"replay": "safe"}))
    addTool(setup.registry, tool("gated", gatedTool))

    # Holds `gated` in its `call` phase, before intent, until the Harness closes.
    async def beforeTool(hookCall: Any, _api: Any, hookContext: Any) -> Any:
        if hookCall["name"] != "gated":
            return None
        gated.resolve()
        return await aborted(hookContext.abortSignal)

    addHooks(setup.registry, ToolTask, {"beforeTool": beforeTool})

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        first = api.executeTool("started", {}, callContext)
        second = api.executeTool("gated", {}, callContext)
        await asyncio.gather(first, second)
        return {}

    addTool(setup.registry, tool("batch", batch))
    setup.faux.setResponses([call("batch"), DONE])
    opened = await open(path, setup)
    id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
    await asyncio.gather(running.promise, gated.promise)
    await opened.harness.close(context)

    opened = await open(path, setup)
    collected = await collectEnds(opened.harness, opened.root.id)
    opened.harness.resume()
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    await opened.harness.waitForIdle(context)
    await collected.stop()
    assert runs == {"started": 1, "gated": 0}
    startedTask, gate = await nestedTasks(opened.harness)
    for task in [startedTask, gate]:
        toMatchObject(task, {"abandonOnRestart": True, "abortRequested": True, "abortReason": "restart"})
        toMatchObject(task["state"], {"status": "terminal", "outcome": {"status": "aborted"}})

    def end(callId: str) -> Any:
        return next((event for event in collected.ends if event["toolCallId"] == callId), {}).get("result")

    assert codes(end("c1/1")) == ["interrupted"]
    toMatchObject(end("c1/1"), {"isError": True, "details": {"step": 1}})
    assert "structuredOutput" not in end("c1/1")
    assert codes(end("c1/2")) == ["abandoned"]
    # The caller reports the interruption.
    [result] = results(await allEntries(opened.root))
    toMatchObject(result, {"toolCallId": "c1", "isError": True})
    assert await opened.harness.snapshot(LiveDoc, opened.root.id, context) == {}
    await opened.harness.close(context)


async def test_abandons_the_work_an_abandoned_nested_call_owns_which_never_runs_a_phase_again(path: str):
    setup = chatSetup()
    phases: list[str] = []
    aborts: list[Any] = []
    started = deferred()
    Worker = worker("test.worker", phases, started, aborts)
    setup.registry.install(defineExtension({"name": "worker", "tasks": [Worker]}))
    holder: dict[str, Any] = {}
    spawnTools(setup, Worker, holder)
    setup.faux.setResponses([call("batch"), DONE])
    opened = await open(path, setup)
    id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
    await started.promise
    await opened.harness.close(context)

    opened = await open(path, setup)
    opened.harness.resume()
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    await opened.harness.waitForIdle(context)
    assert phases == ["work"]
    # The cascade passed the restart reason on.
    assert aborts == ["restart"]
    toMatchObject(
        (await opened.harness.getTask(holder["worker"], context))["state"],
        {"status": "terminal", "outcome": {"status": "aborted"}},
    )
    await opened.harness.close(context)


async def test_keeps_an_abandoned_task_whose_definition_is_missing_waiting_until_it_is_installed_then_runs_its_abort(
    path: str,
):
    setup = chatSetup()
    aborts: list[Any] = []
    started = deferred()
    Worker = worker("test.late", None, started, aborts)
    extension = defineExtension({"name": "late", "tasks": [Worker]})
    setup.registry.install(extension)
    holder: dict[str, Any] = {}
    spawnTools(setup, Worker, holder)
    setup.faux.setResponses([call("batch"), DONE])
    opened = await open(path, setup)
    id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
    await started.promise
    await opened.harness.close(context)

    setup.registry.uninstall(extension)
    opened = await open(path, setup)
    opened.harness.resume()

    async def blocked() -> bool:
        inspected = next(
            (task for task in (await opened.harness.inspect(context))["tasks"] if task["record"]["id"] == holder["worker"]),
            None,
        )
        return inspected is not None and inspected["state"]["kind"] == "blocked"

    await eventually(blocked)
    assert (await opened.harness.getTask(holder["worker"], context))["state"]["status"] != "terminal"
    assert aborts == []
    setup.registry.install(extension)
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    assert aborts == ["restart"]
    toMatchObject(
        (await opened.harness.getTask(holder["worker"], context))["state"],
        {"status": "terminal", "outcome": {"status": "aborted"}},
    )
    await opened.harness.close(context)


async def test_orphans_an_abandoned_task_whose_definition_is_missing_once_an_abort_is_requested(path: str):
    setup = chatSetup()
    started = deferred()
    Worker = worker("test.gone", None, started)
    extension = defineExtension({"name": "gone", "tasks": [Worker]})
    setup.registry.install(extension)
    holder: dict[str, Any] = {}
    spawnTools(setup, Worker, holder)
    setup.faux.setResponses([call("batch"), DONE])
    opened = await open(path, setup)
    id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
    await started.promise
    await opened.harness.close(context)

    setup.registry.uninstall(extension)
    opened = await open(path, setup)
    opened.harness.resume()

    async def marked() -> bool:
        record = await opened.harness.getTask(holder["worker"], context)
        return record is not None and record.get("abortReason") == "restart"

    await eventually(marked)
    await opened.root.abort(context)
    toMatchObject(
        (await opened.harness.getTask(holder["worker"], context))["state"],
        {"status": "terminal", "outcome": {"status": "orphaned", "reason": "missing_task"}},
    )
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "unanswered"
    await opened.harness.close(context)


async def test_marks_nothing_when_the_reopened_harness_never_starts_scheduling(path: str):
    setup = chatSetup()
    running = deferred()

    async def hang(_args: Any, _api: Any, callContext: Any) -> Any:
        running.resolve()
        return await aborted(callContext.abortSignal)

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await api.executeTool("hang", {}, callContext)
        return {}

    addTool(setup.registry, tool("hang", hang))
    addTool(setup.registry, tool("batch", batch))
    setup.faux.setResponses([call("batch"), DONE])
    opened = await open(path, setup)
    await opened.root.submit({"type": "input", "content": "go"}, context)
    await running.promise
    await opened.harness.close(context)

    # An inspection-only open: no resume, no submission.
    opened = await open(path, setup)
    inspected = await opened.harness.inspect(context)
    assert inspected["scheduling"] == "paused"
    await opened.harness.close(context)

    opened = await open(path, setup)
    [nested] = await nestedTasks(opened.harness)
    toMatchObject(nested, {"abandonOnRestart": True, "abortRequested": False})
    assert nested["state"]["status"] == "pending"
    await opened.harness.close(context)


async def test_lets_a_replay_safe_callers_unfinished_nested_calls_recover_and_reattaches_to_them_by_key(path: str):
    setup = chatSetup()
    runs = {"safe": 0, "unsafe": 0, "caller": 0}
    running = {"safe": deferred(), "unsafe": deferred()}

    async def safe(_args: Any, _api: Any, callContext: Any) -> dict[str, Any]:
        runs["safe"] += 1
        if runs["safe"] == 1:
            running["safe"].resolve()
            await aborted(callContext.abortSignal)
        return {"output": [{"type": "text", "text": f"safe run {runs['safe']}"}]}

    async def unsafe(_args: Any, _api: Any, callContext: Any) -> Any:
        runs["unsafe"] += 1
        running["unsafe"].resolve()
        return await aborted(callContext.abortSignal)

    addTool(setup.registry, tool("safe", safe, {"replay": "safe"}))
    addTool(setup.registry, tool("unsafe", unsafe))
    received: list[dict[str, Any]] = []

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        runs["caller"] += 1
        received[:] = await asyncio.gather(
            api.executeTool("safe", {}, callContext),
            api.executeTool("unsafe", {}, callContext),
        )
        return {}

    addTool(setup.registry, tool("batch", batch, {"replay": "safe"}))
    setup.faux.setResponses([call("batch"), DONE])
    opened = await open(path, setup)
    id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
    await asyncio.gather(running["safe"].promise, running["unsafe"].promise)
    await opened.harness.close(context)

    opened = await open(path, setup)
    opened.harness.resume()
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    assert runs == {"safe": 2, "unsafe": 1, "caller": 2}
    # Default keys follow call order, so the rerun found both calls.
    assert len(await nestedTasks(opened.harness)) == 2
    for task in await nestedTasks(opened.harness):
        assert "abandonOnRestart" not in task
        assert "abortReason" not in task
    assert received[0].get("structuredOutput") == "safe run 2"
    assert codes(received[1]) == ["interrupted"]
    await opened.harness.close(context)


async def test_clears_the_slots_of_nested_calls_an_earlier_attempt_finished_when_the_rerun_makes_none(path: str):
    setup = chatSetup()
    finished = deferred()
    attempts = SimpleNamespace(count=0)

    async def quick(*_: Any) -> dict[str, Any]:
        return {"output": [{"type": "text", "text": "ok"}]}

    async def caller(_args: Any, api: Any, callContext: Any) -> Any:
        attempts.count += 1
        if attempts.count == 1:
            await api.executeTool("quick", {}, callContext)
            finished.resolve()
            return await aborted(callContext.abortSignal)
        return {"output": [{"type": "text", "text": "done"}]}

    addTool(setup.registry, tool("quick", quick))
    addTool(setup.registry, tool("caller", caller, {"replay": "safe"}))
    setup.faux.setResponses([call("caller"), DONE])
    opened = await open(path, setup)
    id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
    await finished.promise
    # The finished nested call's slot is still listed, below its running caller.
    assert len((await opened.harness.snapshot(LiveDoc, opened.root.id, context))["nestedTools"]) == 1
    await opened.harness.close(context)

    opened = await open(path, setup)
    holder: dict[str, Any] = {}

    def onCommit(publication: Any, *_: Any) -> None:
        for change in publication["changes"]:
            if change["type"] != "task" or change["value"]["kind"] != ToolTask["definition"]["name"]:
                continue
            if change["value"]["input"]["kind"] != "model" or change["value"]["state"]["status"] != "terminal":
                continue
            live = next(
                (each for each in publication["changes"] if each["type"] == "document" and each["record"]["kind"] == "pi.live"),
                None,
            )
            value = live["value"] if live is not None else None
            holder["slotsAfterCaller"] = len((value or {}).get("nestedTools") or [])

    opened.harness.subscribeCommits(onCommit)
    opened.harness.resume()
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    assert attempts.count == 2
    # The caller's terminal commit removed the slot, though the rerun made no nested call.
    assert holder.get("slotsAfterCaller") == 0
    await opened.harness.close(context)


async def test_aborts_a_nested_call_an_earlier_attempt_of_a_replay_safe_caller_left_running_and_the_rerun_never_made_again(
    path: str,
):
    setup = chatSetup()
    running = deferred()
    attempts = SimpleNamespace(count=0)

    async def slow(_args: Any, _api: Any, callContext: Any) -> Any:
        running.resolve()
        return await aborted(callContext.abortSignal)

    async def caller(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        attempts.count += 1
        # Only the first attempt calls `slow`; the rerun after the restart returns at once.
        if attempts.count == 1:
            await api.executeTool("slow", {}, callContext, {"key": "slow"})
        return {"output": [{"type": "text", "text": "done"}]}

    addTool(setup.registry, tool("slow", slow, {"replay": "safe"}))
    addTool(setup.registry, tool("caller", caller, {"replay": "safe"}))
    setup.faux.setResponses([call("caller"), DONE])
    opened = await open(path, setup)
    id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
    await running.promise
    await opened.harness.close(context)

    opened = await open(path, setup)
    opened.harness.resume()
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    assert attempts.count == 2
    # The rerun's settlement found the leftover call among the tasks it owns and aborted it.
    [leftover] = await nestedTasks(opened.harness)
    toMatchObject(leftover["state"], {"status": "terminal", "outcome": {"status": "aborted"}})
    await opened.harness.close(context)


async def test_reattaches_a_replay_safe_caller_by_default_and_explicit_keys_and_rejects_a_rerun_that_calls_differently():
    for order in ["same", "swapped"]:
        directory = tempfile.mkdtemp(prefix="pi-durable-nested-restart-")
        try:
            path = os.path.join(directory, "session.sqlite")
            setup = chatSetup()
            runs: list[str] = []

            async def echo(args: Any, *_: Any, runs: list[str] = runs) -> dict[str, Any]:
                runs.append(args.get("text") or "")
                return {"output": [{"type": "text", "text": f"echo {args.get('text')}"}]}

            addTool(setup.registry, tool("echo", echo))
            blocked = deferred()
            state: dict[str, Any] = {"callerRuns": 0, "seen": []}

            async def batch(
                _args: Any, api: Any, callContext: Any, order: str = order, blocked: Any = blocked, state: Any = state
            ) -> dict[str, Any]:
                state["callerRuns"] += 1
                first = "b" if state["callerRuns"] == 2 and order == "swapped" else "a"
                one = await api.executeTool("echo", {"text": first}, callContext)
                named = await api.executeTool("echo", {"text": "named"}, callContext, {"key": "named"})
                if state["callerRuns"] == 1:
                    blocked.resolve()
                    await aborted(callContext.abortSignal)
                state["seen"] = [result.get("structuredOutput") for result in [one, named]]
                return {}

            addTool(setup.registry, tool("batch", batch, {"replay": "safe"}))
            setup.faux.setResponses([call("batch"), DONE])
            opened = await open(path, setup)
            id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
            await blocked.promise
            await opened.harness.close(context)

            opened = await open(path, setup)
            opened.harness.resume()
            assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
            [result] = results(await allEntries(opened.root))
            if order == "same":
                assert runs == ["a", "named"]
                assert state["seen"] == ["echo a", "echo named"]
                assert result["isError"] is False
            else:
                # Key 1 was made with other arguments: the rerun throws instead of reusing a different call's result.
                assert runs == ["a", "named"]
                assert result["isError"] is True
                assert "Nested call c1/1 was already made with another tool" in json.dumps(result["content"])
            await opened.harness.close(context)
        finally:
            shutil.rmtree(directory, ignore_errors=True)


async def test_abandons_the_child_tasks_a_replay_unsafe_tool_created_and_awaited_in_memory(path: str):
    setup = chatSetup()
    phases: list[str] = []
    started = deferred()

    async def work(_task: Any, runtime: Any, *_: Any) -> None:
        phases.append("work")
        started.resolve()
        await aborted(runtime.signal)

    async def abort(task: Any, runtime: Any, abortContext: Any) -> None:
        phases.append(f"abort {task.get('abortReason')}")
        await abortedOutcome(task, runtime, abortContext)

    Worker = defineTask(
        {
            "name": "test.direct-worker",
            "version": 1,
            "initial": lambda input: {"phase": "work"},
            "phases": {"work": work},
            "abort": abort,
        }
    )
    setup.registry.install(defineExtension({"name": "direct-worker", "tasks": [Worker]}))

    async def workTool(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        id = await api.createTask(Worker, None, {"ownership": {"kind": "task", "taskId": api.taskId}}, callContext)
        await api.waitForTask(id, callContext)
        return {}

    addTool(setup.registry, tool("work", workTool))
    setup.faux.setResponses([call("work"), DONE])
    opened = await open(path, setup)
    id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
    await started.promise
    await opened.harness.close(context)

    opened = await open(path, setup)
    opened.harness.resume()
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    assert phases == ["work", "abort restart"]
    await opened.harness.close(context)


async def test_leaves_background_work_under_an_abandoned_nested_call_running_and_settles_without_it(path: str):
    setup = chatSetup()
    started = deferred()
    Background = worker("test.background", None, started)
    setup.registry.install(defineExtension({"name": "background", "tasks": [Background]}))
    holder: dict[str, Any] = {}

    async def spawn(_args: Any, api: Any, callContext: Any) -> Any:
        # A conversation the call owns, with background work in it, as a persistent subagent has.
        async def create(tx: Any) -> Any:
            child = await tx.createConversation({"ownership": {"kind": "task", "taskId": api.taskId}})
            return await tx.createTask(
                Background, None, {"conversationId": child["id"], "ownership": {"kind": "conversation"}, "background": True}
            )

        holder["background"] = await api.commit(create, callContext)
        return await aborted(callContext.abortSignal)

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await api.executeTool("spawn", {}, callContext)
        return {}

    addTool(setup.registry, tool("spawn", spawn))
    addTool(setup.registry, tool("batch", batch))
    setup.faux.setResponses([call("batch"), DONE])
    opened = await open(path, setup)
    id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
    await started.promise
    await opened.harness.close(context)

    opened = await open(path, setup)
    opened.harness.resume()
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    [spawned] = await nestedTasks(opened.harness)
    toMatchObject(spawned, {"abortReason": "restart", "state": {"status": "terminal"}})
    toMatchObject(
        await opened.harness.getTask(holder["background"], context),
        {"abortRequested": False, "state": {"status": "running"}},
    )
    await opened.harness.abortTask(holder["background"], context)
    await opened.harness.close(context)

