"""pi coding-agent's ``codemode-worker-admission.test.ts`` (#10748): at most two codemode workers per process, counted
until each actually exits.

Upstream mocks ``Worker.prototype.terminate`` to keep terminated workers alive; here each worker's termination
request is held back, which keeps its thread waiting until the test lets it go.
"""

from __future__ import annotations

import asyncio
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


def exitWorker(worker) -> None:
    """Let a held worker exit: the request the real ``terminate()`` sends."""
    worker._inbox.put(codemode_worker._TERMINATE)


async def run(code: str, signal=None):
    return await executeCodemode("worker-admission", {"code": code}, signal, None, None)


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

    blocked = await run("return 3")
    assert blocked.isError is True and blocked.details == {"calls": []}
    assert any(
        "Codemode worker limit reached (2 workers are still running or shutting down)" in text for text in texts(blocked)
    )
    assert len(terminations) == 2

    alreadyAborted = AbortController()
    alreadyAborted.abort()
    aborted = await run("while (true) {}", alreadyAborted.signal)
    assert aborted.isError is True
    assert any("Script aborted:" in text for text in texts(aborted))
    assert len(terminations) == 2

    # Releasing one actual worker, not its result, permits exactly one new execution.
    worker = terminations.pop(0)
    exitWorker(worker)
    await asyncio.wait_for(asyncio.wrap_future(worker.threadExited), 10)
    assert (await run("return 42")).isError is None
    assert len(terminations) == 2


# #10748: timed-out launches resuming after wasm loading must obey the same limit.
async def test_bounds_workers_that_start_after_their_results_already_timed_out(terminations, monkeypatch):
    wasm = await codemode_host.loadQuickJSWasm()
    loading = asyncio.get_running_loop().create_future()
    monkeypatch.setattr(codemode_host, "loadQuickJSWasm", lambda *_args: loading)
    try:
        results = await asyncio.gather(*(run('// @options: {"timeout_ms": 20}\nwhile (true) {}') for _ in range(3)))
        assert all(result.isError for result in results)
        assert len(terminations) == 0
        loading.set_result(wasm)
        await waitFor(lambda: len(terminations) == 2)
        blocked = await run("return 1")
        assert blocked.isError is True
        assert any("Codemode worker limit reached" in text for text in texts(blocked))
        assert len(terminations) == 2
    finally:
        if not loading.done():
            loading.set_result(wasm)


# #10748: concurrent callers must not race past the shared admission limit.
async def test_admits_only_two_workers_from_a_concurrent_burst(terminations):
    results = await asyncio.gather(*(run("return 1") for _ in range(6)))
    assert len([result for result in results if not result.isError]) == 2
    assert len([result for result in results if result.isError]) == 4
    assert len(terminations) == 2
