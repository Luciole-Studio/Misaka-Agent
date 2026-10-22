"""``createPhotonImages()`` off the event loop: images are prepared in a worker thread that exits when idle.

Translated from pi's ``packages/durable/src/images/node.ts`` (v1.3.0). MISAKA fork: a Python thread stands in for the Node
worker thread, and Pillow for Photon (see ``misaka.durable.images``): nothing is resolved from an installed package. The
thread is a daemon thread, so like an unreferenced Node worker it never keeps the process alive; a pending image keeps
the caller waiting on its future, as upstream's referenced worker does. A processor works from any event loop, as
upstream's does from anywhere in its realm: each request is answered on the loop that made it, and the worker times
itself out when idle, so it exits even when no loop runs any more.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from types import SimpleNamespace
from typing import Any, TypedDict

from misaka.utils.js import _timerDelay

from ..tools.image_processor import ImageLimits, ImageProcessor, PreparedImage
from . import createPhotonImages
from .node_worker import PrepareReply, PrepareRequest, handle


class NodePhotonImagesOptions(TypedDict, total=False):
    worker: bool
    """Prepare images in a worker thread, so a large photo does not block the event loop for the second or two the
    work takes. Default ``True``; ``False`` prepares them on the calling thread."""
    idleMs: float
    """How long the worker stays after its last image before it exits. The next image starts a new one. Default 30 s.
    (Upstream's worker exits to free Photon's WebAssembly memory, which never shrinks while it lives.)"""


async def createNodePhotonImages(options: NodePhotonImagesOptions | None = None) -> ImageProcessor:
    """``createPhotonImages()``, by default in a worker thread that is alive only while it prepares images and for
    ``idleMs`` after. A worker that cannot start falls back to the calling thread."""
    if options is None:
        options = {}
    if options.get("worker") is False:
        return await inProcess()
    idleMs = options["idleMs"] if options.get("idleMs") is not None else 30_000
    thread: PhotonThread | None = None
    # MISAKA fork: the in-process processor itself, not a future of the loop that fell back (upstream keeps the
    # promise), so every loop can use it.
    fallback: ImageProcessor | None = None

    def forget() -> None:
        nonlocal thread
        # Called from the worker that exited; a newer worker started meanwhile stays.
        if thread is not None and thread.exited:
            thread = None

    async def prepare(data: bytes, mimeType: str, limits: ImageLimits) -> PreparedImage | None:
        nonlocal thread, fallback
        if fallback is not None:
            return await fallback.prepare(data, mimeType, limits)
        try:
            while True:
                if thread is None:
                    thread = PhotonThread(idleMs, forget)
                request = thread.prepare(data, mimeType, limits)
                if request is not None:
                    break
                # The worker exited idle just before this request reached it: start the next one, as upstream does
                # once its worker exited.
                thread = None
            return await request
        except WorkerUnavailable:
            # No worker on this platform or build: prepare here from now on.
            fallback = await inProcess()
            return await fallback.prepare(data, mimeType, limits)

    return SimpleNamespace(prepare=prepare)  # type: ignore[return-value]


async def inProcess() -> ImageProcessor:
    return createPhotonImages()


class WorkerUnavailable(Exception):
    """The worker could not start, as opposed to failing on one image."""


class PhotonThread:
    """One worker thread and the requests it has not answered; it exits after ``idleMs`` without requests."""

    def __init__(self, idleMs: float, onExit: Any) -> None:
        self._idleMs = idleMs
        self._onExit = onExit
        # Each request with the loop that waits for it, answered there.
        self._pending: dict[int, tuple[asyncio.Future[PreparedImage | None], asyncio.AbstractEventLoop]] = {}
        self._nextId = 0
        # Whether the worker answered once: a failure before that means it is unavailable.
        self._started = False
        # Set, under the lock, once the worker takes no more requests.
        self._exited = False
        self._lock = threading.Lock()
        self._requests: queue.SimpleQueue[PrepareRequest] = queue.SimpleQueue()
        try:
            self._worker = threading.Thread(target=self._run, name="misaka-durable-images", daemon=True)
            self._worker.start()
        except RuntimeError as error:
            raise WorkerUnavailable("The Photon image worker cannot start") from error

    @property
    def exited(self) -> bool:
        return self._exited

    def prepare(self, data: bytes, mimeType: str, limits: ImageLimits) -> asyncio.Future[PreparedImage | None] | None:
        """The request's future, answered on the calling loop; ``None`` once the worker exited (upstream's processor has
        forgotten an exited worker by then; here the exit can race the call)."""
        loop = asyncio.get_running_loop()
        request: asyncio.Future[PreparedImage | None] = loop.create_future()
        # A copy: the caller keeps its bytes.
        copy = bytes(data)
        with self._lock:
            if self._exited:
                return None
            id = self._nextId
            self._nextId += 1
            self._pending[id] = (request, loop)
            self._requests.put({"id": id, "bytes": copy, "mimeType": mimeType, "limits": dict(limits)})  # type: ignore[typeddict-item]
        return request

    def _run(self) -> None:
        """The worker thread: answer requests until none came for ``idleMs``. MISAKA fork: the idle timeout is the
        worker's own wait, not a timer on a loop that may have closed (upstream's timer terminates the worker)."""
        # As Node's ``setTimeout`` takes the delay.
        idle = _timerDelay(self._idleMs) / 1000
        try:
            while True:
                try:
                    request = self._requests.get(timeout=idle)
                except queue.Empty:
                    with self._lock:
                        # A request may have come between the timeout and the lock.
                        if not self._requests.empty():
                            continue
                        self._exited = True
                    self._onExit()
                    return
                self._message(handle(request))
        except BaseException as error:  # noqa: BLE001 - the thread's end is the caller's to see
            self._fail(error)

    def _message(self, reply: PrepareReply) -> None:
        self._started = True
        with self._lock:
            entry = self._pending.pop(reply["id"], None)
        if entry is None:
            return
        request, loop = entry
        if "error" in reply:
            _settle(request, loop, RuntimeError(reply["error"]))
        else:
            _settle(request, loop, None, reply.get("result"))

    def _fail(self, error: BaseException) -> None:
        failure = error if self._started else WorkerUnavailable("The Photon image worker cannot start")
        if failure is not error:
            failure.__cause__ = error
        with self._lock:
            self._exited = True
            pending = list(self._pending.values())
            self._pending.clear()
        for request, loop in pending:
            _settle(request, loop, failure)
        self._onExit()


def _settle(
    request: asyncio.Future[PreparedImage | None],
    loop: asyncio.AbstractEventLoop,
    error: BaseException | None,
    result: PreparedImage | None = None,
) -> None:
    """Settle ``request`` on its own loop, from the worker thread; nobody waits once that loop closed."""

    def settle() -> None:
        if request.done():
            return
        if error is not None:
            request.set_exception(error)
        else:
            request.set_result(result)

    try:
        loop.call_soon_threadsafe(settle)
    except RuntimeError:
        pass


__all__ = ["NodePhotonImagesOptions", "createNodePhotonImages"]
