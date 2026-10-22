"""Worker thread entry. One worker runs one script inside a fresh QuickJS VM (a separate wasm instance), relays tool
calls and output to the host, and reports the result. The host terminates the worker when the script settles, times
out, or is aborted; the worker exists so that a spinning script never blocks the host thread.

Translated from pi's ``packages/codemode/src/runtime/worker.ts``; the VM itself is driven by ``vm.py``.

MISAKA fork: Python has no ``worker_threads``. ``Worker`` stands in for Node's: it runs ``main`` on a thread of its
own, delivers what the thread posts to the host's event loop as "message", "error" and "exit" events, and queues the
host's messages for the thread, which handles them once ``main`` has returned, as a worker's event loop does. wasm runs
without the GIL, so a spinning script leaves the event loop free. ``terminate()`` cannot stop a thread: the host sets
the interrupt flag first (upstream does too, for Bun), and the thread then ends once the VM returns control, which a
built-in that does not poll the flag can delay (#10748). ``threadExited`` is set from the thread itself as it ends, so
it settles even when the host's event loop has closed by then.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import queue
import threading
from collections.abc import Callable
from typing import Any

from .protocol import WorkerData, WorkerToHostMessage, isHostToWorkerMessage
from .vm import StartVmOptions, VmSession, crashMessage, startVm

# Stack of a worker thread: the wasm stack limit (`quickjs_wasi.MAX_WASM_STACK`) and the host frames below and above
# it (host callbacks run on top of wasm frames). Node's workers default to 4 MB as well.
WORKER_STACK_BYTES = 4 * 1024 * 1024

_stackLock = threading.Lock()
_TERMINATE = object()


class _ParentPort:
    """``parentPort`` inside the worker thread."""

    def __init__(self, worker: Worker) -> None:
        self._worker = worker
        self.listener: Callable[[Any], None] | None = None
        # The worker's VM session, which the thread disposes when it ends; upstream's ends with its worker.
        self.session: VmSession | None = None

    def postMessage(self, message: WorkerToHostMessage) -> None:
        self._worker._emit("message", message)

    def on(self, _event: str, listener: Callable[[Any], None]) -> None:
        self.listener = listener


def crash(parentPort: _ParentPort, error: BaseException) -> None:
    parentPort.postMessage(crashMessage(error))


def main(data: WorkerData, parentPort: _ParentPort) -> None:
    interrupt = data["interrupt"]
    # MISAKA fork: the thread disposes the session when it ends (upstream: the VM lives until the host terminates
    # the worker, so the session is never disposed).
    session = startVm(StartVmOptions(
        data=data,
        wasm=data["wasm"],
        post=parentPort.postMessage,
        shouldInterrupt=interrupt.is_set,
    ))
    parentPort.session = session

    def onMessage(message: Any) -> None:
        if isHostToWorkerMessage(message):
            session.deliver(message)

    parentPort.on("message", onMessage)


class Worker:
    """Node's ``worker_threads.Worker`` running ``main`` with ``workerData``. Construct it on the event loop; listeners
    added with ``on()`` right after construction see every event."""

    def __init__(self, workerData: WorkerData) -> None:
        self._loop = asyncio.get_running_loop()
        self._listeners: dict[str, list[Callable[[Any], None]]] = {}
        self._inbox: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._outbox: list[tuple[str, Any]] = []
        self._outboxLock = threading.Lock()
        self._flushScheduled = False
        self._exited: asyncio.Future[int] = self._loop.create_future()
        # Set by the thread as it ends, after its VM is released: what upstream's `once("exit")` listeners see.
        self.threadExited: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._thread = threading.Thread(target=self._run, args=(workerData,), name="codemode-worker", daemon=True)
        # `threading.stack_size()` applies to every thread started after it, so it is set only around this start.
        with _stackLock:
            previous = threading.stack_size()
            try:
                threading.stack_size(max(previous, WORKER_STACK_BYTES))
            except (ValueError, RuntimeError):  # a platform that cannot size thread stacks keeps its default
                previous = None
            try:
                self._thread.start()
            finally:
                if previous is not None:
                    threading.stack_size(previous)

    def on(self, event: str, listener: Callable[[Any], None]) -> None:
        self._listeners.setdefault(event, []).append(listener)

    def postMessage(self, message: Any) -> None:
        self._inbox.put(message)

    def terminate(self) -> asyncio.Future[int]:
        """Ends the worker once it is idle; the returned future resolves with its exit code. Termination is requested
        at once, as Node's ``terminate()`` does, whether or not the future is awaited. The host sets the interrupt
        flag first."""
        self._inbox.put(_TERMINATE)
        return asyncio.shield(self._exited)

    def _emit(self, event: str, payload: Any) -> None:
        """From the worker thread: deliver ``event`` on the host's event loop. Events queue in order and the loop
        takes them in batches, woken once per batch rather than once per event."""
        with self._outboxLock:
            self._outbox.append((event, payload))
            if self._flushScheduled:
                return
            self._flushScheduled = True
        with contextlib.suppress(RuntimeError):  # the loop is closed: nobody is listening any more
            self._loop.call_soon_threadsafe(self._flush)

    def _flush(self) -> None:
        with self._outboxLock:
            events, self._outbox = self._outbox, []
            self._flushScheduled = False
        for event, payload in events:
            if event == "exit" and not self._exited.done():
                self._exited.set_result(payload)
            for listener in list(self._listeners.get(event, ())):
                listener(payload)

    def _run(self, workerData: WorkerData) -> None:
        parentPort = _ParentPort(self)
        try:
            try:
                main(workerData, parentPort)
            except Exception as error:  # noqa: BLE001 - upstream `main(workerData).catch(crash)`
                crash(parentPort, error)
            while (message := self._inbox.get()) is not _TERMINATE:
                if parentPort.listener is not None:
                    parentPort.listener(message)
        except Exception as error:  # noqa: BLE001 - the worker's "error" event
            self._emit("error", error)
        finally:
            parentPort.listener = None
            try:
                if parentPort.session is not None:
                    parentPort.session.dispose()
            finally:
                self.threadExited.set_result(None)
                # A terminated worker exits with code 1, as Node's does.
                self._emit("exit", 1)


__all__ = ["WORKER_STACK_BYTES", "Worker", "crash", "main"]
