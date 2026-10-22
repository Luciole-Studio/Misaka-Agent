"""pi durable's ``test/codemode.test.ts``.

Adaptations: the sandbox is ``misaka.codemode.CodemodeSandbox``, whose ``start`` the chat wraps on the instance as
upstream reassigns ``sandbox.start``; a ``void promise.then(...)`` is a task the test holds. Temp directories are
removed after each test, as upstream's ``afterEach`` does. Code mode's own temp directories are named
``misaka-codemode-`` (a deliberate MISAKA fork of ``pi-codemode-``); no case here asserts the prefix. "journals results
that finish together in one commit" gives each storage commit 5 ms of latency, so its count of journal commits holds
under upstream's premise that the VM acknowledges a group of results within one commit (see the case).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage, fauxText, fauxToolCall
from misaka.codemode import CodemodeSandbox
from misaka.durable import ToolResultEntry, ToolTask, defineTool
from misaka.durable.codemode.codemode import (
    CodemodeJournalDoc,
    CodemodeRunDoc,
    createCodemodeTool,
)
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.storage.sqlite.node import openNodeSqliteStorage
from misaka.utils.js import formatNumber
from tests.chat_support import allEntries, chatSetup, openChat
from tests.durable_support import ControlledStorage, context, toMatchObject
from tests.harness_support import addHooks, addTool
from tests.task_support import aborted, deferred, eventually

Parameters = {"type": "object", "properties": {"text": {"type": "string"}, "id": {"type": "number"}}}

HEADER = re.compile(r"^Script (completed|failed)\nWall time [\d.]+ seconds\nOutput:\n")


def tool(name: str, execute: Callable[..., Awaitable[dict[str, Any]]], replay: str = "unsafe") -> dict[str, Any]:
    return defineTool({"name": name, "description": name, "parameters": Parameters, "execute": execute, "replay": replay})


DONE = fauxAssistantMessage([fauxText("done")])


def script(code: str) -> Any:
    return fauxAssistantMessage([fauxToolCall("codemode", {"code": code}, {"id": "s1"})], {"stopReason": "toolUse"})


def js(value: Any) -> str:
    """``${value ?? ""}``."""
    return "" if value is None else formatNumber(value) if isinstance(value, int | float) else str(value)


directories: set[str] = set()


@pytest.fixture(autouse=True)
def removeDirectories() -> Iterator[None]:
    yield
    for directory in directories:
        shutil.rmtree(directory, ignore_errors=True)
    directories.clear()


def sqlitePath() -> str:
    directory = tempfile.mkdtemp(prefix="pi-durable-codemode-")
    directories.add(directory)
    return os.path.join(directory, "session.sqlite")


def tempDirectory(prefix: str) -> str:
    directory = tempfile.mkdtemp(prefix=prefix)
    directories.add(directory)
    return directory


def codemodeChat(globals: list[dict[str, Any]] | None = None, namespaces: list[dict[str, Any]] | None = None) -> Any:
    """A chat with the code mode tool and test tools:

    - ``echo`` returns ``echo <text>`` and counts its runs by text;
    - ``hold`` (replay-safe) waits for ``holds.gate`` while ``holds.block`` is set, then returns ``held <id>``;
    - ``slow`` returns ``slow <id>`` once ``slows.release(id)`` was called.
    """
    setup = chatSetup()
    sandbox = CodemodeSandbox({"memoryLimitBytes": 64 * 1024 * 1024})
    vm = SimpleNamespace(starts=0, onStart=None)
    start = sandbox.start

    def counted(code: str, options: Any) -> Any:
        vm.starts += 1
        run = start(code, options)
        if vm.onStart is not None:
            vm.onStart(run)
        return run

    sandbox.start = counted  # type: ignore[method-assign]
    addTool(setup.registry, createCodemodeTool({"sandbox": sandbox, "globals": globals or [], "namespaces": namespaces or []}))
    echo: dict[str, int] = {}

    async def runEcho(args: Any, *_: Any) -> dict[str, Any]:
        text = js(args.get("text"))
        echo[text] = echo.get(text, 0) + 1
        return {"output": [{"type": "text", "text": f"echo {text}"}]}

    addTool(setup.registry, tool("echo", runEcho))
    holds = SimpleNamespace(block=True, reached=deferred(), runs=0)

    async def runHold(args: Any, _api: Any, callContext: Any) -> dict[str, Any]:
        holds.runs += 1
        if holds.block:
            holds.reached.resolve()
            await aborted(callContext.abortSignal)
        return {"output": [{"type": "text", "text": f"held {js(args.get('id'))}"}]}

    addTool(setup.registry, tool("hold", runHold, "safe"))
    pauses = SimpleNamespace(block=True, reached=deferred())

    async def runPause(_args: Any, _api: Any, callContext: Any) -> dict[str, Any]:
        if pauses.block:
            pauses.reached.resolve()
            await aborted(callContext.abortSignal)
        return {"output": [{"type": "text", "text": "paused"}]}

    addTool(setup.registry, tool("pause", runPause, "safe"))
    gates: dict[Any, Any] = {}
    finished: list[Any] = []

    def gate(id: Any) -> Any:
        found = gates.get(id) or deferred()
        gates[id] = found
        return found

    slows = SimpleNamespace(finished=finished, gate=gate, release=lambda id: gate(id).resolve())

    async def runSlow(args: Any, *_: Any) -> dict[str, Any]:
        id = args.get("id") if args.get("id") is not None else 0
        await gate(id).promise
        finished.append(id)
        return {"output": [{"type": "text", "text": f"slow {js(args.get('id'))}"}]}

    addTool(setup.registry, tool("slow", runSlow))
    return SimpleNamespace(setup=setup, sandbox=sandbox, vm=vm, echo=echo, holds=holds, pauses=pauses, slows=slows)


async def toolTasks(harness: Any) -> list[dict[str, Any]]:
    page = await harness.commit(lambda tx: tx.scanTasks({"kind": ToolTask["definition"]["name"]}, 100), context)
    return page["items"]  # type: ignore[no-any-return]


async def scriptTask(harness: Any) -> int:
    return next(task for task in await toolTasks(harness) if task["input"]["kind"] == "model")["id"]  # type: ignore[no-any-return]


async def nestedCount(harness: Any) -> int:
    return len([task for task in await toolTasks(harness) if task["input"]["kind"] == "nested"])


async def resultText(root: Any) -> dict[str, Any]:
    """The code mode call's result text."""
    entry = next(candidate for candidate in await allEntries(root) if ToolResultEntry.is_(candidate))
    message = entry["model"][0]
    return {
        # Without the "Script completed" header and its wall time, which tests of it check themselves.
        "text": HEADER.sub("", "".join(item["text"] if item["type"] == "text" else "" for item in message["content"]))
        if message["role"] == "toolResult"
        else "",
        "isError": message["role"] == "toolResult" and message["isError"],
    }


async def noop(*_: Any) -> None:
    return None


async def restart(
    chat: Any,
    code: str,
    between: Callable[[Any, int], Awaitable[Any]] = noop,
    beforeClose: Callable[[Any, int], Awaitable[Any]] = noop,
) -> SimpleNamespace:
    """Run ``code`` until ``holds.reached``, close the Harness (a restart), let ``between`` change the stored state, then
    reopen with ``hold`` no longer blocking and wait for the answer."""
    path = sqlitePath()
    chat.setup.faux.setResponses([script(code), DONE])
    opened = await openChat(await openNodeSqliteStorage(path), chat.setup)
    id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
    await chat.holds.reached.promise
    await beforeClose(opened.harness, await scriptTask(opened.harness))
    await opened.harness.close(context)
    chat.holds.block = False
    opened = await openChat(await openNodeSqliteStorage(path), chat.setup)
    await between(opened.harness, await scriptTask(opened.harness))
    opened.harness.resume()
    assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
    return SimpleNamespace(harness=opened.harness, root=opened.root, result=await resultText(opened.root))


def editJournal(change: Callable[[dict[str, Any]], None]) -> Callable[[Any, int], Awaitable[None]]:
    """``between``: change the journal of the script's task."""

    async def between(opened: Any, task: int) -> None:
        async def edit(tx: Any) -> None:
            change(await tx.doc(CodemodeJournalDoc, task))

        await opened.commit(edit, context)

    return between


def setStore(store: dict[str, Any], clearJournal: bool = False) -> Callable[[Any, int], Awaitable[None]]:
    """``between``: start the rerun with ``store``, and optionally as if the restart came before the first journal
    commit."""

    async def between(opened: Any, task: int) -> None:
        async def edit(tx: Any) -> None:
            run = await tx.doc(CodemodeRunDoc, task)
            run["start"]["store"] = store
            if clearJournal:
                (await tx.doc(CodemodeJournalDoc, task))["entries"].length = 0

        await opened.commit(edit, context)

    return between


def journalReaches(check: Callable[[dict[str, Any]], bool]) -> Callable[[Any, int], Awaitable[None]]:
    """``beforeClose``: wait until the journal's snapshot passes ``check``."""

    async def beforeClose(opened: Any, task: int) -> None:
        async def reached() -> bool:
            journal = await opened.snapshot(CodemodeJournalDoc, task, context)
            return journal is not None and check(journal)

        await eventually(reached)

    return beforeClose


def firstIssuedAfter(journal: dict[str, Any]) -> Any:
    entries = journal["entries"]
    return entries[0].get("issuedAfter") if len(entries) > 0 else None


class TestDurableCodeMode:
    async def test_runs_a_script_that_calls_tools_as_nested_calls_and_journals_each_result_before_delivering_it(
        self,
    ) -> None:
        chat = codemodeChat()
        chat.holds.block = False
        chat.setup.faux.setResponses(
            [
                script('const a = await tools.echo({ text: "a" }); const b = await tools.echo({ text: "b" }); return [a, b];'),
                DONE,
            ]
        )
        opened = await openChat(await openNodeSqliteStorage(sqlitePath()), chat.setup)
        await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
        assert await resultText(opened.root) == {"text": '["echo a","echo b"]', "isError": False}
        assert await nestedCount(opened.harness) == 2
        await opened.harness.close(context)

    async def test_resumes_a_script_after_a_restart_without_running_its_finished_calls_again(self) -> None:
        chat = codemodeChat()
        done = await restart(
            chat,
            """const a = await tools.echo({ text: "a" });
            const held = await tools.hold({ id: 1 });
            const b = await tools.echo({ text: "b" });
            return [a, held, b, Date.now() > 0];""",
        )
        assert done.result == {"text": '["echo a","held 1","echo b",true]', "isError": False}
        assert chat.echo == {"a": 1, "b": 1}
        # The VM ran twice; hold, replay-safe, reran once after the restart.
        assert chat.vm.starts == 2
        assert chat.holds.runs == 2
        await done.harness.close(context)

    async def test_replays_results_in_the_order_they_arrived_not_the_order_the_calls_were_made(self) -> None:
        chat = codemodeChat()
        code = """const order = [];
            await Promise.all([tools.slow({ id: 1 }).then(() => order.push(1)), tools.slow({ id: 2 }).then(() => order.push(2))]);
            await tools.echo({ text: order.join(",") });
            await tools.hold({});
            return order;"""
        # 2 finishes first, then 1.
        chat.slows.release(2)

        async def releaseOne() -> None:
            await eventually(lambda: 2 in chat.slows.finished)
            chat.slows.release(1)

        releasing = asyncio.ensure_future(releaseOne())
        done = await restart(chat, code)
        await releasing
        assert done.result == {"text": "[2,1]", "isError": False}
        assert chat.echo == {"2,1": 1}
        await done.harness.close(context)

    async def test_journals_globals_with_their_results_and_hands_a_replay_the_recorded_ones(self) -> None:
        counter = 0

        async def next_(*_: Any) -> int:
            nonlocal counter
            counter += 1
            return counter

        chat = codemodeChat([{"name": "next", "execute": next_}])
        done = await restart(chat, "const n = await next(); await tools.hold({}); return [n, await next()];")
        # The first next() ran once and was replayed from the journal; the second ran after the restart.
        assert done.result == {"text": "[1,2]", "isError": False}
        assert counter == 2
        await done.harness.close(context)

    async def test_keeps_the_scripts_clock_and_randomness_across_a_restart(self) -> None:
        chat = codemodeChat()
        done = await restart(
            chat,
            """const before = [Date.now(), Math.random()];
            await tools.hold({});
            await tools.echo({ text: String(before[0]) + "/" + String(before[1]) });
            return "ok";""",
        )
        assert done.result["text"] == "ok"
        # One echo: the same arguments, so the rerun admitted no second call.
        assert list(chat.echo.values()) == [1]
        await done.harness.close(context)

    async def test_ends_the_call_when_a_resumed_script_made_a_different_number_of_calls_than_before(self) -> None:
        chat = codemodeChat()

        def change(journal: dict[str, Any]) -> None:
            journal["entries"][0]["issuedAfter"] = 7

        done = await restart(chat, 'await tools.echo({ text: "a" }); await tools.hold({});', editJournal(change))
        assert done.result["isError"] is True
        assert "after result 1 it had made 2 calls, where it had made 7" in done.result["text"]
        await done.harness.close(context)

    async def test_ends_the_call_at_once_when_a_resumed_script_waits_without_making_a_call_it_made_before(self) -> None:
        chat = codemodeChat()

        def change(journal: dict[str, Any]) -> None:
            journal["entries"][0]["id"] = 99

        done = await restart(chat, 'await tools.echo({ text: "a" }); await tools.hold({});', editJournal(change))
        assert done.result["isError"] is True
        assert "it waits without making call 99" in done.result["text"]
        # hold, admitted before the restart, resumed on its own; no call was admitted by the replay.
        assert await nestedCount(done.harness) == 2
        await done.harness.close(context)

    async def test_ends_the_call_when_a_resumed_script_calls_a_global_with_other_arguments(self) -> None:
        async def one(*_: Any) -> int:
            return 1

        chat = codemodeChat([{"name": "next", "execute": one}])

        def change(journal: dict[str, Any]) -> None:
            journal["entries"][0]["global"]["args"] = "other"

        done = await restart(chat, "await next(); await tools.hold({});", editJournal(change))
        assert done.result["isError"] is True
        assert "call 1 is next" in done.result["text"]
        await done.harness.close(context)

    async def test_admits_no_call_of_a_diverging_replay_even_one_the_script_never_made_before(self) -> None:
        chat = codemodeChat()
        # With k set, the replay makes three extra calls after result 1, which the live run did not make; the count in
        # result 1's acknowledgment shows the divergence only after they were issued.
        done = await restart(
            chat,
            """await tools.echo({ text: "a" });
            const extra = load("k") ?? 0;
            for (let i = 0; i < extra; i++) tools.echo({ text: "extra" + i });
            await tools.hold({});""",
            setStore({"k": 3}),
            journalReaches(lambda journal: firstIssuedAfter(journal) == 2),
        )
        assert done.result["isError"] is True
        assert "after result 1 it had made 5 calls, where it had made 2" in done.result["text"]
        assert chat.echo == {"a": 1}
        assert await nestedCount(done.harness) == 2
        await done.harness.close(context)

    async def test_ends_the_call_when_a_resumed_script_ends_where_it_went_on_before(self) -> None:
        chat = codemodeChat()
        done = await restart(
            chat,
            'await tools.echo({ text: "a" }); if (load("k")) return "early"; await tools.hold({});',
            setStore({"k": True}),
            # The live run acknowledged result 1 before the restart: it went on after it.
            journalReaches(lambda journal: firstIssuedAfter(journal) == 2),
        )
        assert done.result["isError"] is True
        assert "it ended after result 1, where it went on before" in done.result["text"]
        await done.harness.close(context)

    async def test_ends_the_call_when_a_resumed_script_ends_before_calls_the_live_run_made_even_without_a_recorded_count(
        self,
    ) -> None:
        chat = codemodeChat()

        async def between(opened: Any, task: int) -> None:
            async def edit(tx: Any) -> None:
                run = await tx.doc(CodemodeRunDoc, task)
                run["start"]["store"] = {"k": True}
                # As if the live run crashed before recording result 1's count.
                journal = await tx.doc(CodemodeJournalDoc, task)
                del journal["entries"][0]["issuedAfter"]

            await opened.commit(edit, context)

        done = await restart(
            chat,
            'await tools.echo({ text: "a" }); if (load("k")) return "early"; await tools.echo({ text: "b" }); await tools.hold({});',
            between,
        )
        assert done.result["isError"] is True
        assert "it ended after result 1, where it went on before" in done.result["text"]
        await done.harness.close(context)

    async def test_ends_the_call_when_a_resumed_script_makes_a_call_an_earlier_attempt_admitted_with_other_arguments(
        self,
    ) -> None:
        chat = codemodeChat()
        done = await restart(
            chat,
            'await tools.echo({ text: load("k") ?? "a" }); await tools.hold({});',
            # As if the restart came before the first journal commit: the rerun runs live from the start.
            setStore({"k": "other"}, clearJournal=True),
        )
        assert done.result["isError"] is True
        assert "different path" in done.result["text"]
        assert "call 1 is tools.echo" in done.result["text"]
        assert chat.echo == {"a": 1}
        await done.harness.close(context)

    async def test_records_a_count_the_live_run_never_recorded_as_soon_as_a_replay_acknowledges_it(self) -> None:
        chat = codemodeChat()
        path = sqlitePath()
        chat.setup.faux.setResponses([script('await tools.echo({ text: "a" }); await tools.hold({});'), DONE])
        opened = await openChat(await openNodeSqliteStorage(path), chat.setup)
        id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
        await chat.holds.reached.promise
        await opened.harness.close(context)
        # As if result 1's count was never recorded; hold blocks again after the restart.
        chat.holds.reached = deferred()
        opened = await openChat(await openNodeSqliteStorage(path), chat.setup)
        task = await scriptTask(opened.harness)

        async def forget(tx: Any) -> None:
            del (await tx.doc(CodemodeJournalDoc, task))["entries"][0]["issuedAfter"]

        await opened.harness.commit(forget, context)
        opened.harness.resume()
        # The replay acknowledges result 1 and goes live; the script waits on hold, and the count is already recorded.
        await chat.holds.reached.promise
        await journalReaches(lambda journal: firstIssuedAfter(journal) == 2)(opened.harness, task)
        await opened.harness.close(context)
        chat.holds.block = False
        opened = await openChat(await openNodeSqliteStorage(path), chat.setup)
        opened.harness.resume()
        assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
        await opened.harness.close(context)

    async def test_lists_the_scripts_tool_calls_in_its_results_details(self) -> None:
        chat = codemodeChat()

        async def fails(*_: Any) -> dict[str, Any]:
            return {"output": [{"type": "text", "text": "permission denied"}], "isError": True}

        addTool(chat.setup.registry, tool("fails", fails))
        chat.setup.faux.setResponses(
            [
                script('await tools.echo({ text: "a" }); try { await tools.fails({}); } catch {} tools.hold({ id: 9 }); return 1;'),
                DONE,
            ]
        )
        opened = await openChat(await openNodeSqliteStorage(sqlitePath()), chat.setup)
        await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
        entry = next(candidate for candidate in await allEntries(opened.root) if ToolResultEntry.is_(candidate))
        details = entry["model"][0].get("details")
        # A call still running, or not yet launched, when the script ended shows as cancelled.
        toMatchObject(
            details["calls"],
            [
                {"id": 1, "name": "echo", "args": '{"text":"a"}', "status": "ok"},
                {"id": 2, "name": "fails", "args": "{}", "status": "error", "error": "permission denied"},
                {"id": 3, "name": "hold", "args": '{"id":9}', "status": "cancelled"},
            ],
        )
        assert details["calls"][0]["durationMs"] >= 0
        await opened.harness.close(context)

    async def test_stores_no_outcome_when_the_host_closes_the_sandbox_and_replays_the_script_after_a_restart(
        self,
    ) -> None:
        chat = codemodeChat()
        path = sqlitePath()
        chat.setup.faux.setResponses(
            [script('await tools.echo({ text: "a" }); await tools.hold({}); return "done";'), DONE]
        )
        opened = await openChat(await openNodeSqliteStorage(path), chat.setup)
        id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
        await chat.holds.reached.promise
        task = await scriptTask(opened.harness)
        # A host shutting down closes the sandbox before the Harness.
        await chat.sandbox.close()
        await asyncio.sleep(0.1)
        run = await opened.harness.snapshot(CodemodeRunDoc, task, context)
        assert run is not None and "result" not in run
        await opened.harness.close(context)
        # The next process has a new sandbox.
        chat.holds.block = False
        addTool(chat.setup.registry, createCodemodeTool({"sandbox": CodemodeSandbox({"memoryLimitBytes": 64 * 1024 * 1024})}))
        opened = await openChat(await openNodeSqliteStorage(path), chat.setup)
        opened.harness.resume()
        assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
        assert await resultText(opened.root) == {"text": "done", "isError": False}
        assert chat.echo == {"a": 1}
        await opened.harness.close(context)

    async def test_aborts_a_call_still_running_when_the_script_ends(self) -> None:
        chat = codemodeChat()
        chat.setup.faux.setResponses([script('tools.hold({}); await tools.echo({ text: "a" }); return 1;'), DONE])
        opened = await openChat(await openNodeSqliteStorage(sqlitePath()), chat.setup)
        await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
        assert await resultText(opened.root) == {"text": "1", "isError": False}
        hold = next(
            task
            for task in await toolTasks(opened.harness)
            if task["input"]["kind"] == "nested" and task["input"]["call"]["name"] == "hold"
        )
        assert chat.holds.runs == 1
        toMatchObject(hold["state"], {"status": "terminal", "outcome": {"status": "aborted"}})
        await opened.harness.close(context)

    async def test_admits_no_call_once_the_script_has_ended_even_one_it_made_before_ending(self) -> None:
        chat = codemodeChat()
        chat.holds.block = False
        storage = ControlledStorage()

        # Hold the fingerprint commit, and so the admission of the script's call, until the script has ended.
        def onStart(run: Any) -> None:
            held = storage.holdCommits()
            run.result.add_done_callback(lambda _: held.release())

        chat.vm.onStart = onStart
        chat.setup.faux.setResponses([script('tools.echo({ text: "late" }); return 1;'), DONE])
        opened = await openChat(storage, chat.setup)
        await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
        assert await resultText(opened.root) == {"text": "1", "isError": False}
        assert len(chat.echo) == 0
        assert await nestedCount(opened.harness) == 0
        await opened.harness.close(context)

    async def test_checks_a_reruns_calls_against_an_earlier_attempts_before_admitting_any_later_call(self) -> None:
        chat = codemodeChat()
        # With k set, call 1 has other arguments and call 2 is another tool than before; call 3 would be new.
        done = await restart(
            chat,
            """const k = load("k");
            tools.echo({ text: k ?? "a" });
            if (k) tools.echo({ text: "new" });
            await tools.hold({});""",
            # As if the restart came before the first journal commit.
            setStore({"k": "x"}, clearJournal=True),
        )
        assert done.result["isError"] is True
        # The first run already made more calls than before.
        assert "its first run made 3 calls, where it had made 2" in done.result["text"]
        assert await nestedCount(done.harness) == 2
        assert chat.echo == {"a": 1}
        await done.harness.close(context)

    async def test_ends_the_call_when_a_rerun_makes_a_global_call_where_an_earlier_attempt_made_a_tool_call(
        self,
    ) -> None:
        async def one(*_: Any) -> int:
            return 1

        chat = codemodeChat([{"name": "pick", "execute": one}])
        done = await restart(
            chat,
            'if (load("k")) { await pick(); await tools.echo({ text: "new" }); } else { await tools.hold({}); }',
            setStore({"k": True}),
        )
        assert done.result["isError"] is True
        assert "call 1 is pick(), but was tools.hold({})" in done.result["text"]
        assert len(chat.echo) == 0
        await done.harness.close(context)

    async def test_ends_the_call_when_a_reruns_first_run_makes_fewer_calls_than_an_earlier_attempt_admitted(
        self,
    ) -> None:
        # Live, the first run admits hold 1 and hold 2. With k set, it makes only hold 1, and after its result a global
        # and a new tool call: the global's ID is hold 2's key, which no check after the release would see.
        async def one(*_: Any) -> int:
            return 1

        chat = codemodeChat([{"name": "pick", "execute": one}])
        done = await restart(
            chat,
            """const k = load("k");
            const first = tools.hold({ id: 1 });
            if (!k) tools.hold({ id: 2 });
            await first;
            await pick();
            await tools.echo({ text: "new" });""",
            setStore({"k": True}),
            journalReaches(lambda journal: journal.get("firstIssued") == 2),
        )
        assert done.result["isError"] is True
        assert "its first run made 1 calls, where it had made 2" in done.result["text"]
        assert len(chat.echo) == 0
        await done.harness.close(context)

    async def test_ends_the_call_when_a_rerun_without_journal_entries_ends_in_its_first_run_where_it_went_on_before(
        self,
    ) -> None:
        chat = codemodeChat()
        done = await restart(
            chat,
            'if (load("k")) { store("x", 1); return "early"; } await tools.hold({});',
            setStore({"k": True}),
            journalReaches(lambda journal: journal.get("firstIssued") == 1),
        )
        assert done.result["isError"] is True
        assert "it ended after result 0, where it went on before" in done.result["text"]
        await done.harness.close(context)

    async def test_checks_every_call_a_rerun_holds_before_launching_any_of_them(self) -> None:
        # Live, call 1 is a global that never finishes and call 2 is hold; with k set, call 1 is a new tool call and
        # call 2 holds with other arguments. Checking call 1 alone would launch it before call 2's conflict shows.
        async def forever(_args: Any, _api: Any, callContext: Any) -> None:
            await aborted(callContext.abortSignal)

        chat = codemodeChat([{"name": "forever", "execute": forever}])
        done = await restart(
            chat,
            """const k = load("k");
            if (k) tools.echo({ text: "new" }); else forever();
            await tools.hold({ id: k ? 2 : 1 });""",
            setStore({"k": True}),
        )
        assert done.result["isError"] is True
        assert 'call 2 is tools.hold({"id":2}), but was tools.hold({"id":1})' in done.result["text"]
        assert len(chat.echo) == 0
        assert await nestedCount(done.harness) == 1
        await done.harness.close(context)

    async def test_keeps_a_globals_undefined_result_undefined_live_and_replayed(self) -> None:
        from misaka.utils.js import UNDEFINED

        async def nothing(*_: Any) -> Any:
            return UNDEFINED

        chat = codemodeChat([{"name": "nothing", "execute": nothing}])
        done = await restart(
            chat,
            "const before = await nothing(); await tools.hold({}); return [before === undefined, (await nothing()) === undefined];",
        )
        assert done.result == {"text": "[true,true]", "isError": False}
        await done.harness.close(context)

    async def test_rejects_a_failed_tool_call_with_its_error_text_and_structured_output(self) -> None:
        chat = codemodeChat()

        async def fails(*_: Any) -> dict[str, Any]:
            return {"output": [{"type": "text", "text": "permission denied"}], "isError": True}

        addTool(chat.setup.registry, tool("fails", fails))
        chat.holds.block = False
        chat.setup.faux.setResponses(
            [script("try { await tools.fails({}); } catch (e) { return [e.message, e.structuredOutput]; }"), DONE]
        )
        opened = await openChat(await openNodeSqliteStorage(sqlitePath()), chat.setup)
        await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
        assert await resultText(opened.root) == {"text": '["permission denied","permission denied"]', "isError": False}
        await opened.harness.close(context)

    async def test_resolves_a_schema_tools_error_result_that_carries_its_value_and_rejects_one_without_with_its_text(
        self,
    ) -> None:
        chat = codemodeChat()
        Exit = {
            "type": "object",
            "required": ["output", "exitCode"],
            "properties": {"output": {"type": "string"}, "exitCode": {"type": "number"}},
        }

        # Like bash on a nonzero exit: an error result with its value.
        async def run(*_: Any) -> dict[str, Any]:
            return {
                "output": [{"type": "text", "text": "no matches"}],
                "structuredOutput": {"output": "no matches", "exitCode": 1},
                "isError": True,
            }

        async def denied(*_: Any) -> dict[str, Any]:
            return {"output": [{"type": "text", "text": "permission denied"}], "isError": True}

        empty = {"type": "object", "properties": {}}
        addTool(
            chat.setup.registry,
            defineTool({"name": "run", "description": "run", "parameters": empty, "structuredOutputSchema": Exit, "execute": run}),
        )
        addTool(
            chat.setup.registry,
            defineTool(
                {"name": "denied", "description": "denied", "parameters": empty, "structuredOutputSchema": Exit, "execute": denied}
            ),
        )
        chat.holds.block = False
        chat.setup.faux.setResponses(
            [
                script(
                    """const r = await tools.run({});
                    let e; try { await tools.denied({}); } catch (error) { e = [error.message, error.structuredOutput ?? null]; }
                    return [r.exitCode, r.output, e];"""
                ),
                DONE,
            ]
        )
        opened = await openChat(await openNodeSqliteStorage(sqlitePath()), chat.setup)
        await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
        assert await resultText(opened.root) == {"text": '[1,"no matches",["permission denied",null]]', "isError": False}
        await opened.harness.close(context)

    async def test_resolves_a_schema_less_tool_with_its_text_as_its_declaration_says_also_when_it_returned_images(
        self,
    ) -> None:
        chat = codemodeChat()
        PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="

        async def picture(args: Any, *_: Any) -> dict[str, Any]:
            return {
                "output": [
                    *([{"type": "text", "text": "a cat"}] if args["caption"] else []),
                    {"type": "image", "data": PNG, "mimeType": "image/png"},
                ]
            }

        addTool(
            chat.setup.registry,
            defineTool(
                {
                    "name": "picture",
                    "description": "picture",
                    "parameters": {"type": "object", "required": ["caption"], "properties": {"caption": {"type": "boolean"}}},
                    "execute": picture,
                }
            ),
        )
        chat.holds.block = False
        chat.setup.faux.setResponses(
            [script("return [await tools.picture({ caption: true }), await tools.picture({ caption: false })];"), DONE]
        )
        opened = await openChat(await openNodeSqliteStorage(sqlitePath()), chat.setup)
        await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
        assert await resultText(opened.root) == {"text": '["a cat",""]', "isError": False}
        await opened.harness.close(context)

    async def test_delivers_a_result_only_after_its_journal_commit(self) -> None:
        storage = ControlledStorage()
        seen: list[str] = []

        async def first(*_: Any) -> int:
            seen.append("first")
            # The next commit is this result's journal commit: it never lands.
            storage.failNextCommit(RuntimeError("lost"))
            return 1

        async def second(*_: Any) -> int:
            seen.append("second")
            return 2

        chat = codemodeChat([{"name": "first", "execute": first}, {"name": "second", "execute": second}])
        chat.holds.block = False
        chat.setup.faux.setResponses([script("await first(); await second();"), DONE])
        opened = await openChat(storage, chat.setup)
        await opened.root.submit({"type": "input", "content": "go"}, context)
        await eventually(lambda: "first" in seen)
        await asyncio.sleep(0.1)
        assert seen == ["first"]
        with contextlib.suppress(Exception):
            await opened.harness.close(context)

    async def test_refuses_to_resume_when_the_sandboxs_fingerprint_or_the_code_mode_version_changed(self) -> None:
        for field in ["fingerprint", "version"]:
            chat = codemodeChat()

            async def between(opened: Any, task: int, field: str = field) -> None:
                async def edit(tx: Any) -> None:
                    run = await tx.doc(CodemodeRunDoc, task)
                    if field == "fingerprint":
                        run["start"]["fingerprint"] = "another sandbox"
                    else:
                        run["start"]["version"] = 0

                await opened.commit(edit, context)

            done = await restart(chat, 'await tools.hold({}); await tools.echo({ text: "late" });', between)
            assert done.result["isError"] is True
            assert "cannot resume" in done.result["text"]
            assert len(chat.echo) == 0
            await done.harness.close(context)

    async def test_times_a_script_out_across_a_restart_without_admitting_new_calls(self) -> None:
        chat = codemodeChat()

        async def wait(*_: Any) -> None:
            await asyncio.sleep(0.4)

        done = await restart(
            chat,
            """// @options: {"timeout_ms": 300}
            await tools.hold({}); await tools.echo({ text: "late" });""",
            wait,
        )
        assert done.result["isError"] is True
        assert "deadline passed" in done.result["text"]
        assert len(chat.echo) == 0
        assert chat.vm.starts == 1
        await done.harness.close(context)

    async def test_returns_a_decided_outcome_after_a_restart_without_running_the_script_again(self) -> None:
        chat = codemodeChat()
        path = sqlitePath()
        chat.holds.block = False
        chat.setup.faux.setResponses([script('return "first"'), DONE])
        # Stop after execute() returned, so after the outcome is stored, and before the result commits.
        stopped = SimpleNamespace(block=True, reached=deferred())

        async def afterTool(call: Any, _result: Any, _api: Any, hookContext: Any) -> None:
            if call["name"] != "codemode" or not stopped.block:
                return
            stopped.reached.resolve()
            await aborted(hookContext.abortSignal)
            return

        addHooks(chat.setup.registry, ToolTask, {"afterTool": afterTool})
        opened = await openChat(await openNodeSqliteStorage(path), chat.setup)
        id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
        await stopped.reached.promise
        task = await scriptTask(opened.harness)
        run = await opened.harness.snapshot(CodemodeRunDoc, task, context)
        assert run is not None and "result" in run
        await opened.harness.close(context)
        stopped.block = False
        opened = await openChat(await openNodeSqliteStorage(path), chat.setup)
        opened.harness.resume()
        assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
        assert await resultText(opened.root) == {"text": "first", "isError": False}
        assert chat.vm.starts == 1
        await opened.harness.close(context)

    async def test_keeps_store_values_per_conversation_as_of_a_forks_entry(self) -> None:
        chat = codemodeChat()
        chat.holds.block = False
        chat.setup.faux.setResponses(
            [script('store("k", 1); return load("k")'), DONE, script('return [load("k") ?? null]'), DONE]
        )
        opened = await openChat(await openNodeSqliteStorage(sqlitePath()), chat.setup)
        root = opened.root
        await (await root.submit({"type": "input", "content": "first"}, context)).wait(context)
        firstUser = next(entry for entry in await allEntries(root) if entry["kind"] == "pi.user")
        await (await root.submit({"type": "input", "content": "second"}, context)).wait(context)
        results = [entry for entry in await allEntries(root) if ToolResultEntry.is_(entry)]
        texts = [entry["model"][0]["content"][1]["text"] for entry in results]
        assert texts == ["1", "[1]"]
        # A fork at the first input is from before the first script stored anything.
        chat.setup.faux.setResponses([script('return [load("k") ?? null]'), DONE])
        fork = await root.fork(firstUser["id"], {"ownership": {"kind": "ownerless"}}, context)
        await (await fork.submit({"type": "input", "content": "fork"}, context)).wait(context)
        forkResult = [entry for entry in await allEntries(fork) if ToolResultEntry.is_(entry)][-1]
        assert forkResult["model"][0]["content"][1]["text"] == "[null]"
        await opened.harness.close(context)

    async def test_journals_results_that_finish_together_in_one_commit(self) -> None:
        release = deferred()
        waiting = 0

        async def wait(*_: Any) -> None:
            nonlocal waiting
            waiting += 1
            if waiting == 50:
                release.resolve()
            await release.promise

        chat = codemodeChat([{"name": "wait", "execute": wait}])
        chat.holds.block = False
        chat.setup.faux.setResponses(
            [script("return (await Promise.all(Array.from({ length: 50 }, () => wait()))).length"), DONE]
        )
        storage = await openNodeSqliteStorage(sqlitePath())
        # MISAKA fork: upstream's premise is that the VM acknowledges a group of delivered results within one storage
        # commit. misaka's VM runs on a thread that shares the GIL and takes about 0.1 ms per result, against
        # sub-millisecond SQLite commits, so the 49 acknowledgments would spread over more commits than upstream's; each
        # commit here takes 5 ms more, so the VM catches up during one, as in upstream's runs.
        commit = storage.commit

        async def slowCommit(writes: Any, callContext: Any) -> Any:
            await asyncio.sleep(0.005)
            return await commit(writes, callContext)

        storage.commit = slowCommit  # type: ignore[method-assign]
        opened = await openChat(storage, chat.setup)
        journalCommits = 0

        def onCommit(publication: Any, *_: Any) -> None:
            nonlocal journalCommits
            for change in publication["changes"]:
                if change["type"] == "document" and change["record"]["kind"] == "pi.codemode.journal":
                    journalCommits += 1

        opened.harness.subscribeCommits(onCommit)
        await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
        assert await resultText(opened.root) == {"text": "50", "isError": False}
        # The journal's creation and a few groups, not one commit per result (51).
        assert journalCommits < 10
        await opened.harness.close(context)


async def runScript(code: str, chat: Any = None) -> SimpleNamespace:
    """Run ``code`` once in a chat with a Node environment; the result's content and details."""
    chat = chat or codemodeChat()
    chat.holds.block = False
    cwd = tempDirectory("pi-codemode-env-")
    chat.setup.faux.setResponses([script(code), DONE])
    opened = await openChat(await openNodeSqliteStorage(sqlitePath()), chat.setup, {"env": NodeExecutionEnv({"cwd": cwd})})
    await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
    entry = next(candidate for candidate in await allEntries(opened.root) if ToolResultEntry.is_(candidate))
    message = entry["model"][0]
    await opened.harness.close(context)
    return SimpleNamespace(
        text="".join(item["text"] if item.get("text") is not None else f"<{item['type']}>" for item in message["content"]),
        message=message,
    )


class TestDurableCodeModeOutputAndToolSearch:
    async def test_lays_out_text_items_console_lines_and_the_returned_value_under_a_header(self) -> None:
        text = (await runScript('text("one"); console.log("log", 1); text({ two: 2 }); return "three";')).text
        assert HEADER.match(text)
        assert '==> text 1/3 <==\none\n==> text 2/3 <==\n{"two":2}\n==> text 3/3 <==\nthree' in text
        assert "<console_output>\nlog 1\n</console_output>" in text

    async def test_cuts_text_past_the_token_budget_to_its_start_and_end_with_the_full_text_in_a_file(self) -> None:
        done = await runScript(
            """// @options: {"max_output_tokens": 20}
            text("BEGIN" + "x".repeat(1000) + "END");"""
        )
        assert "Warning: truncated output" in done.text
        assert "BEGIN" in done.text
        assert "END" in done.text
        path = done.message["details"]["fullOutputPath"]
        directories.add(os.path.dirname(path))
        assert f"[Full output: {path}" in done.text
        assert Path(path).read_text(encoding="utf-8") == f"BEGIN{'x' * 1000}END"

    async def test_keeps_a_result_inside_the_tools_output_limits_whatever_max_output_tokens_says_spilling_the_rest(
        self,
    ) -> None:
        # 6 MiB of text: over the Harness's 4 MiB limit, which would cut it without a spill file.
        big = await runScript(
            """// @options: {"max_output_tokens": 2000000}
            return "x".repeat(6 * 1024 * 1024);"""
        )
        assert "Warning: truncated output" in big.text
        assert len(big.text) <= 1024 * 1024 + 1000
        directories.add(os.path.dirname(big.message["details"]["fullOutputPath"]))
        assert len(Path(big.message["details"]["fullOutputPath"]).read_text(encoding="utf-8")) == 6 * 1024 * 1024
        # 200,000 short lines: within the byte limit, over the 100,000 lines one.
        long = await runScript(
            """// @options: {"max_output_tokens": 2000000}
            return Array.from({ length: 200000 }, () => "a").join("\\n");"""
        )
        assert "Warning: truncated output" in long.text
        assert len(long.text.split("\n")) < 100_000
        assert long.message["details"].get("fullOutputPath") is not None
        directories.add(os.path.dirname(long.message["details"]["fullOutputPath"]))

    async def test_writes_the_results_files_to_the_same_paths_when_it_reruns_after_a_crash(self) -> None:
        chat = codemodeChat()
        chat.holds.block = False
        cwd = tempDirectory("pi-codemode-env-")
        written = deferred()

        # The process dies after the full output was written and before the result was committed.
        class CrashAfterWrite(NodeExecutionEnv):
            async def writeFile(self, path: str, content: str | bytes, callContext: Any) -> Any:
                result = await super().writeFile(path, content, callContext)
                written.resolve(path)
                await aborted(callContext.abortSignal)
                return result

        storage = sqlitePath()
        chat.setup.faux.setResponses([script('// @options: {"max_output_tokens": 20}\nreturn "x".repeat(1000);'), DONE])
        opened = await openChat(await openNodeSqliteStorage(storage), chat.setup, {"env": CrashAfterWrite({"cwd": cwd})})
        id = (await opened.root.submit({"type": "input", "content": "go"}, context)).id
        first = await written.promise
        await opened.harness.close(context)
        opened = await openChat(await openNodeSqliteStorage(storage), chat.setup, {"env": NodeExecutionEnv({"cwd": cwd})})
        opened.harness.resume()
        assert (await (await opened.harness.submission(id, context)).wait(context))["status"] == "done"
        entry = next(candidate for candidate in await allEntries(opened.root) if ToolResultEntry.is_(candidate))
        details = entry["model"][0].get("details")
        assert details["fullOutputPath"] == first
        directories.add(os.path.dirname(first))
        assert os.listdir(os.path.dirname(first)) == ["output.txt"]
        await opened.harness.close(context)

    async def test_saves_images_to_files_and_names_them_next_to_the_image(self) -> None:
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
        done = await runScript(f'image("data:image/png;base64,{png}");')
        saved = re.search(r"\[Image saved to (\S+) \(image/png, \d+ bytes\)\]", done.text)
        assert saved is not None
        directories.add(os.path.dirname(saved.group(1)))
        assert base64.b64encode(Path(saved.group(1)).read_bytes()).decode("ascii") == png
        assert any(item["type"] == "image" for item in done.message["content"])

    async def test_calls_a_deferred_tool_the_model_is_not_offered_and_finds_it_in_all_tools(self) -> None:
        chat = codemodeChat()

        async def later(*_: Any) -> dict[str, Any]:
            return {"output": [{"type": "text", "text": "later ran"}]}

        addTool(
            chat.setup.registry,
            defineTool(
                {
                    "name": "later",
                    "description": "A deferred tool",
                    "parameters": {"type": "object", "properties": {}},
                    "deferred": True,
                    "execute": later,
                }
            ),
        )
        done = await runScript('return [await tools.later({}), ALL_TOOLS.some((tool) => tool.name === "later")];', chat)
        assert '["later ran",true]' in done.text

    async def test_reports_a_failed_script_with_its_error_and_the_calls_already_made(self) -> None:
        done = await runScript('await tools.echo({ text: "a" }); throw new Error("boom");')
        assert done.message["isError"] is True
        assert re.match(r"^Script failed\n", done.text)
        assert "Error: boom" in done.text
        assert "Tool calls made before the failure (they are not undone): echo (ok)" in done.text

    async def test_finds_tools_with_search_tools_describe_tool_and_describe_namespace_and_lists_them_in_all_tools(
        self,
    ) -> None:
        chat = codemodeChat([], [{"name": "github", "description": "GitHub MCP server", "prefix": "mcp__github__"}])

        async def listIssues(*_: Any) -> dict[str, Any]:
            return {"output": []}

        addTool(chat.setup.registry, tool("mcp__github__list_issues", listIssues))
        done = await runScript(
            """const found = await searchTools("issues");
            const inNamespace = await searchTools("echo", { namespace: "github" });
            return {
                found: found.map((tool) => tool.name),
                inNamespace,
                described: (await describeTool("echo")).includes("echo(args:"),
                missing: (await describeTool("nope")) ?? null,
                namespace: await describeNamespace("github"),
                all: ALL_TOOLS.map((tool) => tool.name).includes("mcp__github__list_issues"),
            };""",
            chat,
        )
        assert json.loads(HEADER.sub("", done.text)) == {
            "found": ["mcp__github__list_issues"],
            "inNamespace": [],
            "described": True,
            "missing": None,
            "namespace": {"name": "github", "description": "GitHub MCP server", "tools": ["mcp__github__list_issues"]},
            "all": True,
        }
