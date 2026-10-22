"""pi codemode's ``worker-lifetime.test.ts`` (#10748), and the misaka thread-worker adaptations.

Upstream mocks ``Worker.prototype.terminate`` so a worker outlives its result; here the worker's termination request is
held back the same way, which keeps the thread waiting in its inbox until the test lets it go.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from misaka.codemode import CodemodeSandbox, CodemodeTool
from misaka.codemode.runtime import host as codemode_host
from misaka.codemode.runtime import worker as codemode_worker
from misaka.utils.js import AbortController

REPO = Path(__file__).resolve().parent.parent


def matches(actual, expected) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and matches(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(matches(a, e) for a, e in zip(actual, expected, strict=True))
    if callable(expected):
        return expected(actual)
    return actual == expected


class Calls:
    """``vi.fn()``: counts its calls, optionally returning or raising."""

    def __init__(self, result=None, error: BaseException | None = None) -> None:
        self.count = 0
        self.args: list[tuple] = []
        self._result = result
        self._error = error

    def __call__(self, *args):
        self.count += 1
        self.args.append(args)
        if self._error is not None:
            raise self._error
        return self._result() if callable(self._result) else self._result


@pytest.fixture
def heldTermination(monkeypatch):
    """Hold back every worker's termination: ``terminate()`` records the worker and returns, and the thread stays in
    its inbox until ``release()`` sends the real request."""
    held: list[codemode_worker.Worker] = []
    real = codemode_worker.Worker.terminate

    def terminate(self):
        held.append(self)
        return asyncio.get_running_loop().create_future()

    monkeypatch.setattr(codemode_worker.Worker, "terminate", terminate)

    class Held:
        workers = held

        @staticmethod
        def release(worker) -> None:
            real(worker)

        @staticmethod
        def releaseAll() -> None:
            for worker in held:
                real(worker)

    yield Held
    Held.releaseAll()


# #10748: reject admission before creating a worker, not by terminating an excess worker.
async def test_does_not_start_a_worker_when_admission_is_denied():
    onWorkerStarted = Calls()
    acquireWorker = Calls(error=RuntimeError("worker capacity exhausted"))
    sandbox = CodemodeSandbox({"acquireWorker": acquireWorker, "onWorkerStarted": onWorkerStarted})
    try:
        result = await sandbox.execute("while (true) {}")
        assert matches(result, {"ok": False, "error": {"kind": "sandbox", "message": "worker capacity exhausted"}})
        assert acquireWorker.count == 1
        assert onWorkerStarted.count == 0
    finally:
        await sandbox.close()


# #10748: a failed Worker constructor must return its reserved capacity.
async def test_releases_admission_when_worker_construction_fails(monkeypatch):
    # MISAKA fork: there is no worker URL to get wrong; the thread is what fails to start.
    def failing(_workerData):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(codemode_host, "Worker", failing)
    releaseWorker = Calls()
    acquireWorker = Calls(result=lambda: releaseWorker)
    sandbox = CodemodeSandbox({"acquireWorker": acquireWorker})
    try:
        result = await sandbox.execute("return 1")
        assert matches(result, {"ok": False, "error": {"kind": "sandbox", "message": lambda m: "Failed to start worker:" in m}})
        assert acquireWorker.count == 1
        assert releaseWorker.count == 1
    finally:
        await sandbox.close()


# #10748: loading failures must not reserve worker capacity.
async def test_does_not_acquire_admission_when_wasm_loading_fails():
    acquireWorker = Calls(result=lambda: Calls())
    failed = asyncio.get_running_loop().create_future()
    failed.set_exception(RuntimeError("wasm unavailable"))
    sandbox = CodemodeSandbox({"acquireWorker": acquireWorker, "wasm": failed})
    try:
        result = await sandbox.execute("return 1")
        assert matches(result, {"ok": False, "error": {"kind": "sandbox", "message": "Failed to load QuickJS: wasm unavailable"}})
        assert acquireWorker.count == 0
    finally:
        await sandbox.close()


# #10748: a non-interruptible built-in can leave termination pending.
@pytest.mark.parametrize("how", ["abort", "timeout", "close"])
async def test_settles_without_waiting_for_worker_exit(how, heldTermination):
    called = asyncio.Event()
    toolSignal = None
    exited: list[concurrent.futures.Future[None]] = []

    def hang(_args, context):
        nonlocal toolSignal
        toolSignal = context["signal"]
        called.set()
        return asyncio.get_running_loop().create_future()

    sandbox = CodemodeSandbox({
        "timeoutMs": 100 if how == "timeout" else 10_000,
        "onWorkerStarted": exited.append,
        "tools": [CodemodeTool(name="hang", execute=hang)],
    })
    controller = AbortController()
    pending = sandbox.execute('text("partial"); await tools.hang()', {"signal": controller.signal})
    try:
        await asyncio.wait_for(called.wait(), 10)
        if how == "abort":
            controller.abort(RuntimeError("cancelled"))
        closing = asyncio.ensure_future(sandbox.close()) if how == "close" else None
        result = await asyncio.wait_for(asyncio.shield(pending), 5)
        assert matches(result, {
            "ok": False,
            "error": {"kind": "timeout" if how == "timeout" else "aborted"},
            "output": [{"type": "text", "text": "partial"}],
            "calls": [{"name": "hang", "status": "cancelled"}],
        })
        assert toolSignal is not None and toolSignal.aborted
        assert len(heldTermination.workers) == 1
        assert len(exited) == 1 and not exited[0].done()
        if closing is not None:
            await asyncio.wait_for(closing, 1)
        # Neither close() nor late worker messages may replace the decided outcome or wait for exit.
        await asyncio.wait_for(sandbox.close(), 1)
        heldTermination.workers[0]._emit("message", {"type": "done", "ok": True, "value": "42", "writes": "[]"})
        await asyncio.sleep(0.05)
        assert pending.result() is result
        assert len(heldTermination.workers) == 1
    finally:
        heldTermination.releaseAll()
        await sandbox.close()
    await asyncio.wait_for(asyncio.wrap_future(exited[0]), 10)


# #10748: exit accounting applies to successful executions and host-controlled runs too.
async def test_reports_the_actual_exit_of_execute_and_start_workers():
    exits: list[concurrent.futures.Future[None]] = []
    releaseWorker = Calls()
    acquireWorker = Calls(result=lambda: releaseWorker)
    sandbox = CodemodeSandbox({"acquireWorker": acquireWorker, "onWorkerStarted": exits.append})
    try:
        assert matches(await sandbox.execute("return 1"), {"ok": True, "value": 1})
        run = sandbox.start("return 2", {"startedAt": 0, "seed": 0, "onCall": lambda _call: None})
        assert matches(await run.result, {"ok": True, "value": 2})
        assert run.finished is True
        assert len(exits) == 2
        await asyncio.wait_for(asyncio.gather(*(asyncio.wrap_future(exit) for exit in exits)), 10)
        assert acquireWorker.count == 2
        assert releaseWorker.count == 2
    finally:
        await sandbox.close()


# #10748: a failing lifetime hook must not leak the worker it was handed.
async def test_requests_termination_when_on_worker_started_throws():
    exited: list[concurrent.futures.Future[None]] = []
    releaseWorker = Calls()

    def onWorkerStarted(future):
        exited.append(future)
        raise RuntimeError("lifetime hook failed")

    sandbox = CodemodeSandbox({"acquireWorker": lambda: releaseWorker, "onWorkerStarted": onWorkerStarted})
    try:
        result = await sandbox.execute("while (true) {}")
        assert matches(result, {"ok": False, "error": {"kind": "sandbox", "message": "lifetime hook failed"}})
        assert len(exited) == 1
        await asyncio.wait_for(asyncio.wrap_future(exited[0]), 10)
        assert releaseWorker.count == 1
    finally:
        await sandbox.close()


# -- misaka adaptations ------------------------------------------------------------------------------------------


# The worker is a thread: the reservation comes back from the thread itself, also after the event loop that started
# the worker has closed (a session in a helper loop that ended while a cancelled script was still in a built-in).
def test_releases_admission_from_the_worker_thread_after_its_event_loop_closed():
    released = threading.Event()
    exits: list[concurrent.futures.Future[None]] = []

    async def run():
        sandbox = CodemodeSandbox({"acquireWorker": lambda: released.set, "onWorkerStarted": exits.append})
        controller = AbortController()
        pending = sandbox.execute("return 'a'.repeat(30000).indexOf('a'.repeat(3000) + 'b')", {"signal": controller.signal})
        await asyncio.sleep(0.3)
        controller.abort()
        return await pending

    result = asyncio.run(run())
    assert matches(result, {"ok": False, "error": {"kind": "aborted"}})
    assert released.wait(30)
    assert exits[0].result(timeout=1) is None


# The VM's host functions live in one process-wide wasmtime-py table: creating and closing VMs on several threads at
# once must not corrupt it (a result no longer waits for its worker, so this overlap is the normal case).
def test_vms_can_be_created_and_released_on_several_threads_at_once():
    from misaka.codemode import loadQuickJSWasm, quickjs_wasi

    module = asyncio.run(_load(loadQuickJSWasm))
    errors: list[BaseException] = []

    def churn():
        try:
            for _ in range(8):
                vm = quickjs_wasi.QuickJS.create(wasm=module)
                assert vm.evalCode("1 + 1").toNumber() == 2
                vm.dispose()
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    threads = [threading.Thread(target=churn) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert errors == []


async def _load(loadQuickJSWasm):
    return await loadQuickJSWasm()


FIXTURE = textwrap.dedent("""
    import asyncio, atexit, json, sys, time
    from misaka.codemode import CodemodeSandbox, CodemodeTool
    from misaka.utils.js import AbortController

    how = sys.argv[1]
    started = time.perf_counter()
    atexit.register(lambda: print(json.dumps({"type": "exit", "elapsedMs": (time.perf_counter() - started) * 1000}), flush=True))

    async def main():
        controller = AbortController()
        loop = asyncio.get_running_loop()
        sandbox = None

        def ready(_args, _context):
            if how == "abort":
                loop.call_later(0.1, controller.abort)
            elif how == "close":
                loop.call_later(0.1, lambda: asyncio.ensure_future(sandbox.close()))

        sandbox = CodemodeSandbox({"memoryLimitBytes": 256 * 1024 * 1024, "tools": [CodemodeTool(name="ready", execute=ready)]})
        # Measured from the script's start, as upstream's fixture measures from its own.
        result = await sandbox.execute(
            "await tools.ready(); return 'a'.repeat(300000).indexOf('a'.repeat(10000) + 'b')",
            {"signal": controller.signal, "timeoutMs": 300 if how == "timeout" else float("inf")},
        )
        if result["ok"]:
            raise RuntimeError("The script completed instead of being cancelled")
        await sandbox.close()
        print(json.dumps({"type": "result", "kind": result["error"]["kind"], "elapsedMs": (time.perf_counter() - started) * 1000}), flush=True)

    asyncio.run(main())
""")


# #10748: prompt cancellation and prompt process exit with a real worker that is stuck in a built-in for ~10 s.
@pytest.mark.parametrize("how", ["abort", "timeout", "close"])
def test_exits_promptly_with_a_real_worker(how, tmp_path):
    script = tmp_path / "worker_process_exit.py"
    script.write_text(FIXTURE)
    # Imports are not timed; the fixture's clock starts once the codemode package is loaded.
    completed = subprocess.run(
        [sys.executable, str(script), how], cwd=REPO, capture_output=True, text=True, timeout=20, check=False
    )
    lines = completed.stdout.strip().splitlines()
    assert len(lines) == 2, (completed.stdout, completed.stderr)
    result, exit = json.loads(lines[0]), json.loads(lines[1])
    assert matches(result, {"type": "result", "kind": "timeout" if how == "timeout" else "aborted"})
    assert exit["type"] == "exit"
    assert result["elapsedMs"] < 1000
    assert exit["elapsedMs"] - result["elapsedMs"] < 1000


def test_terminate_requests_termination_without_being_awaited():
    # Node's `terminate()` starts termination when it is called; the returned future is optional.
    async def run():
        exits: list[concurrent.futures.Future[None]] = []
        sandbox = CodemodeSandbox({"onWorkerStarted": exits.append})
        started = time.perf_counter()
        assert matches(await sandbox.execute("return 1"), {"ok": True, "value": 1})
        await asyncio.wait_for(asyncio.wrap_future(exits[0]), 10)
        return time.perf_counter() - started

    assert asyncio.run(run()) < 10


# MISAKA fork: a VM that fails to start (QuickJS refuses to initialize, the instance cannot be made, a wasm allocation
# fails) closes its store at once. Its host functions keep it alive in the process-wide table, so nothing else would.
@pytest.mark.parametrize("failure", ["qjs_init", "instantiate"])
def test_a_vm_that_fails_to_start_releases_its_store(monkeypatch, failure):
    import wasmtime
    import wasmtime._func as wasmtime_func

    from misaka.codemode import loadQuickJSWasm, quickjs_wasi

    module = asyncio.run(_load(loadQuickJSWasm))
    freed: list[bool] = []
    deallocate = wasmtime_func.FUNCTIONS.deallocate

    def counting(index):
        freed.append(quickjs_wasi._hostFunctionsLock.locked())
        return deallocate(index)

    monkeypatch.setattr(wasmtime_func.FUNCTIONS, "deallocate", counting)
    if failure == "qjs_init":
        exports = quickjs_wasi._Exports.__getattr__
        monkeypatch.setattr(
            quickjs_wasi._Exports, "__getattr__",
            lambda self, name: (lambda *_args: 1) if name == "qjs_init" else exports(self, name),
        )
        expected = "Failed to initialize QuickJS runtime"
    else:
        def refuse(*_args):
            raise wasmtime.WasmtimeError("instantiation failed")

        monkeypatch.setattr(wasmtime.Linker, "instantiate", refuse)
        expected = "instantiation failed"

    with pytest.raises(Exception, match=expected):
        quickjs_wasi.QuickJS.create(wasm=module)
    # Every host function was freed, under the table's lock, without waiting for a garbage collection.
    assert freed and all(freed)
