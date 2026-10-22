"""pi durable's ``test/harness-nested-tools.test.ts``.

Adaptations: a ``Promise`` a tool leaves running is an eager ``asyncio`` task whose failure the test observes; a BigInt
detail, which is not JSON, is a ``frozenset``, which ``copyJson`` rejects the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage, fauxText, fauxToolCall
from misaka.chord.context import withAbortSignal
from misaka.durable import (
    AssistantEntry,
    LiveDoc,
    MemoryStorage,
    NestedCallDoc,
    ToolResultEntry,
    ToolTask,
    UsageDoc,
    defineTool,
    watchEvents,
)
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from misaka.utils.js import UNDEFINED, AbortController
from tests.chat_support import allEntries, chatSetup, openChat
from tests.durable_support import AnyOf, context, toMatchObject
from tests.harness_support import addHooks, addTool
from tests.task_support import aborted, deferred, eventually

Parameters = {"type": "object", "properties": {"text": {"type": "string"}}}
NUMBER = AnyOf(int, float)


def tool(name: str, execute: Callable[..., Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return defineTool({"name": name, "description": name, "parameters": Parameters, "execute": execute, **(extra or {})})


def echoTool(extra: dict[str, Any] | None = None) -> SimpleNamespace:
    """Returns ``echo <text>`` and counts its runs."""
    state = SimpleNamespace(runs=0)

    async def execute(args: Any, *_: Any) -> dict[str, Any]:
        state.runs += 1
        return {"output": [{"type": "text", "text": f"echo {args.get('text') or ''}"}]}

    return SimpleNamespace(registration=tool("echo", execute, extra), state=state)


def call(name: str, args: dict[str, str] | None = None, id: str = "c1") -> Any:
    return fauxAssistantMessage([fauxToolCall(name, args or {}, {"id": id})], {"stopReason": "toolUse"})


DONE = fauxAssistantMessage([fauxText("done")])


def results(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry["model"][0] for entry in entries if ToolResultEntry.is_(entry)]


def text(result: dict[str, Any] | None) -> str:
    """The text of a result message, or the value a schema-less nested call gives programs."""
    if result is None:
        return ""
    if "role" not in result:
        value = result.get("structuredOutput")
        return value if isinstance(value, str) else ""
    return "|".join(item["text"] if item["type"] == "text" else "" for item in result["content"])


async def toolTasks(harness: Any) -> list[dict[str, Any]]:
    page = await harness.commit(lambda tx: tx.scanTasks({"kind": ToolTask["definition"]["name"]}, 100), context)
    return page["items"]  # type: ignore[no-any-return]


async def runOnce(setup: Any, responses: list[Any]) -> SimpleNamespace:
    setup.faux.setResponses(responses)
    opened = await openChat(MemoryStorage(), setup)
    settled = await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
    return SimpleNamespace(
        harness=opened.harness, root=opened.root, status=settled["status"], entries=await allEntries(opened.root)
    )


def consume(future: Any) -> Any:
    """``promise.catch(() => {})``: observe a left-running call's failure."""
    future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return future


def outcomeOf(record: dict[str, Any] | None) -> Any:
    return record["state"]["outcome"] if record is not None and record["state"]["status"] == "terminal" else None


async def collect(harness: Any, conversationId: int, keep: Callable[[dict[str, Any]], bool] | None = None) -> Any:
    """Collect the events of a conversation from now on; returns ``(stream, events)``."""
    stream = await watchEvents(harness, conversationId, context)
    events: list[dict[str, Any]] = []

    async def onBatch(batch: Any, *_: Any) -> None:
        events.extend(event for event in batch if keep is None or keep(event))

    stream.start(onBatch)
    return stream, events


@pytest.fixture
def sqlitePath() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="pi-durable-nested-")
    yield os.path.join(directory, "session.sqlite")
    shutil.rmtree(directory, ignore_errors=True)


async def test_stores_one_document_per_nested_call_keyed_by_its_key_written_only_by_that_call():
    setup = chatSetup()
    echo = echoTool()
    addTool(setup.registry, echo.registration)

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await asyncio.gather(
            api.executeTool("echo", {"text": "a"}, callContext),
            api.executeTool("echo", {"text": "b"}, callContext, {"key": "named"}),
        )
        return {}

    addTool(setup.registry, tool("batch", batch))
    setup.faux.setResponses([call("batch"), DONE])
    storage = MemoryStorage()
    # Per document written, the keys of the nested calls it holds.
    written: list[dict[str, Any]] = []
    commit = storage.commit

    def recording(writes: Any, commitContext: Any) -> Any:
        for write in writes:
            if write["type"] == "document.create":
                written.append({"kind": write["record"]["kind"], "key": write["record"].get("key")})
        return commit(writes, commitContext)

    storage.commit = recording  # type: ignore[method-assign]
    opened = await openChat(storage, setup)
    harness, root = opened.harness, opened.root
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    nested = [write for write in written if write["kind"].startswith("pi.tool.")]
    assert nested == [
        {"kind": NestedCallDoc["definition"]["kind"], "key": "1"},
        {"kind": NestedCallDoc["definition"]["kind"], "key": "named"},
    ]
    await harness.close(context)


async def test_rejects_a_nested_call_of_an_aborted_caller_and_leaves_no_document_for_it():
    setup = chatSetup()
    echo = echoTool()
    addTool(setup.registry, echo.registration)
    started = deferred()
    marked = deferred()
    holder: dict[str, Any] = {}

    async def late(_args: Any, api: Any, *_: Any) -> dict[str, Any]:
        holder["caller"] = api.taskId
        started.resolve()
        await marked.promise
        # Its context is aborted too, so pass one that is not: the admission itself must refuse.
        try:
            await api.executeTool("echo", {"text": "a"}, context)
        except Exception as error:  # noqa: BLE001 - the refusal is what the test checks
            holder["rejected"] = error
        return {}

    addTool(setup.registry, tool("late", late))
    setup.faux.setResponses([call("late"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "input", "content": "go"}, context)
    await started.promise

    async def mark(tx: Any) -> None:
        record = await tx.task(holder["caller"])
        tx.setTask({**record, "abortRequested": True})

    await harness.commit(mark, context)
    marked.resolve()
    await submission.wait(context)
    assert re.search(r"abort mark|aborted|has settled", str(holder.get("rejected")))
    assert await harness.snapshot(NestedCallDoc, holder["caller"], "1", context) is None
    await harness.close(context)


async def test_runs_a_nested_call_as_its_own_tool_task_and_returns_its_result_to_the_caller_not_the_transcript():
    setup = chatSetup()
    echo = echoTool()
    addTool(setup.registry, echo.registration)
    holder: dict[str, Any] = {}

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        holder["nested"] = await api.executeTool("echo", {"text": "a"}, callContext)
        return {"output": [{"type": "text", "text": f"got {text(holder['nested'])}"}]}

    addTool(setup.registry, tool("batch", batch))
    ran = await runOnce(setup, [call("batch"), DONE])
    harness, root = ran.harness, ran.root
    assert ran.status == "done"
    parent, child = await toolTasks(harness)
    assert holder["nested"] == {
        "taskId": child["id"],
        "structuredOutput": "echo a",
        "isError": False,
        "diagnostics": [],
        "durationMs": NUMBER,
    }
    # One result entry: the model-issued call's, which records nothing of its nested calls.
    assert [[result["toolCallId"], text(result)] for result in results(ran.entries)] == [["c1", "got echo a"]]
    assert "nestedCalls" not in results(ran.entries)[0]
    toMatchObject(parent["input"], {"kind": "model", "callId": "c1"})
    assert child["owner"] == parent["id"]
    assert child["input"] == {
        "kind": "nested",
        "parent": parent["id"],
        "parentCallId": "c1",
        "key": "1",
        "call": {"type": "toolCall", "id": "c1/1", "name": "echo", "arguments": {"text": "a"}},
    }
    # The receipt stays small: the result lived in the caller's documents, which retired with it.
    assert child["state"] == {"status": "terminal", "outcome": {"status": "completed", "result": {"kind": "nested"}}}
    assert await harness.snapshot(NestedCallDoc, parent["id"], "1", context) is None
    assert await harness.snapshot(LiveDoc, root.id, context) == {}
    await harness.close(context)


async def test_gives_each_nested_call_its_own_task_and_call_id():
    setup = chatSetup()
    seen: list[dict[str, Any]] = []

    async def who(_args: Any, api: Any, *_: Any) -> dict[str, Any]:
        seen.append({"taskId": api.taskId, "callId": api.callId})
        return {}

    addTool(setup.registry, tool("who", who))

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await asyncio.gather(
            api.executeTool("who", {}, callContext, {"key": "a"}),
            api.executeTool("who", {}, callContext, {"key": "b"}),
        )
        return {}

    addTool(setup.registry, tool("batch", batch))
    ran = await runOnce(setup, [call("batch"), DONE])
    assert sorted(each["callId"] for each in seen) == ["c1/a", "c1/b"]
    assert len({each["taskId"] for each in seen}) == 2
    await ran.harness.close(context)


async def test_runs_the_tool_hooks_on_nested_calls_which_see_their_parent():
    setup = chatSetup()
    echo = echoTool()
    addTool(setup.registry, echo.registration)
    parents: list[Any] = []

    def beforeTool(hookCall: Any, *_: Any) -> Any:
        parents.append(hookCall.get("parent"))
        if hookCall.get("parent") is not None and hookCall["arguments"].get("text") == "secret":
            return {"block": "no secrets"}
        return None

    def afterTool(hookCall: Any, result: Any, *_: Any) -> Any:
        if hookCall.get("parent") is None:
            return None
        return {**result, "output": [{"type": "text", "text": "rewritten"}]}

    addHooks(setup.registry, ToolTask, {"beforeTool": beforeTool, "afterTool": afterTool})
    nested: list[dict[str, Any]] = []

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        nested.append(await api.executeTool("echo", {"text": "secret"}, callContext))
        nested.append(await api.executeTool("echo", {"text": "public"}, callContext))
        return {}

    addTool(setup.registry, tool("batch", batch))
    ran = await runOnce(setup, [call("batch"), DONE])
    harness = ran.harness
    parent = (await toolTasks(harness))[0]
    assert parents == [None, {"taskId": parent["id"], "callId": "c1"}, {"taskId": parent["id"], "callId": "c1"}]
    toMatchObject(
        nested[0],
        {"isError": True, "diagnostics": [{"severity": "error", "code": "blocked", "message": "Tool call blocked: no secrets"}]},
    )
    assert text(nested[1]) == "rewritten"
    assert echo.state.runs == 1
    await harness.close(context)


async def test_returns_an_error_result_for_an_unavailable_tool_and_invalid_arguments():
    setup = chatSetup()
    addTool(setup.registry, echoTool().registration)
    nested: list[dict[str, Any]] = []

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        nested.append(await api.executeTool("missing", {}, callContext))
        nested.append(await api.executeTool("echo", {"text": {"not": "a string"}}, callContext))
        return {}

    addTool(setup.registry, tool("batch", batch))
    ran = await runOnce(setup, [call("batch"), DONE])
    assert [[result["isError"], (result["diagnostics"] or [{}])[0].get("code")] for result in nested] == [
        [True, "tool_unavailable"],
        [True, "invalid_arguments"],
    ]
    await ran.harness.close(context)


async def test_returns_the_call_a_key_already_names_and_rejects_reusing_the_key_for_another_call():
    setup = chatSetup()
    echo = echoTool()
    addTool(setup.registry, echo.registration)
    holder: dict[str, Any] = {}

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await api.executeTool("echo", {"text": "a"}, callContext, {"key": "k"})
        holder["reused"] = await api.executeTool("echo", {"text": "a"}, callContext, {"key": "k"})
        await api.executeTool("echo", {"text": "b"}, callContext, {"key": "k"})
        return {}

    addTool(setup.registry, tool("batch", batch))
    ran = await runOnce(setup, [call("batch"), DONE])
    assert echo.state.runs == 1
    assert text(holder["reused"]) == "echo a"
    assert "Nested call c1/k was already made with another tool or other arguments" in text(results(ran.entries)[0])
    await ran.harness.close(context)


async def test_matches_a_reused_keys_arguments_regardless_of_key_order():
    setup = chatSetup()
    runs = SimpleNamespace(count=0)

    async def pair(args: Any, *_: Any) -> dict[str, Any]:
        runs.count += 1
        return {"output": [{"type": "text", "text": f"{args['a']}{args['b']}"}]}

    addTool(
        setup.registry,
        defineTool(
            {
                "name": "pair",
                "description": "pair",
                "parameters": {
                    "type": "object",
                    "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                    "required": ["a", "b"],
                },
                "execute": pair,
            }
        ),
    )
    nested: list[dict[str, Any]] = []

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        nested.append(await api.executeTool("pair", {"a": "x", "b": "y"}, callContext, {"key": "k"}))
        nested.append(await api.executeTool("pair", {"b": "y", "a": "x"}, callContext, {"key": "k"}))
        return {}

    addTool(setup.registry, tool("batch", batch))
    ran = await runOnce(setup, [call("batch"), DONE])
    assert runs.count == 1
    assert [text(result) for result in nested] == ["xy", "xy"]
    await ran.harness.close(context)


async def test_aborts_the_nested_calls_a_caller_left_running_when_it_returns_keeping_their_details():
    setup = chatSetup()
    running = deferred()

    async def hang(_args: Any, api: Any, callContext: Any) -> Any:
        api.output("partial\n")
        await api.details({"step": 1}, callContext)
        running.resolve()
        return await aborted(callContext.abortSignal)

    addTool(setup.registry, tool("hang", hang))

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        consume(api.executeTool("hang", {}, callContext))
        await running.promise
        return {"output": [{"type": "text", "text": "returned early"}]}

    addTool(setup.registry, tool("batch", batch))
    setup.faux.setResponses([call("batch"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    stream, ends = await collect(harness, root.id, lambda event: event["type"] == "tool_execution_end")
    settled = await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    assert settled["status"] == "done"
    await harness.waitForIdle(context)
    await stream.stop()
    [result] = results(await allEntries(root))
    assert text(result) == "returned early"
    child = (await toolTasks(harness))[1]
    toMatchObject(child["state"], {"status": "terminal", "outcome": {"status": "aborted", "result": {"kind": "nested"}}})
    # The aborted call's result keeps its details; programs get no partial output, which only the model read.
    childEnd = next(event for event in ends if event["toolCallId"] == "c1/1")
    toMatchObject(childEnd, {"result": {"isError": True, "details": {"step": 1}}})
    assert "structuredOutput" not in childEnd["result"]
    # The nested call ends, with its result, before its caller.
    assert [[event["toolCallId"], "result" in event] for event in ends] == [["c1/1", True], ["c1", False]]
    assert await harness.snapshot(LiveDoc, root.id, context) == {}
    await harness.close(context)


USAGE = {
    "input": 1,
    "output": 2,
    "cacheRead": 0,
    "cacheWrite": 0,
    "totalTokens": 3,
    "cost": {"input": 0.1, "output": 0.2, "cacheRead": 0, "cacheWrite": 0, "total": 0.3},
}


async def test_records_a_nested_calls_usage_under_its_tool():
    setup = chatSetup()

    async def paid(*_: Any) -> dict[str, Any]:
        return {"usage": USAGE}

    addTool(setup.registry, tool("paid", paid))

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await api.executeTool("paid", {}, callContext)
        return {}

    addTool(setup.registry, tool("batch", batch))
    ran = await runOnce(setup, [call("batch"), DONE])
    assert (await ran.harness.snapshot(UsageDoc, ran.root.id, context))["tools"] == {"paid": USAGE}
    await ran.harness.close(context)


async def test_reports_nested_calls_in_pi_live_nestedtools_and_as_tool_events_with_their_parent():
    setup = chatSetup()
    release = deferred()
    running = deferred()

    async def slow(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        api.output("working\n")
        await api.details({"step": 1}, callContext)
        running.resolve()
        await release.promise
        return {"output": [{"type": "text", "text": "slow done"}]}

    addTool(setup.registry, tool("slow", slow))

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        result = await api.executeTool("slow", {}, callContext)
        return {"output": [{"type": "text", "text": text(result)}]}

    addTool(setup.registry, tool("batch", batch))
    setup.faux.setResponses([call("batch"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    stream = await watchEvents(harness, root.id, context)
    assert stream.snapshot["nestedTools"] == []
    events: list[dict[str, Any]] = []

    async def onBatch(batch: Any, *_: Any) -> None:
        events.extend(batch)

    stream.start(onBatch)
    submission = await root.submit({"type": "input", "content": "go"}, context)
    await running.promise
    live = await harness.snapshot(LiveDoc, root.id, context)
    assert live["nestedTools"] == [
        {
            "callId": "c1/1",
            "parentCallId": "c1",
            "parentTaskId": NUMBER,
            "name": "slow",
            "taskId": NUMBER,
            "arguments": {},
            "status": "running",
            "output": "working\n",
            "details": {"step": 1},
        }
    ]
    release.resolve()
    await submission.wait(context)
    await harness.waitForIdle(context)
    await stream.stop()
    tools = [
        [event["type"], event["toolCallId"], event.get("parentToolCallId")]
        for event in events
        if event["type"] in ("tool_execution_start", "tool_execution_end")
    ]
    assert tools == [
        ["tool_execution_start", "c1", None],
        ["tool_execution_start", "c1/1", "c1"],
        ["tool_execution_end", "c1/1", "c1"],
        ["tool_execution_end", "c1", None],
    ]
    nestedEnd = next(
        event for event in events if event["type"] == "tool_execution_end" and event["toolCallId"] == "c1/1"
    )
    toMatchObject(nestedEnd, {"result": {"structuredOutput": "slow done", "isError": False}})
    # Events name the call's task and, for a nested call, the calling one.
    parent, child = await toolTasks(harness)
    parentEnd = next(event for event in events if event["type"] == "tool_execution_end" and event["toolCallId"] == "c1")
    toMatchObject(nestedEnd, {"taskId": child["id"], "parentTaskId": parent["id"]})
    toMatchObject(parentEnd, {"taskId": parent["id"]})
    assert "parentTaskId" not in parentEnd
    assert any(
        event["type"] == "tool_execution_update"
        and event["toolCallId"] == "c1/1"
        and event.get("parentToolCallId") == "c1"
        for event in events
    )
    assert await harness.snapshot(LiveDoc, root.id, context) == {}
    await harness.close(context)


async def test_aborts_nested_calls_with_their_caller():
    setup = chatSetup()
    running = deferred()

    async def hang(_args: Any, _api: Any, callContext: Any) -> Any:
        running.resolve()
        return await aborted(callContext.abortSignal)

    addTool(setup.registry, tool("hang", hang))

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await api.executeTool("hang", {}, callContext)
        return {}

    addTool(setup.registry, tool("batch", batch))
    setup.faux.setResponses([call("batch"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "input", "content": "go"}, context)
    await running.promise
    await root.abort(context)
    assert (await submission.wait(context))["status"] == "unanswered"
    parent, child = await toolTasks(harness)
    toMatchObject(outcomeOf(child), {"status": "aborted", "result": {"kind": "nested"}})
    toMatchObject(outcomeOf(parent), {"status": "aborted", "result": {"kind": "model"}})
    assert await harness.snapshot(LiveDoc, root.id, context) == {}
    await harness.close(context)


async def test_aborts_a_running_nested_call_when_its_caller_throws():
    setup = chatSetup()
    running = deferred()

    async def hang(_args: Any, _api: Any, callContext: Any) -> Any:
        running.resolve()
        return await aborted(callContext.abortSignal)

    addTool(setup.registry, tool("hang", hang))

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        consume(api.executeTool("hang", {}, callContext))
        await running.promise
        raise RuntimeError("caller failed")

    addTool(setup.registry, tool("batch", batch))
    ran = await runOnce(setup, [call("batch"), DONE])
    assert ran.status == "done"
    assert "caller failed" in text(results(ran.entries)[0])
    child = (await toolTasks(ran.harness))[1]
    toMatchObject(child["state"], {"status": "terminal", "outcome": {"status": "aborted"}})
    await ran.harness.close(context)


async def test_reattaches_a_replay_safe_caller_to_the_nested_calls_it_made_before_a_restart(sqlitePath: str):
    setup = chatSetup()
    echo = echoTool()
    addTool(setup.registry, echo.registration)
    blocked = deferred()
    runs = SimpleNamespace(count=0)

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        runs.count += 1
        first = await api.executeTool("echo", {"text": "a"}, callContext)
        if runs.count == 1:
            blocked.resolve()
            await aborted(callContext.abortSignal)
        # A call new to the rerun: its slot, after the reattached one's, is found and finishes.
        second = await api.executeTool("echo", {"text": "b"}, callContext)
        return {"output": [{"type": "text", "text": f"run {runs.count}: {text(first)}, {text(second)}"}]}

    addTool(setup.registry, tool("batch", batch, {"replay": "safe"}))
    setup.faux.setResponses([call("batch"), DONE])
    opened = await openChat(await openNodeSqliteStorage(sqlitePath), setup)
    id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
    await blocked.promise
    await opened.harness.close(context)

    opened = await openChat(await openNodeSqliteStorage(sqlitePath), setup)
    opened.harness.resume()
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    assert runs.count == 2
    assert echo.state.runs == 2
    assert text(results(await allEntries(opened.root))[0]) == "run 2: echo a, echo b"
    tasks = await toolTasks(opened.harness)
    assert len([task for task in tasks if task["input"]["kind"] == "nested"]) == 2
    outcome = outcomeOf(tasks[1])
    assert (outcome or {}).get("result", {}).get("kind") == "nested"
    await opened.harness.close(context)


async def test_stops_only_the_wait_when_a_caller_cancels_it_the_call_runs_on_and_its_key_returns_its_result():
    setup = chatSetup()
    cancel = AbortController()
    release = deferred()
    attempts: list[str] = []
    holder: dict[str, Any] = {}

    async def slow(*_: Any) -> dict[str, Any]:
        cancel.abort(RuntimeError("cancelled"))
        await release.promise
        return {"output": [{"type": "text", "text": "slow done"}]}

    addTool(setup.registry, tool("slow", slow))

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        cancelled = withAbortSignal(cancel.signal, callContext)
        try:
            await api.executeTool("slow", {}, cancelled, {"key": "same"})
            attempts.append("first done")
        except Exception:  # noqa: BLE001 - the cancelled wait is what the test checks
            attempts.append("first cancelled")
        release.resolve()
        holder["retry"] = await api.executeTool("slow", {}, callContext, {"key": "same"})
        return {}

    addTool(setup.registry, tool("batch", batch))
    ran = await runOnce(setup, [call("batch"), DONE])
    assert attempts == ["first cancelled"]
    toMatchObject(holder["retry"], {"isError": False, "structuredOutput": "slow done"})
    assert len(await toolTasks(ran.harness)) == 2
    await ran.harness.close(context)


async def test_admits_one_nested_call_for_a_key_used_by_concurrent_calls():
    setup = chatSetup()
    echo = echoTool()
    addTool(setup.registry, echo.registration)
    both: list[dict[str, Any]] = []

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        both.extend(
            await asyncio.gather(
                api.executeTool("echo", {"text": "a"}, callContext, {"key": "k"}),
                api.executeTool("echo", {"text": "a"}, callContext, {"key": "k"}),
            )
        )
        return {}

    addTool(setup.registry, tool("batch", batch))
    ran = await runOnce(setup, [call("batch"), DONE])
    assert echo.state.runs == 1
    assert [text(result) for result in both] == ["echo a", "echo a"]
    assert len(await toolTasks(ran.harness)) == 2
    await ran.harness.close(context)


async def test_aborts_left_running_nested_calls_of_a_caller_interrupted_while_it_cleaned_them_up(sqlitePath: str):
    setup = chatSetup()
    running = deferred()
    cleaning = deferred()
    release = deferred()
    hangRuns = SimpleNamespace(count=0)

    async def hang(_args: Any, api: Any, callContext: Any) -> Any:
        hangRuns.count += 1
        api.output("partial\n")
        await api.details({"step": 1}, callContext)
        running.resolve()
        with contextlib.suppress(Exception):
            await aborted(callContext.abortSignal)
        # The caller's cleanup waits for this abort; hold it there until the Harness closes.
        cleaning.resolve()
        await release.promise
        raise RuntimeError("aborted")

    addTool(setup.registry, tool("hang", hang))

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        consume(api.executeTool("hang", {}, callContext))
        await running.promise
        return {"output": [{"type": "text", "text": "returned early"}]}

    addTool(setup.registry, tool("batch", batch))
    setup.faux.setResponses([call("batch"), DONE])
    opened = await openChat(await openNodeSqliteStorage(sqlitePath), setup)
    id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
    await cleaning.promise
    closed = opened.harness.close(context)
    release.resolve()
    await closed

    opened = await openChat(await openNodeSqliteStorage(sqlitePath), setup)
    opened.harness.resume()
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    await opened.harness.waitForIdle(context)
    # The caller is replay-unsafe: it reports the interruption, and its cleanup aborts the nested call.
    [result] = results(await allEntries(opened.root))
    toMatchObject(result, {"toolCallId": "c1", "isError": True})
    assert "Tool batch was interrupted" in text(result)
    child = (await toolTasks(opened.harness))[1]
    toMatchObject(child["state"], {"status": "terminal", "outcome": {"status": "aborted"}})
    assert hangRuns.count == 1
    assert await opened.harness.snapshot(LiveDoc, opened.root.id, context) == {}
    await opened.harness.close(context)


async def test_aborts_a_nested_call_whose_admission_was_still_committing_when_its_caller_returned():
    setup = chatSetup()
    addTool(setup.registry, echoTool().registration)

    async def hang(_args: Any, _api: Any, callContext: Any) -> Any:
        return await aborted(callContext.abortSignal)

    addTool(setup.registry, tool("hang", hang))

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await api.executeTool("echo", {"text": "warm"}, callContext, {"key": "warm"})
        consume(api.executeTool("hang", {}, callContext, {"key": "late"}))
        return {}

    addTool(setup.registry, tool("batch", batch))
    ran = await runOnce(setup, [call("batch"), DONE])
    assert ran.status == "done"
    late = next(
        task
        for task in await toolTasks(ran.harness)
        if task["input"]["kind"] == "nested" and task["input"]["call"]["name"] == "hang"
    )
    toMatchObject(late["state"], {"status": "terminal", "outcome": {"status": "aborted"}})
    assert await ran.harness.snapshot(LiveDoc, ran.root.id, context) == {}
    await ran.harness.close(context)


async def test_aborts_every_left_running_nested_call_before_waiting_for_any():
    setup = chatSetup()
    bAborted = deferred()
    started = {"a": deferred(), "b": deferred()}

    # A only stops once B was told to stop.
    async def a(*_: Any) -> dict[str, Any]:
        started["a"].resolve()
        await bAborted.promise
        return {}

    async def b(_args: Any, _api: Any, callContext: Any) -> Any:
        started["b"].resolve()
        with contextlib.suppress(Exception):
            await aborted(callContext.abortSignal)
        bAborted.resolve()
        raise RuntimeError("aborted")

    addTool(setup.registry, tool("a", a))
    addTool(setup.registry, tool("b", b))

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        consume(api.executeTool("a", {}, callContext, {"key": "a"}))
        consume(api.executeTool("b", {}, callContext, {"key": "b"}))
        await asyncio.gather(started["a"].promise, started["b"].promise)
        return {}

    addTool(setup.registry, tool("batch", batch))
    ran = await runOnce(setup, [call("batch"), DONE])
    assert ran.status == "done"
    nested = [task for task in await toolTasks(ran.harness) if task["input"]["kind"] == "nested"]
    assert [task["state"]["status"] for task in nested] == ["terminal", "terminal"]
    await ran.harness.close(context)


async def test_rejects_explicit_keys_that_are_empty_have_a_slash_are_proto_or_are_positive_integers():
    setup = chatSetup()
    addTool(setup.registry, echoTool().registration)
    errors: list[str] = []

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        for key in ["", "a/b", "__proto__", "7"]:
            try:
                await api.executeTool("echo", {}, callContext, {"key": key})
            except Exception as error:  # noqa: BLE001 - the rejection is what the test checks
                errors.append(str(error))
        return {}

    addTool(setup.registry, tool("batch", batch))
    ran = await runOnce(setup, [call("batch"), DONE])
    rule = 'must be non-empty, without "/", not "__proto__", and not a positive integer'
    assert errors == [
        f'Nested call key "" {rule}',
        f'Nested call key "a/b" {rule}',
        f'Nested call key "__proto__" {rule}',
        f'Nested call key "7" {rule}',
    ]
    await ran.harness.close(context)


async def test_runs_nested_calls_of_nested_calls_and_ends_them_children_first():
    setup = chatSetup()
    echo = echoTool()
    addTool(setup.registry, echo.registration)

    async def inner(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        result = await api.executeTool("echo", {"text": "deep"}, callContext, {"key": "x"})
        return {"output": [{"type": "text", "text": text(result)}]}

    async def outer(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        result = await api.executeTool("inner", {}, callContext)
        return {"output": [{"type": "text", "text": text(result)}]}

    addTool(setup.registry, tool("inner", inner))
    addTool(setup.registry, tool("outer", outer))
    setup.faux.setResponses([call("outer"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    stream, events = await collect(
        harness, root.id, lambda event: event["type"] in ("tool_execution_start", "tool_execution_end")
    )
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    await harness.waitForIdle(context)
    await stream.stop()
    assert text(results(await allEntries(root))[0]) == "echo deep"
    outerTask, innerTask, deep = await toolTasks(harness)
    assert [innerTask["owner"], deep["owner"]] == [outerTask["id"], innerTask["id"]]
    assert [[event["type"], event["toolCallId"], event.get("parentToolCallId")] for event in events] == [
        ["tool_execution_start", "c1", None],
        ["tool_execution_start", "c1/1", "c1"],
        ["tool_execution_start", "c1/1/x", "c1/1"],
        ["tool_execution_end", "c1/1/x", "c1/1"],
        ["tool_execution_end", "c1/1", "c1"],
        ["tool_execution_end", "c1", None],
    ]
    assert await harness.snapshot(LiveDoc, root.id, context) == {}
    await harness.close(context)


async def test_reports_the_output_of_several_running_nested_calls_and_shows_them_to_a_late_subscriber():
    setup = chatSetup()
    release = deferred()
    running = {"one": deferred(), "two": deferred()}
    wrote = {"one": deferred(), "two": deferred()}

    async def slow(args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        name = args["text"]
        running[name].resolve()
        await wrote[name].promise
        api.output(f"{name}\n")
        await api.details({"name": name}, callContext)
        await release.promise
        return {}

    addTool(setup.registry, tool("slow", slow))

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await asyncio.gather(
            api.executeTool("slow", {"text": "one"}, callContext),
            api.executeTool("slow", {"text": "two"}, callContext),
        )
        return {}

    addTool(setup.registry, tool("batch", batch))
    setup.faux.setResponses([call("batch"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "input", "content": "go"}, context)
    await asyncio.gather(running["one"].promise, running["two"].promise)
    stream = await watchEvents(harness, root.id, context)
    assert [[slot["callId"], slot["status"]] for slot in stream.snapshot["nestedTools"]] == [
        ["c1/1", "running"],
        ["c1/2", "running"],
    ]
    updates: list[list[Any]] = []

    async def onBatch(batch: Any, *_: Any) -> None:
        for event in batch:
            if event["type"] == "tool_execution_update" and event.get("output") is not None:
                updates.append([event["toolCallId"], event["output"]])

    stream.start(onBatch)
    # The second slot writes first, so a wrong slot index in the update path would show.
    wrote["two"].resolve()
    await eventually(lambda: len(updates) == 1)
    wrote["one"].resolve()
    await eventually(lambda: len(updates) == 2)
    release.resolve()
    await submission.wait(context)
    await stream.stop()
    assert updates == [["c1/2", {"set": "two\n"}], ["c1/1", {"set": "one\n"}]]
    await harness.close(context)


async def test_commits_only_the_status_of_a_nested_call_made_with_progress_false_and_keeps_its_output_in_the_result():
    setup = chatSetup()
    running = deferred()
    release = deferred()

    async def chatty(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        api.output("line\n")
        api.diagnostic({"severity": "info", "message": "note"})
        await api.details({"step": 1}, callContext)
        running.resolve()
        await release.promise
        return {}

    addTool(setup.registry, tool("chatty", chatty))
    holder: dict[str, Any] = {}

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        holder["result"] = await api.executeTool("chatty", {}, callContext, {"progress": False})
        return {}

    addTool(setup.registry, tool("batch", batch))
    setup.faux.setResponses([call("batch"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "input", "content": "go"}, context)
    # `details()` resolved before this, so a progress commit would have landed already.
    await running.promise
    [slot] = (await harness.snapshot(LiveDoc, root.id, context))["nestedTools"]
    assert slot == {
        "callId": "c1/1",
        "parentCallId": "c1",
        "parentTaskId": NUMBER,
        "name": "chatty",
        "taskId": NUMBER,
        "arguments": {},
        "status": "running",
    }
    release.resolve()
    await submission.wait(context)
    toMatchObject(
        holder["result"],
        {"structuredOutput": "line\n", "details": {"step": 1}, "diagnostics": [{"severity": "info", "message": "note"}]},
    )
    await harness.close(context)


async def test_loses_the_running_output_of_a_nested_call_made_with_progress_false_when_it_is_aborted():
    setup = chatSetup()
    running = deferred()

    async def chatty(_args: Any, api: Any, callContext: Any) -> Any:
        api.output("line\n")
        api.diagnostic({"severity": "info", "message": "note"})
        await api.details({"step": 1}, callContext)
        running.resolve()
        return await aborted(callContext.abortSignal)

    addTool(setup.registry, tool("chatty", chatty))

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        consume(api.executeTool("chatty", {}, callContext, {"progress": False}))
        await running.promise
        return {}

    addTool(setup.registry, tool("batch", batch))
    setup.faux.setResponses([call("batch"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    stream, ends = await collect(harness, root.id, lambda event: event["type"] == "tool_execution_end")
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    await harness.waitForIdle(context)
    await stream.stop()
    # The abort's result is built from the slot, which never got the output.
    end = next(event for event in ends if event["toolCallId"] == "c1/1")
    result = end["result"]
    toMatchObject(result, {"isError": True})
    assert "structuredOutput" not in result
    assert "details" not in result
    assert [diagnostic.get("code") for diagnostic in result["diagnostics"]] == ["aborted"]
    await harness.close(context)


async def test_shows_a_nested_calls_arguments_in_its_slot_as_made_then_unbounded_as_it_runs_with_them():
    setup = chatSetup()
    hookEntered = deferred()
    hookRelease = deferred()
    running = deferred()
    release = deferred()
    large = "x" * (64 * 1024)

    async def slow(*_: Any) -> dict[str, Any]:
        running.resolve()
        await release.promise
        return {}

    addTool(setup.registry, tool("slow", slow))

    async def beforeTool(hookCall: Any, *_: Any) -> Any:
        if hookCall.get("parent") is None:
            return None
        hookEntered.resolve()
        await hookRelease.promise
        return {"arguments": {"text": large}}

    addHooks(setup.registry, ToolTask, {"beforeTool": beforeTool})

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await api.executeTool("slow", {"text": "asked"}, callContext)
        return {}

    addTool(setup.registry, tool("batch", batch))
    setup.faux.setResponses([call("batch"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    stream, starts = await collect(harness, root.id, lambda event: event["type"] == "tool_execution_start")
    submission = await root.submit({"type": "input", "content": "go"}, context)

    async def slot() -> dict[str, Any]:
        return (await harness.snapshot(LiveDoc, root.id, context))["nestedTools"][0]  # type: ignore[no-any-return]

    await hookEntered.promise
    toMatchObject(await slot(), {"status": "pending", "arguments": {"text": "asked"}})
    hookRelease.resolve()
    await running.promise
    toMatchObject(await slot(), {"status": "running", "arguments": {"text": large}})
    release.resolve()
    await submission.wait(context)
    await harness.waitForIdle(context)
    await stream.stop()
    # The start event carries the arguments the call runs with; the task input keeps the ones it was made with.
    start = next(event for event in starts if event["toolCallId"] == "c1/1")
    toMatchObject(start, {"args": {"text": large}})
    nested = next(task for task in await toolTasks(harness) if task["input"]["kind"] == "nested")
    toMatchObject(nested["input"], {"call": {"arguments": {"text": "asked"}}})
    await harness.close(context)


async def test_runs_a_nested_call_whose_repaired_arguments_set_an_optional_property_to_undefined():
    setup = chatSetup()
    seen: list[Any] = []

    async def repaired(args: Any, *_: Any) -> dict[str, Any]:
        seen.append(args)
        return {}

    addTool(setup.registry, tool("repaired", repaired, {"prepareArguments": lambda _args: {"text": UNDEFINED}}))
    holder: dict[str, Any] = {}

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        holder["result"] = await api.executeTool("repaired", {"text": "x"}, callContext)
        return {}

    addTool(setup.registry, tool("batch", batch))
    ran = await runOnce(setup, [call("batch"), DONE])
    toMatchObject(holder["result"], {"isError": False})
    assert seen == [{}]
    await ran.harness.close(context)


async def test_finds_each_nested_calls_slot_after_slots_in_the_middle_were_removed():
    setup = chatSetup()
    addTool(setup.registry, echoTool().registration)

    async def inner(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await api.executeTool("echo", {"text": "m1"}, callContext)
        await api.executeTool("echo", {"text": "m2"}, callContext)
        return {}

    addTool(setup.registry, tool("inner", inner))
    before = deferred()
    checked = deferred()
    slots: list[dict[str, Any]] = []

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await api.executeTool("echo", {"text": "n1"}, callContext)
        # n2 makes m1 and m2 and settles, which removes them from between n2 and n3.
        await api.executeTool("inner", {}, callContext)
        await api.executeTool("echo", {"text": "n3"}, callContext)
        await api.executeTool("echo", {"text": "n4"}, callContext)
        live = await api.snapshot(LiveDoc, api.conversationId, callContext)
        slots.extend(
            {"callId": slot["callId"], "status": slot["status"], "summary": slot.get("summary")}
            for slot in ((live or {}).get("nestedTools") or [])
        )
        before.resolve()
        await checked.promise
        return {}

    addTool(setup.registry, tool("batch", batch))
    setup.faux.setResponses([call("batch"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "input", "content": "go"}, context)
    await before.promise
    done = {"status": "done", "summary": {"isError": False, "durationMs": NUMBER}}
    assert slots == [{"callId": callId, **done} for callId in ["c1/1", "c1/2", "c1/3", "c1/4"]]
    checked.resolve()
    await submission.wait(context)
    await harness.close(context)


async def test_gives_callers_a_result_with_its_task_id_and_only_diagnostics_for_a_nested_call_the_scheduler_faulted():
    setup = chatSetup()

    # Details that are not JSON make the result commit throw, so the task faults and stores no result.
    async def echo(*_: Any) -> dict[str, Any]:
        return {"details": {"count": frozenset([1])}}

    addTool(setup.registry, tool("echo", echo))
    holder: dict[str, Any] = {}

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        holder["result"] = await api.executeTool("echo", {"text": "a"}, callContext)
        return {}

    addTool(setup.registry, tool("batch", batch))
    setup.faux.setResponses([call("batch"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    child = (await toolTasks(harness))[1]
    toMatchObject(child["state"], {"status": "terminal", "outcome": {"status": "faulted"}})
    result = holder["result"]
    assert result == {
        "taskId": child["id"],
        "isError": True,
        "diagnostics": [{"severity": "error", "code": "faulted", "message": result["diagnostics"][0]["message"]}],
    }
    assert "Tool echo failed" in result["diagnostics"][0]["message"]
    await harness.close(context)


async def test_gives_callers_what_a_schema_less_tool_streamed_before_it_threw():
    setup = chatSetup()

    async def fails(_args: Any, api: Any, *_: Any) -> Any:
        api.output("half way\n")
        raise RuntimeError("broke")

    addTool(setup.registry, tool("fails", fails))
    holder: dict[str, Any] = {}

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        holder["result"] = await api.executeTool("fails", {}, callContext)
        return {}

    addTool(setup.registry, tool("batch", batch))
    ran = await runOnce(setup, [call("batch"), DONE])
    toMatchObject(
        holder["result"],
        {"isError": True, "structuredOutput": "half way\n", "diagnostics": [{"code": "tool_error", "message": "broke"}]},
    )
    await ran.harness.close(context)


async def test_shows_a_finished_nested_call_as_a_done_slot_with_its_summary_until_its_caller_settles():
    setup = chatSetup()
    release = deferred()
    checked = deferred()

    # Diagnostics win over output for the error text, which is cut to 500 characters.
    async def verbose(*_: Any) -> dict[str, Any]:
        return {
            "output": [{"type": "text", "text": "ignored"}],
            "isError": True,
            "diagnostics": [{"severity": "error", "message": "e" * 600}],
        }

    addTool(setup.registry, tool("verbose", verbose))
    addTool(setup.registry, echoTool().registration)

    async def paid(*_: Any) -> dict[str, Any]:
        return {"usage": USAGE}

    addTool(setup.registry, tool("paid", paid))

    # An error with mixed content: programs get the list, the summary its text.
    async def broken(*_: Any) -> dict[str, Any]:
        return {
            "output": [
                {"type": "text", "text": "bad image"},
                {"type": "image", "data": "AAAA", "mimeType": "image/png"},
            ],
            "isError": True,
        }

    addTool(setup.registry, tool("broken", broken))

    async def batch(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        await api.executeTool("echo", {"text": "a"}, callContext)
        await api.executeTool("missing", {}, callContext)
        await api.executeTool("paid", {}, callContext)
        await api.executeTool("broken", {}, callContext)
        await api.executeTool("verbose", {}, callContext)
        checked.resolve()
        await release.promise
        return {}

    addTool(setup.registry, tool("batch", batch))
    setup.faux.setResponses([call("batch"), DONE])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    submission = await root.submit({"type": "input", "content": "go"}, context)
    await checked.promise
    slots = (await harness.snapshot(LiveDoc, root.id, context))["nestedTools"]
    assert [{"callId": slot["callId"], "status": slot["status"], "summary": slot.get("summary")} for slot in slots] == [
        {"callId": "c1/1", "status": "done", "summary": {"isError": False, "durationMs": NUMBER}},
        {"callId": "c1/2", "status": "done", "summary": {"isError": True, "error": "Tool missing is not available"}},
        {"callId": "c1/3", "status": "done", "summary": {"isError": False, "durationMs": NUMBER, "usage": USAGE}},
        {"callId": "c1/4", "status": "done", "summary": {"isError": True, "durationMs": NUMBER, "error": "bad image"}},
        {"callId": "c1/5", "status": "done", "summary": {"isError": True, "durationMs": NUMBER, "error": "e" * 500}},
    ]
    release.resolve()
    await submission.wait(context)
    assert await harness.snapshot(LiveDoc, root.id, context) == {}
    await harness.close(context)


def test_migrates_version_1_tool_task_input_to_a_model_issued_call():
    migrate = ToolTask["definition"]["migrate"]
    assert migrate({"assistant": 3, "callId": "c1"}, {"phase": "call"}, 1) == {
        "input": {"kind": "model", "assistant": 3, "callId": "c1"},
        "checkpoint": {"phase": "call"},
    }


# ─── tool task version 1 records ────────────────────────────────────────────


async def storeVersion1(setup: Any, path: str, checkpoint: Any) -> int:
    """Store a live tool task for call ``c1`` of an assistant entry, close, and rewrite its record as version 1 stored
    it: ``{assistant, callId}`` input at version 1, with ``checkpoint``. Returns the task ID."""
    # No submission, so the Harness never starts scheduling and the task stays pending.
    opened = await openChat(await openNodeSqliteStorage(path), setup)
    harness, root = opened.harness, opened.root

    async def create(tx: Any) -> Any:
        message = call("echo", {"text": "old"}).model_dump(mode="json", exclude_none=True)
        entry = await tx.appendEntry(AssistantEntry, root.id, {"model": [message]})
        return await tx.createTask(
            ToolTask, {"kind": "model", "assistant": entry["id"], "callId": "c1"}, {"ownership": {"kind": "conversation"}}
        )

    id = await root.commit(create, context)
    await harness.close(context)
    storage = await openNodeSqliteStorage(path)
    record = await storage.task(id, context)
    input = {key: value for key, value in record["input"].items() if key != "kind"}
    state = {**record["state"], "checkpoint": checkpoint}
    await storage.commit([{"type": "task", "value": {**record, "version": 1, "input": input, "state": state}}], context)
    await storage.close(context)
    return id  # type: ignore[no-any-return]


async def test_version_1_records_migrates_a_pending_version_1_task_and_runs_it_as_a_model_issued_call(sqlitePath: str):
    setup = chatSetup()
    echo = echoTool()
    addTool(setup.registry, echo.registration)
    id = await storeVersion1(setup, sqlitePath, {"phase": "call"})
    opened = await openChat(await openNodeSqliteStorage(sqlitePath), setup)
    harness, root = opened.harness, opened.root
    assert (await harness.getTask(id, context))["version"] == 1
    harness.resume()
    settled = await harness.waitForTask(id, context)
    assert settled["version"] == 2
    toMatchObject(settled["input"], {"kind": "model", "callId": "c1"})
    toMatchObject(settled["state"]["outcome"], {"status": "completed", "result": {"kind": "model"}})
    assert echo.state.runs == 1
    [result] = results(await allEntries(root))
    assert [result["toolCallId"], text(result)] == ["c1", "echo old"]
    await harness.close(context)


async def test_version_1_records_migrates_a_version_1_task_interrupted_after_intent_and_settles_it_interrupted(
    sqlitePath: str,
):
    setup = chatSetup()
    echo = echoTool()
    addTool(setup.registry, echo.registration)
    intent = {"phase": "execute", "arguments": {"text": "old"}, "replay": "unsafe"}
    id = await storeVersion1(setup, sqlitePath, intent)
    opened = await openChat(await openNodeSqliteStorage(sqlitePath), setup)
    harness, root = opened.harness, opened.root
    harness.resume()
    settled = await harness.waitForTask(id, context)
    assert settled["version"] == 2
    toMatchObject(settled["state"]["outcome"], {"status": "failed", "result": {"kind": "model"}})
    assert echo.state.runs == 0
    [result] = results(await allEntries(root))
    toMatchObject(result, {"toolCallId": "c1", "isError": True})
    assert "Tool echo was interrupted" in text(result)
    await harness.close(context)

