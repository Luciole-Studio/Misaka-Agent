"""Translated from pi's ``packages/codemode/src/runtime/host.ts``.

MISAKA fork: the worker is a thread (``worker.py``), so there is no worker file to resolve and no ``workerUrl``
option.
"""

from __future__ import annotations

import inspect
import threading

from ..types import CodemodeSandboxOptions
from ..wasm import CodemodeWasmModule, loadQuickJSWasm
from .execution import (
    CodemodeSandboxBase,
    VmChannel,
    VmEvents,
    VmLauncher,
    errorMessage,
)
from .protocol import HostToWorkerMessage, VmStartData, WorkerData
from .worker import Worker


def workerLauncher(options: CodemodeSandboxOptions) -> VmLauncher:
    """Each execution gets its own worker thread and QuickJS VM. A fresh worker per run keeps termination simple: a
    runaway script, including one that only spins the microtask queue, is stopped and cannot poison a later run.
    Before terminating, the host sets an interrupt flag that the VM polls, because a thread spinning in wasm cannot be
    stopped from outside. Stopping requests termination without waiting for exit: some built-ins do not poll the
    flag, so a worker can outlive the script's result (MISAKA fork: upstream says so of Bun; a thread is always
    in that situation)."""

    async def launch(data: VmStartData, events: VmEvents) -> VmChannel:
        try:
            wasm = options.get("wasm")
            pending = loadQuickJSWasm() if wasm is None else wasm
            module: CodemodeWasmModule = await pending if inspect.isawaitable(pending) else pending
        except Exception as error:
            raise RuntimeError(f"Failed to load QuickJS: {errorMessage(error)}") from error
        # pi: a `SharedArrayBuffer` of one Int32.
        interrupt = threading.Event()
        workerData: WorkerData = {**data, "wasm": module, "interrupt": interrupt}  # type: ignore[typeddict-item]
        acquireWorker = options.get("acquireWorker")
        releaseWorker = acquireWorker() if acquireWorker is not None else None
        try:
            worker = Worker(workerData)
        except Exception as error:
            if releaseWorker is not None:
                releaseWorker()
            raise RuntimeError(f"Failed to start worker: {errorMessage(error)}") from error
        if releaseWorker is not None:
            # MISAKA fork: released from the worker thread as it ends (upstream: `worker.once("exit", releaseWorker)`).
            # The "exit" event reaches the host's event loop, which may have closed while a cancelled thread is still
            # inside a built-in; the reservation would then never be returned.
            worker.threadExited.add_done_callback(lambda _future: releaseWorker())
        worker.on("message", events.message)
        worker.on("error", lambda error: events.failure({
            "kind": "sandbox",
            "name": type(error).__name__,
            "message": errorMessage(error),
        }))
        worker.on("exit", lambda code: events.failure({
            "kind": "sandbox",
            "message": f"Worker exited with code {code} before the script settled",
        }))

        async def stop() -> None:
            interrupt.set()
            # A non-interruptible built-in can delay the thread's end until it returns (#10748).
            # Do not hold up the result while that worker exits.
            worker.terminate()
            # Upstream unrefs the worker here; a daemon thread never keeps the process alive.

        onWorkerStarted = options.get("onWorkerStarted")
        try:
            if onWorkerStarted is not None:
                onWorkerStarted(worker.threadExited)
        except Exception:
            await stop()
            raise

        def post(message: HostToWorkerMessage) -> None:
            worker.postMessage(message)

        return VmChannel(post=post, stop=stop)

    return launch


class CodemodeSandbox(CodemodeSandboxBase):
    """Runs JavaScript in a QuickJS VM (a separate wasm instance) inside a worker thread. The worker keeps script
    execution off the event loop's thread: QuickJS runs synchronously, so a spinning script there would block the
    event loop."""

    def __init__(self, options: CodemodeSandboxOptions | None = None) -> None:
        options = options or {}
        super().__init__(options, workerLauncher(options))


__all__ = ["CodemodeSandbox", "Worker", "workerLauncher"]
