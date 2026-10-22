"""pi durable's ``test/harness-live-deltas.test.ts``."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from misaka.ai.providers.faux import fauxAssistantMessage, fauxText, fauxToolCall
from misaka.durable import LiveDoc, MemoryStorage, defineTool
from tests.chat_support import chatSetup, openChat, waitFor
from tests.durable_support import AnyOf, ArrayContaining, context, documentChanges
from tests.harness_support import addTool
from tests.task_support import aborted, race

EMPTY_OBJECT = {"type": "object", "properties": {}}
OUTPUT = ["tools", 0, "output"]


def collectLiveCommits(harness: Any) -> list[list[Any]]:
    commits: list[list[Any]] = []

    def onCommit(publication: Any, _context: Any) -> None:
        for change in documentChanges(publication):
            if change["record"]["kind"] == "pi.live" and len(change["ops"]) > 0:
                commits.append(list(change["ops"]))

    harness.subscribeCommits(onCommit)
    return commits


def live(harness: Any) -> Any:
    return harness.snapshot(LiveDoc, 1, context)


def isOutput(op: list[Any]) -> bool:
    return op[1] == OUTPUT


async def drive(outputLimits: dict[str, Any] | None = None) -> SimpleNamespace:
    """Drive one tool call step by step and capture the exact Chord operations of every ``pi.live`` commit. Each
    ``step()`` runs one action inside the tool and returns the operations of the commit it caused."""
    setup = chatSetup()
    actions: list[Callable[[Any], Any]] = []
    holder: dict[str, Callable[[], None] | None] = {"wake": None}

    async def execute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        while True:
            while len(actions) == 0:
                waiting: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                holder["wake"] = lambda waiting=waiting: waiting.done() or waiting.set_result(None)  # type: ignore[misc]
                await race(waiting, aborted(callContext.abortSignal))
            if not await actions.pop(0)(api):
                return {}

    addTool(
        setup.registry,
        defineTool(
            {
                "name": "drive",
                "description": "Driven by the test",
                "parameters": EMPTY_OBJECT,
                "outputLimits": outputLimits if outputLimits is not None else {},
                "execute": execute,
            }
        ),
    )
    setup.faux.setResponses(
        [
            fauxAssistantMessage([fauxToolCall("drive", {}, {"id": "c1"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage([fauxText("done")]),
        ]
    )
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    commits = collectLiveCommits(harness)
    submission = await root.submit({"type": "input", "content": "go"}, context)

    async def running() -> bool:
        snapshot = await live(harness)
        tools = None if snapshot is None else snapshot.get("tools")
        return tools is not None and len(tools) > 0 and tools[0]["status"] == "running"  # type: ignore[no-any-return]

    await waitFor(running)

    def push(action: Callable[[Any], Any]) -> None:
        actions.append(action)
        wake = holder["wake"]
        if wake is not None:
            wake()

    async def step(action: Callable[[Any], Any]) -> list[Any]:
        """Operations of every ``pi.live`` commit so far."""
        before = len(commits)

        async def run(api: Any) -> bool:
            result = action(api)
            if inspect.isawaitable(result):
                await result
            return True

        push(run)
        await waitFor(lambda: len(commits) > before)
        return commits[-1]

    async def finish() -> list[list[Any]]:
        """Let the tool return and the run finish; returns the commits made meanwhile."""
        before = len(commits)

        async def stop(api: Any) -> bool:
            return False

        push(stop)
        await submission.wait(context)
        return commits[before:]

    return SimpleNamespace(harness=harness, commits=commits, step=step, finish=finish)


async def test_hands_a_generation_over_to_its_tool_round_and_starts_a_tool_with_one_field_write_each():
    setup = chatSetup()

    async def noop(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": []}

    addTool(
        setup.registry,
        defineTool({"name": "noop", "description": "noop", "parameters": EMPTY_OBJECT, "execute": noop}),
    )
    setup.faux.setResponses(
        [
            fauxAssistantMessage([fauxToolCall("noop", {}, {"id": "c1"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage([fauxText("done")]),
        ]
    )
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    commits = collectLiveCommits(harness)
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    tasks = await harness.commit(lambda tx: tx.scanTasks({"conversationId": root.id}, 20), context)

    def id(kind: str) -> list[int]:
        return [task["id"] for task in tasks["items"] if task["kind"] == kind]

    firstGeneration, secondGeneration = sorted(id("pi.generation"))
    [tool] = id("pi.tool")
    entries = await root.entries({}, 10, None, context)
    result = next(entry for entry in entries["items"] if entry["kind"] == "pi.tool-result")["id"]
    assert commits == [
        # submission
        [["s", ["run"], {"taskId": firstGeneration, "inputs": [AnyOf(int)]}]],
        # request
        [["s", ["generation"], {"attempt": 1}]],
        # the generation starts its tool round and keeps the run
        ArrayContaining(
            [
                ["d", ["generation"]],
                ["s", ["tools"], [{"callId": "c1", "name": "noop", "taskId": tool, "status": "pending"}]],
            ]
        ),
        # intent
        [["s", ["tools", 0, "status"], "running"]],
        # result
        ArrayContaining([["s", ["tools", 0, "status"], "done"], ["s", ["tools", 0, "entry"], result]]),
        # the generation's tools phase hands the run to the next generation
        ArrayContaining([["d", ["tools"]], ["s", ["run", "taskId"], secondGeneration]]),
        [["s", ["generation"], {"attempt": 1}]],
        # the answer ends the run
        ArrayContaining([["d", ["run"]], ["d", ["generation"]]]),
    ]
    assert len(commits[2]) == 2
    assert len(commits[4]) == 2
    assert len(commits[5]) == 2
    await harness.close(context)


async def test_appends_head_output_and_then_only_updates_the_dropped_counts_once_the_window_is_full():
    run = await drive({"maxLines": 2})
    assert await run.step(lambda api: api.output("one\n")) == [["s", OUTPUT, "one\n"]]
    assert await run.step(lambda api: api.output("two\n")) == [["a", OUTPUT, "two\n"]]
    # The window is full: the retained text stays; only the counts change.
    assert await run.step(lambda api: api.output("three\n")) == [
        ["s", ["tools", 0, "droppedBytes"], 6],
        ["s", ["tools", 0, "droppedLines"], 1],
    ]
    assert await run.step(lambda api: api.output("four\n")) == [
        ["s", ["tools", 0, "droppedBytes"], 11],
        ["s", ["tools", 0, "droppedLines"], 2],
    ]
    await run.finish()
    await run.harness.close(context)


async def test_slides_a_tail_window_as_a_front_trim_plus_an_append():
    run = await drive({"maxLines": 3, "retain": "tail"})
    assert await run.step(lambda api: api.output("line 1\nline 2\nline 3\n")) == [["s", OUTPUT, "line 1\nline 2\nline 3\n"]]
    assert await run.step(lambda api: api.output("line 4\n")) == [
        ["t", OUTPUT, 7],
        ["a", OUTPUT, "line 4\n"],
        ["s", ["tools", 0, "droppedBytes"], 7],
        ["s", ["tools", 0, "droppedLines"], 1],
    ]
    # The buffer keeps only the window, and later slides stay minimal and exact.
    assert await run.step(lambda api: api.output("line 5\nline 6\n")) == [
        ["t", OUTPUT, 14],
        ["a", OUTPUT, "line 5\nline 6\n"],
        ["s", ["tools", 0, "droppedBytes"], 21],
        ["s", ["tools", 0, "droppedLines"], 3],
    ]
    assert (await live(run.harness))["tools"][0]["output"] == "line 4\nline 5\nline 6\n"
    await run.finish()
    await run.harness.close(context)


async def test_writes_the_whole_window_when_chords_overlap_search_cannot_find_the_shared_part():
    # A retained window beyond the 64 KiB overlap scan.
    wide = await drive({"maxBytes": 100 * 1024, "maxLines": 1_000_000, "retain": "tail"})

    def line(index: int) -> str:
        return f"{str(index).rjust(10, '0')} {'x' * 989}\n"

    text = "".join(line(index) for index in range(100))
    await wide.step(lambda api: api.output(text))
    slid = await wide.step(lambda api: api.output(line(100) + line(101) + line(102) + line(103)))
    assert [op[0] for op in slid if isOutput(op)] == ["s"]
    await wide.finish()
    await wide.harness.close(context)

    # Repetitive output still finds an overlap here; Chord's bounded candidate search can give up on other inputs and
    # then writes one window.
    repetitive = await drive({"maxLines": 50, "retain": "tail"})
    await repetitive.step(lambda api: api.output("y\n" * 50))
    repeated = await repetitive.step(lambda api: api.output("z\n"))
    assert [op for op in repeated if isOutput(op)] == [["t", OUTPUT, 2], ["a", OUTPUT, "z\n"]]
    await repetitive.finish()
    await repetitive.harness.close(context)


async def test_diffs_details_leaf_by_leaf_and_appends_diagnostics():
    run = await drive()
    details = ["tools", 0, "details"]
    assert await run.step(lambda api: api.details({"step": 1, "log": "a"}, context)) == [
        ["s", details, {"step": 1, "log": "a"}]
    ]
    assert await run.step(lambda api: api.details({"step": 2, "log": "ab"}, context)) == ArrayContaining(
        [["s", [*details, "step"], 2], ["a", [*details, "log"], "b"]]
    )
    assert await run.step(lambda api: api.details({"step": 2}, context)) == [["d", [*details, "log"]]]
    diagnostics = ["tools", 0, "diagnostics"]
    first = {"severity": "info", "message": "first"}
    second = {"severity": "warn", "message": "second"}
    assert await run.step(lambda api: api.diagnostic(first)) == [["s", diagnostics, [first]]]
    assert await run.step(lambda api: api.diagnostic(second)) == [["p", diagnostics, 1, 0, [second]]]
    # Settlement moves everything into the result entry and keeps the slot small.
    [settled, *_rest] = await run.finish()
    assert settled == ArrayContaining([["s", ["tools", 0, "status"], "done"], ["d", details], ["d", diagnostics]])
    await run.harness.close(context)


async def test_streams_partial_text_as_appends():
    setup = chatSetup({"tokensPerSecond": 400, "tokenSize": {"min": 4, "max": 4}})
    setup.faux.setResponses([fauxAssistantMessage([fauxText("word " * 250)])])
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    commits = collectLiveCommits(harness)
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    partials = commits[2:-1]
    assert len(partials) > 2
    assert partials[0] == [["s", ["generation", "message"], AnyOf(dict)]]
    for ops in partials[1:]:
        assert ops == [["a", ["generation", "message", "content", 0, "text"], AnyOf(str)]]
    await harness.close(context)


async def test_stores_a_complete_base_exactly_in_the_commits_where_nothing_runs():
    # Storage that remembers whether each commit wrote pi.live as a base or a delta.
    written: dict[int, str] = {}
    liveId: int | None = None

    class RecordingStorage(MemoryStorage):
        async def commit(self, writes: Any, context: Any) -> int:  # type: ignore[override]
            kind: str | None = None
            for write in writes:
                if write["type"] == "document.change" and write["id"] == liveId:
                    kind = write["content"]["kind"]
            seq = await super().commit(writes, context)
            if kind is not None:
                written[seq] = kind
            return seq

    setup = chatSetup()

    def runningTool(name: str) -> Any:
        async def execute(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
            api.output(f"{name} output\n")
            await asyncio.sleep(0.15)
            api.output(f"{name} more\n")
            return {}

        return execute

    for name in ("first", "second"):
        addTool(
            setup.registry,
            defineTool({"name": name, "description": name, "parameters": EMPTY_OBJECT, "execute": runningTool(name)}),
        )
    setup.faux.setResponses(
        [
            fauxAssistantMessage([fauxToolCall("first", {}, {"id": "a"}), fauxToolCall("second", {}, {"id": "b"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage([fauxText("done")]),
        ]
    )
    setup.settings["toolExecution"] = "sequential"
    opened = await openChat(RecordingStorage(), setup)
    harness, root = opened.harness, opened.root
    values: dict[int, Any] = {}

    def onCommit(publication: Any, _context: Any) -> None:
        nonlocal liveId
        for change in documentChanges(publication):
            if change["record"]["kind"] != "pi.live":
                continue
            liveId = change["record"]["id"]
            if change["value"] is not None:
                values[publication["seq"]] = change["value"]

    harness.subscribeCommits(onCommit)
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)

    def nothingRuns(value: dict[str, Any]) -> bool:
        return value.get("generation") is None and not any(slot["status"] == "running" for slot in value.get("tools") or [])

    kinds = []
    for seq, kind in written.items():
        assert (kind == "base") == nothingRuns(values[seq])
        kinds.append(kind)
    # Bases at the handover, after each sequential tool, after the round, and when the run ends.
    assert len([kind for kind in kinds if kind == "base"]) >= 5
    assert "delta" in kinds
    await harness.close(context)


async def test_starts_calls_the_request_did_not_offer_as_done_and_marks_a_faulted_tools_slot_done_without_an_entry():
    setup = chatSetup()

    # Not strict JSON: the result commit throws and the scheduler faults the task.
    async def bad(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": [], "details": {"fn": lambda: 1}}

    addTool(setup.registry, defineTool({"name": "bad", "description": "bad", "parameters": EMPTY_OBJECT, "execute": bad}))
    setup.faux.setResponses(
        [
            fauxAssistantMessage([fauxToolCall("ghost", {}, {"id": "g"}), fauxToolCall("bad", {}, {"id": "b"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage([fauxText("done")]),
        ]
    )
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    commits = collectLiveCommits(harness)
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    entries = await root.entries({}, 10, None, context)
    ghostResult = next(entry for entry in entries["items"] if entry["kind"] == "pi.tool-result")["id"]
    handover = next(ops for ops in commits if any(op[0] == "s" and op[1][0] == "tools" for op in ops))
    assert [
        "s",
        ["tools"],
        [
            {"callId": "g", "name": "ghost", "status": "done", "entry": ghostResult},
            {"callId": "b", "name": "bad", "taskId": AnyOf(int), "status": "pending"},
        ],
    ] in handover
    # The fault cleanup writes only the status.
    assert [["s", ["tools", 1, "status"], "done"]] in commits
    await harness.close(context)


async def test_commits_the_tool_calling_answer_its_tool_tasks_the_generations_wait_and_the_tool_round_in_one_commit():
    setup = chatSetup()

    async def noop(_args: Any, api: Any, callContext: Any) -> dict[str, Any]:
        return {"output": []}

    addTool(
        setup.registry,
        defineTool({"name": "noop", "description": "noop", "parameters": EMPTY_OBJECT, "execute": noop}),
    )
    setup.faux.setResponses(
        [
            fauxAssistantMessage([fauxToolCall("noop", {}, {"id": "a"}), fauxToolCall("noop", {}, {"id": "b"})], {"stopReason": "toolUse"}),
            fauxAssistantMessage([fauxText("done")]),
        ]
    )
    opened = await openChat(MemoryStorage(), setup)
    harness, root = opened.harness, opened.root
    handover: Any = None

    def onCommit(publication: Any, _context: Any) -> None:
        nonlocal handover
        for change in documentChanges(publication):
            value = change["value"]
            if change["record"]["kind"] == "pi.live" and value is not None and len(value.get("tools") or []) == 2:
                handover = publication if handover is None else handover

    harness.subscribeCommits(onCommit)
    await (await root.submit({"type": "input", "content": "go"}, context)).wait(context)
    kinds = [change["value"]["kind"] for change in handover["changes"] if change["type"] in ("entry", "task")]
    assert sorted(kinds) == ["pi.assistant", "pi.generation", "pi.tool", "pi.tool"]
    await harness.close(context)


async def test_writes_an_aborted_tools_slot_with_field_level_ops():
    run = await drive()
    await run.step(lambda api: api.output("partial\n"))
    await run.step(lambda api: api.details({"n": 1}, context))
    taskId = (await live(run.harness))["tools"][0]["taskId"]
    before = len(run.commits)
    await run.harness.abortTask(taskId, context)

    def isAbortCommit(ops: list[Any]) -> bool:
        return any(op[0] == "s" and op[1] == ["tools", 0, "status"] for op in ops)

    await waitFor(lambda: any(isAbortCommit(ops) for ops in run.commits[before:]))
    abortCommit = next(ops for ops in run.commits[before:] if isAbortCommit(ops))
    assert abortCommit == ArrayContaining(
        [
            ["s", ["tools", 0, "status"], "done"],
            ["s", ["tools", 0, "entry"], AnyOf(int)],
            ["d", OUTPUT],
            ["d", ["tools", 0, "details"]],
        ]
    )
    assert len(abortCommit) == 4
    await run.harness.close(context)


def test_keeps_a_complete_base_exactly_while_nothing_runs():
    def base(value: dict[str, Any]) -> bool:
        return LiveDoc["definition"]["checkpointWhen"](value, [], {"deltasSinceBase": 1000})  # type: ignore[no-any-return]

    def slot(status: str) -> dict[str, Any]:
        return {"callId": "c", "name": "n", "status": status}

    run = {"taskId": 1, "inputs": []}
    assert base({}) is True
    assert base({"run": run, "generation": {"attempt": 1}}) is False
    assert base({"run": run, "tools": [slot("pending"), slot("done")]}) is True
    assert base({"run": run, "tools": [slot("done"), slot("running")]}) is False
    assert base({"run": run}) is True
