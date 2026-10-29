"""pi coding-agent's ``codemode-worker-admission.test.ts`` (#10748, #11184): at most two codemode workers per process,
counted until each actually exits; further scripts wait for a free one in arrival order.

Upstream mocks ``Worker.prototype.terminate`` to keep terminated workers alive; here each worker's termination
request is held back, which keeps its thread waiting until the test lets it go.
"""

from __future__ import annotations

import asyncio
import re
import time

import pytest

from misaka.codemode.runtime import host as codemode_host
from misaka.codemode.runtime import worker as codemode_worker
from misaka.extensions.codemode import execute as codemode_execute
from misaka.extensions.codemode.execute import executeCodemode
from misaka.utils.js import AbortController


def texts(result) -> list[str]:
    return [block.text for block in result.content if block.type == "text"]


@pytest.fixture
def terminations(monkeypatch):
    """The workers whose termination was requested and is held back, in order."""
    held: list[codemode_worker.Worker] = []

    def terminate(self):
        held.append(self)
        return asyncio.get_running_loop().create_future()

    monkeypatch.setattr(codemode_worker.Worker, "terminate", terminate)
    yield held
    for worker in held:
        exitWorker(worker)
    # Every reservation comes back once the threads end; later tests start from zero.
    deadline = time.monotonic() + 30
    while codemode_execute.activeCodemodeWorkers > 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert codemode_execute.activeCodemodeWorkers == 0
    assert codemode_execute.codemodeWorkerWaiters == []


def exitWorker(worker) -> None:
    """Let a held worker exit: the request the real ``terminate()`` sends."""
    worker._inbox.put(codemode_worker._TERMINATE)


async def run(code: str, signal=None):
    return await executeCodemode("worker-admission", {"code": code}, signal, None, None)


async def releaseOne(terminations) -> None:
    """Let the first held worker actually exit."""
    worker = terminations.pop(0)
    exitWorker(worker)
    await asyncio.wait_for(asyncio.wrap_future(worker.threadExited), 10)


async def waitFor(check) -> None:
    deadline = time.monotonic() + 10
    while not check():
        if time.monotonic() > deadline:
            raise AssertionError("Condition was not reached")
        await asyncio.sleep(0.005)


# #10748: fresh sandboxes must share capacity and retain it until actual worker exit.
@pytest.mark.parametrize("how", ["timeout", "abort", "success"])
async def test_bounds_surviving_workers(how, terminations, monkeypatch):
    started: list[codemode_worker.Worker] = []
    base = codemode_host.Worker

    class Recording(base):
        def __init__(self, workerData) -> None:
            super().__init__(workerData)
            started.append(self)

    monkeypatch.setattr(codemode_host, "Worker", Recording)
    for i in range(2):
        controller = AbortController()
        startedBefore = len(started)
        code = (
            '// @options: {"timeout_ms": 200}\nwhile (true) {}'
            if how == "timeout"
            else "while (true) {}" if how == "abort" else "return 1"
        )
        pending = asyncio.ensure_future(run(code, controller.signal))
        if how == "abort":
            await waitFor(lambda before=startedBefore: len(started) > before)
            controller.abort()
        result = await pending
        if how == "success":
            assert result.isError is None
        else:
            assert result.isError is True
            assert any(("Script timed out:" if how == "timeout" else "Script aborted:") in text for text in texts(result))
        assert len(terminations) == i + 1

    # #11184: at capacity, a script waits for a worker instead of failing.
    waiting = asyncio.ensure_future(run("return 3"))

    abortWhileWaiting = AbortController()
    aborted = asyncio.ensure_future(run("while (true) {}", abortWhileWaiting.signal))
    await asyncio.sleep(0.05)
    abortWhileWaiting.abort()
    abortedResult = await aborted
    assert abortedResult.isError is True
    assert any(re.search(r"Script aborted:[\s\S]*The script did not start\.", text) for text in texts(abortedResult))

    timedOut = await run('// @options: {"timeout_ms": 50}\nreturn 1')
    assert timedOut.isError is True
    assert any("Execution timed out after 50 ms while waiting for a free codemode worker" in text for text in texts(timedOut))

    assert not waiting.done()
    assert len(terminations) == 2

    # Releasing one actual worker, not its result, admits exactly the waiting script.
    await releaseOne(terminations)
    admitted = await waiting
    assert admitted.isError is None
    assert "3" in texts(admitted)
    assert len(terminations) == 2


# #10748: timed-out launches resuming after wasm loading must not start workers.
async def test_starts_no_worker_for_a_launch_that_resumes_after_its_result_timed_out(terminations, monkeypatch):
    wasm = await codemode_host.loadQuickJSWasm()
    loading = asyncio.get_running_loop().create_future()
    monkeypatch.setattr(codemode_host, "loadQuickJSWasm", lambda *_args: loading)
    try:
        results = await asyncio.gather(*(run('// @options: {"timeout_ms": 20}\nwhile (true) {}') for _ in range(3)))
        assert all(result.isError for result in results)
        assert len(terminations) == 0
        loading.set_result(wasm)
        assert (await run("return 1")).isError is None
        # Only the last script's worker started.
        assert len(terminations) == 1
    finally:
        if not loading.done():
            loading.set_result(wasm)


# #10748, #11184: a concurrent burst runs two workers at a time and queues the rest.
async def test_queues_a_concurrent_burst_behind_two_workers(terminations):
    results: list = []

    async def one():
        results.append(await run("return 1"))

    pending = [asyncio.ensure_future(one()) for _ in range(6)]
    for released in range(4):
        await waitFor(lambda released=released: len(terminations) == 2 and len(results) == released + 2)
        await asyncio.sleep(0.05)
        assert len(results) == released + 2
        await releaseOne(terminations)
    await asyncio.gather(*pending)
    assert all(result.isError is None for result in results)
    assert len(terminations) == 2


# #11184: in-process hosts with many sessions can raise the limit.
async def test_reads_the_limit_from_misaka_codemode_max_workers(terminations, monkeypatch):
    monkeypatch.setenv("MISAKA_CODEMODE_MAX_WORKERS", "3")
    results: list = []

    async def one():
        results.append(await run("return 1"))

    pending = [asyncio.ensure_future(one()) for _ in range(4)]
    await waitFor(lambda: len(results) == 3)
    await asyncio.sleep(0.05)
    assert len(results) == 3
    assert len(terminations) == 3
    await releaseOne(terminations)
    await asyncio.gather(*pending)
    assert all(result.isError is None for result in results)


async def test_ignores_an_invalid_misaka_codemode_max_workers(terminations, monkeypatch):
    monkeypatch.setenv("MISAKA_CODEMODE_MAX_WORKERS", "0")
    results: list = []

    async def one():
        results.append(await run("return 1"))

    pending = [asyncio.ensure_future(one()) for _ in range(3)]
    await waitFor(lambda: len(results) == 2)
    await asyncio.sleep(0.05)
    assert len(results) == 2
    await releaseOne(terminations)
    await asyncio.gather(*pending)
