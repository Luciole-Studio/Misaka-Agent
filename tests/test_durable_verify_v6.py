"""Regression tests for the independent verification of the durable port (VERIFY-V6, durable harness/session/code mode).

Each test names the finding it pins. A ``CancelledError`` raised by awaiting a future someone else cancelled, while the
running task itself is not cancelled, is the Python form of awaiting a rejected promise: upstream's ``catch`` sees it.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import tempfile
import time
from typing import Any

import pytest

from misaka.ai.providers.faux import fauxAssistantMessage, fauxText
from misaka.ai.types import ClassifierResult
from misaka.chord.context import BACKGROUND_CONTEXT, withAbortSignal
from misaka.durable import LiveDoc, MemoryStorage, ToolTask
from misaka.durable.documents import defineDoc
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.harness.usage import addUsage
from misaka.durable.harness.util import Waiters
from misaka.durable.session.session import SessionImpl
from misaka.utils.js import AbortController, eager
from tests.chat_support import allEntries, chatSetup, openChat
from tests.durable_support import context
from tests.harness_support import addHooks, addTool
from tests.test_durable_codemode_models import USAGE, modelsChat
from tests.test_durable_codemode_models import run as runCodemode
from tests.test_durable_harness_tools import (
    DONE,
    calls,
    consume,
    results,
    resultText,
    tool,
)


async def cancelledAwait() -> None:
    """Await a future that was cancelled: ``CancelledError`` while the running task is not being cancelled."""
    future = asyncio.get_running_loop().create_future()
    future.cancel()
    await future


async def toolOutcomes(storage: MemoryStorage) -> list[Any]:
    page = await storage.scanTasks({"kind": "pi.tool"}, 10, None, context)
    return [task["state"].get("outcome") for task in page["items"]]


# --- V6-SCH-B01: tool execute and tool hooks see a cancelled await as a throw ------------------------------------------


async def test_sch_b01_a_tool_that_awaits_a_cancelled_future_fails_with_its_partial_output() -> None:
    setup = chatSetup()

    async def fail(_args: Any, api: Any, _context: Any) -> dict[str, Any]:
        api.output("partial\n")
        await cancelledAwait()
        return {}

    addTool(setup.registry, tool("fail", fail))
    setup.faux.setResponses([calls(("fail", {}, "c1")), DONE])
    storage = MemoryStorage()
    opened = await openChat(storage, setup)
    settled = await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
    entries = await allEntries(opened.root)
    assert settled["status"] == "done"
    [result] = results(entries)
    assert result["isError"] is True
    assert resultText(result) == "partial\n|<harness>\n[error] The operation was aborted\n</harness>"
    [outcome] = await toolOutcomes(storage)
    assert outcome["status"] == "failed"
    assert outcome["error"] == {"message": "Tool fail threw"}
    await opened.harness.close(context)


async def test_sch_b01_an_after_tool_hook_that_awaits_a_cancelled_future_is_reported_and_the_result_commits() -> None:
    setup = chatSetup()

    async def echo(_args: Any, _api: Any, _context: Any) -> dict[str, Any]:
        return {"output": [{"type": "text", "text": "raw"}]}

    async def afterTool(*_: Any) -> None:
        await cancelledAwait()

    addTool(setup.registry, tool("echo", echo))
    addHooks(setup.registry, ToolTask, {"afterTool": afterTool})
    setup.faux.setResponses([calls(("echo", {}, "c1")), DONE])
    storage = MemoryStorage()
    opened = await openChat(storage, setup)
    await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
    entries = await allEntries(opened.root)
    assert [(result["isError"], resultText(result)) for result in results(entries)] == [(False, "raw")]
    assert [outcome["status"] for outcome in await toolOutcomes(storage)] == ["completed"]
    assert "The operation was aborted" in [str(error) for error in setup.reports]
    await opened.harness.close(context)


async def test_sch_b01_a_before_tool_hook_that_awaits_a_cancelled_future_blocks_the_call() -> None:
    setup = chatSetup()
    ran: list[str] = []

    async def echo(_args: Any, _api: Any, _context: Any) -> dict[str, Any]:
        ran.append("echo")
        return {"output": [{"type": "text", "text": "raw"}]}

    async def beforeTool(*_: Any) -> None:
        await cancelledAwait()

    addTool(setup.registry, tool("echo", echo))
    addHooks(setup.registry, ToolTask, {"beforeTool": beforeTool})
    setup.faux.setResponses([calls(("echo", {}, "c1")), DONE])
    storage = MemoryStorage()
    opened = await openChat(storage, setup)
    await (await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context)
    entries = await allEntries(opened.root)
    assert ran == []
    [result] = results(entries)
    assert result["isError"] is True
    assert "Tool call blocked: The operation was aborted" in resultText(result)
    assert [outcome["status"] for outcome in await toolOutcomes(storage)] == ["completed"]
    await opened.harness.close(context)


async def test_sch_b01_thrown_by_maps_a_cancelled_await_and_reraises_the_tasks_own_cancellation() -> None:
    from misaka.durable.harness.util import asThrown, thrownBy

    caught: list[BaseException] = []
    try:
        await cancelledAwait()
    except asyncio.CancelledError as error:
        caught.append(thrownBy(error))
    assert [type(error).__name__ for error in caught] == ["DOMException"]
    assert str(caught[0]) == "The operation was aborted"

    started = asyncio.Event()
    outcome: list[str] = []

    async def cancelledItself() -> None:
        try:
            started.set()
            await asyncio.sleep(10)
        except asyncio.CancelledError as error:
            try:
                thrownBy(error)
            except asyncio.CancelledError:
                outcome.append("re-raised")
                raise

    task = asyncio.ensure_future(cancelledItself())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert outcome == ["re-raised"]
    assert isinstance(asThrown(KeyboardInterrupt()), Exception)


# --- V4-B15 (durable side): chord's `Progress.stop()` hands back `Deferred` waiters ---------------------------------


async def test_v4_b15_details_still_waiting_when_execute_returns_settle_with_the_terminal_commit() -> None:
    setup = chatSetup()
    holder: dict[str, Any] = {}

    async def slow(_args: Any, api: Any, _context: Any) -> dict[str, Any]:
        api.output("first\n")
        # The output commit is in flight, so these details wait for the next window: the terminal commit settles them.
        holder["pending"] = api.details({"step": 1}, context)
        consume(holder["pending"])
        return {}

    addTool(setup.registry, tool("slow", slow))
    setup.faux.setResponses([calls(("slow", {}, "c1")), DONE])
    opened = await openChat(MemoryStorage(), setup)
    await asyncio.wait_for((await opened.root.submit({"type": "input", "content": "go"}, context)).wait(context), 10)
    assert await asyncio.wait_for(holder["pending"], 10) is None
    await opened.harness.close(context)


async def test_v4_b15_details_still_waiting_reject_when_the_call_is_aborted() -> None:
    setup = chatSetup()
    holder: dict[str, Any] = {}
    inAfterTool = asyncio.Event()

    async def slow(_args: Any, api: Any, _context: Any) -> dict[str, Any]:
        api.output("first\n")
        holder["pending"] = api.details({"step": 1}, context)
        consume(holder["pending"])
        return {}

    async def afterTool(_call: Any, _result: Any, _api: Any, callContext: Any) -> None:
        inAfterTool.set()
        future = asyncio.get_running_loop().create_future()
        callContext.abortSignal.addEventListener("abort", lambda: future.done() or future.set_result(None), once=True)
        await future
        raise RuntimeError("aborted")

    addTool(setup.registry, tool("slow", slow))
    addHooks(setup.registry, ToolTask, {"afterTool": afterTool})
    setup.faux.setResponses([calls(("slow", {}, "c1")), DONE])
    opened = await openChat(MemoryStorage(), setup)
    submission = await opened.root.submit({"type": "input", "content": "go"}, context)
    await asyncio.wait_for(inAfterTool.wait(), 10)
    taskId = (await opened.harness.snapshot(LiveDoc, opened.root.id, context))["tools"][0]["taskId"]
    await opened.harness.abortTask(taskId, context)
    # Rejected, not left waiting (a wait that times out would raise TimeoutError).
    with pytest.raises(Exception) as rejected:
        await asyncio.wait_for(holder["pending"], 10)
    assert not isinstance(rejected.value, TimeoutError)
    await asyncio.wait_for(submission.wait(context), 10)
    await opened.harness.close(context)


# --- V6-SCH-B02 / B03: waiters and the scheduler's ordered sets -----------------------------------------------------


async def test_sch_b02_a_waiter_its_awaiter_gave_up_on_leaves_its_set() -> None:
    waiters = Waiters()
    for _ in range(100):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(waiters.add(7, context), 0)
    assert waiters.keys() == []
    controller = AbortController()
    kept = waiters.add(7, withAbortSignal(controller.signal, context))
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(waiters.add(7, context), 0)
    assert waiters.keys() == [7]
    waiters.resolve(7, "done")
    assert await kept == "done"


async def test_sch_b03_fail_fast_checks_keep_insertion_order() -> None:
    setup = chatSetup()
    opened = await openChat(MemoryStorage(), setup)
    checks = opened.harness._tasks._failFastChecks
    checks[3] = None
    checks[9] = None
    assert list(checks) == [3, 9]
    checks.clear()
    await opened.harness.close(context)


# --- V6-SES-B01 / B02: the Storage guard and `failed` -------------------------------------------------------------

Counter = defineDoc(
    {"kind": "t.v6.counter", "version": 1, "scope": "session", "history": "latest", "initial": lambda: {"count": 0}}
)


class CommitThenRaise(MemoryStorage):
    """A backend whose next commit lands, then raises."""

    failNext: BaseException | None = None

    @eager
    async def commit(self, writes: Any, callContext: Any) -> Any:
        seq = await MemoryStorage.commit(self, writes, callContext)
        if self.failNext is not None:
            error, self.failNext = self.failNext, None
            raise error
        return seq


async def increment(session: SessionImpl) -> None:
    async def change(tx: Any) -> None:
        doc = await tx.doc(Counter)
        doc["count"] = doc["count"] + 1

    await session.commit(change, BACKGROUND_CONTEXT)


@pytest.mark.parametrize("error", [RuntimeError("boom"), asyncio.CancelledError()], ids=["throw", "cancelled"])
async def test_ses_b01_a_failed_commit_fails_the_session_whatever_it_raised(error: BaseException) -> None:
    storage = CommitThenRaise()
    session = SessionImpl(storage)
    await increment(session)
    storage.failNext = error
    with pytest.raises(BaseException):  # noqa: B017 - the backend's own error
        await increment(session)
    assert session.failure is not None
    with pytest.raises(Exception, match="failed"):
        await increment(session)
    record = await storage.findDocument({"kind": "t.v6.counter", "scope": {"kind": "session"}}, "current", context)
    assert (await storage.document(record["id"], "current", context))["value"]["count"] == 2


async def test_ses_b01_a_read_whose_own_task_is_cancelled_does_not_fail_the_session() -> None:
    entered = asyncio.Event()

    class SlowRead(MemoryStorage):
        @eager
        async def findDocument(self, *args: Any) -> Any:
            entered.set()
            await asyncio.sleep(10)
            return await MemoryStorage.findDocument(self, *args)

    session = SessionImpl(SlowRead())

    async def read(tx: Any) -> None:
        await tx.doc(Counter)

    task = asyncio.ensure_future(session.commit(read, BACKGROUND_CONTEXT))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert session.failure is None


@pytest.mark.parametrize("early", [True, False], ids=["read-before-close", "read-after-close"])
async def test_ses_b02_failed_does_not_reject_for_a_storage_that_cannot_close(early: bool) -> None:
    class BadClose(MemoryStorage):
        @eager
        async def close(self, _context: Any) -> None:
            raise RuntimeError("cannot close")

    session = SessionImpl(BadClose())
    if early:
        session.failed  # noqa: B018 - made before the close
    with pytest.raises(RuntimeError, match="cannot close"):
        await session.close(context)
    failed = session.failed
    await asyncio.sleep(0)
    assert failed.done() is False
    assert session.failure is not None
    assert (await session.closed)["reason"] == "failed"


# --- V6-SES-B03: the forced retention reads like JavaScript's `Number()` ------------------------------------------


@pytest.mark.parametrize("value", ["", "junk", "0", " 25 "])
def test_ses_b03_a_forced_retention_reads_like_number(value: str) -> None:
    import subprocess

    probe = (
        "import tests.durable_support, misaka.durable.session.session as s; "
        "v = s.documentRetention(None); print(v)"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env={**os.environ, "MISAKA_DURABLE_DOCUMENT_RETENTION_MS": value},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert done.returncode == 0, done.stderr
    retention = float(done.stdout.strip())
    expected = {"": 0.0, "junk": float("nan"), "0": 0.0, " 25 ": 25.0}[value]
    assert retention == expected or (math.isnan(retention) and math.isnan(expected))


# --- V6-CM-B01: usage counters a provider left unset stay absent ---------------------------------------------------


async def test_cm_b01_a_later_call_reporting_reasoning_adds_to_usage_that_had_none() -> None:
    chat = modelsChat()
    seen = {"n": 0}

    async def classify(model: Any, _context: Any, _options: Any = None) -> ClassifierResult:
        seen["n"] += 1
        usage = {**USAGE, **({"reasoning": 3} if seen["n"] == 2 else {})}
        return ClassifierResult.model_validate(
            {
                "api": model.api,
                "provider": model.provider,
                "model": model.id,
                "answers": {"good": {"type": "bool", "probability": 1}},
                "usage": usage,
                "stopReason": "stop",
                "timestamp": 0,
            }
        )

    chat.setup.models.classify = classify
    done = await runCodemode(
        chat,
        """const model = await models.getModelOfType("classifier", "test", "judge");
        const c = { state: {}, questions: { good: { type: "bool", instructions: "?", criteria: { true: "y", false: "n" } } } };
        const a = await models.classify(model, c);
        const b = await models.classify(model, c);
        return [a.stopReason, b.stopReason];""",
    )
    assert json.loads(done.text) == ["stop", "stop"]
    total = done.usage["tools"]["pi.codemode.models.classify"]
    assert "cacheWrite1h" not in total
    assert total["reasoning"] == 3
    assert total["input"] == 20


def test_cm_b01_add_usage_treats_a_stored_null_counter_as_zero() -> None:
    total = {**json.loads(json.dumps(USAGE)), "reasoning": None, "cacheWrite1h": None}
    addUsage(total, {**USAGE, "reasoning": 2, "cacheWrite1h": 5})
    assert (total["reasoning"], total["cacheWrite1h"]) == (2, 5)


# --- V6-HAR-B01: an input entry of `None` is `null`, which upstream rejects -----------------------------------------


async def test_har_b01_an_input_whose_entry_is_none_is_rejected_and_nothing_is_written() -> None:
    setup = chatSetup()
    setup.faux.setResponses([fauxAssistantMessage([fauxText("ok")])])
    opened = await openChat(MemoryStorage(), setup)
    before = await allEntries(opened.root)
    with pytest.raises(TypeError, match="Cannot destructure property 'kind' of 'entry' as it is null"):
        await opened.root.submit({"type": "input", "content": "x", "entry": None}, context)
    assert await allEntries(opened.root) == before
    await opened.harness.close(context)


# --- V6-ME-B02: a watched file rewritten in place is reported (macOS: FSEvents, as libuv) ----------------------------


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="native watching")
async def test_me_b02_a_directory_watch_reports_a_file_in_it_rewritten_in_place() -> None:
    """``fs.watch(directory)`` reports a file in it whose content changes: libuv uses FSEvents for a directory on macOS,
    whose file events include it; a kqueue watch of the directory reports only entries added, removed or renamed."""
    from misaka.durable.env.node_watch import _fsWatch

    root = tempfile.mkdtemp(prefix="misaka-v6-fswatch-")
    inner = os.path.join(root, "inner.txt")
    writeFile(inner, "a", "w")
    await asyncio.sleep(0.3)
    names: list[str | None] = []
    watcher = _fsWatch(root, False, names.append, lambda: names.append("<error>"))
    try:
        await asyncio.sleep(0.3)
        writeFile(inner, "b", "r+")
        deadline = time.monotonic() + 2
        while "inner.txt" not in names and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert "inner.txt" in names
    finally:
        watcher.close()


def writeFile(path: str, content: str, mode: str) -> None:
    with open(path, mode, encoding="utf-8") as file:
        file.write(content)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows polls")
async def test_me_b02_a_watched_file_rewritten_in_place_is_reported_without_any_other_event() -> None:
    root = tempfile.mkdtemp(prefix="misaka-v6-watch-")
    env = NodeExecutionEnv({"cwd": root})
    changes: list[dict[str, Any]] = []
    target = os.path.join(root, "AGENTS.md")
    writeFile(target, "one", "w")
    watcher = (await env.watch([{"path": "AGENTS.md"}, {"path": "dir"}], changes.append, context))["value"]
    os.mkdir(os.path.join(root, "dir"))
    writeFile(os.path.join(root, "dir", "inner.txt"), "a", "w")
    # Past the debounce and macOS's settle rescan: nothing but the rewrite itself can report the change now.
    await asyncio.sleep(1.2)

    async def rewrite(path: str, content: str) -> bool:
        start = len(changes)
        # In place: the file keeps its inode, its directory's entries do not change.
        writeFile(path, content, "r+")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if any(path in change.get("paths", []) for change in changes[start:]):
                return True
            await asyncio.sleep(0.02)
        return False

    try:
        assert await rewrite(target, "two!") is True
        assert await rewrite(os.path.join(root, "dir", "inner.txt"), "b") is True
    finally:
        await watcher.close(context)


# --- R2-N6 (applied to the new BaseException sites): a closed coroutine is neither a rejection nor a cancellation ---


async def test_closing_a_coroutine_that_waits_on_a_commit_does_not_fail_the_session() -> None:
    session = SessionImpl(MemoryStorage())
    guarded = session.storage
    pending = asyncio.get_running_loop().create_future()

    async def run() -> Any:
        return await pending

    # The undecorated body: a coroutine that can be closed while it waits, as a loop closed without cancelling does.
    body = type(guarded)._call.__wrapped__(guarded, None, run)
    body.send(None)
    body.close()
    pending.cancel()
    await asyncio.sleep(0)
    assert session.failure is None
