"""Translated from pi's ``packages/env/src/watch.ts`` (v1.3.0)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Literal, TypedDict

from misaka.chord.context import Context
from misaka.durable.env import FileError, WatchChange, WatchTarget
from misaka.utils.js import (
    AbortController,
    Timeout,
    clearTimeout,
    jsString,
    setTimeout,
    spawn,
)

from .connection import Connection, Json, isConnectionLost
from .errors import toFileError


class RemoteWatchOptions(TypedDict, total=False):
    """How the daemon watches, like ``NodeExecutionEnv``'s ``NodeWatchOptions``. ``mode``: force a mode; by default
    Windows and file systems that do not report remote changes poll. ``pollIntervalMs``: interval between snapshots in
    ``polling`` mode; default 2000 ms. ``maxDirectories``: most directories one watcher covers; default 10,000."""

    mode: Literal["native", "polling"]
    pollIntervalMs: int
    maxDirectories: int


RECONNECT_FIRST_MS = 1000
RECONNECT_MAX_MS = 30_000


class RemoteWatcher:
    """A watcher running in the daemon (Durable's ``NodeFileWatcher`` next to the files). When the connection is lost, it
    is opened again once the connection can be started again, and reports ``overflow``: changes in between were not
    seen."""

    def __init__(
        self,
        connection: Connection,
        targets: list[WatchTarget],
        onChange: Callable[[WatchChange], None],
        options: RemoteWatchOptions,
    ) -> None:
        self._connection = connection
        self._targets = targets
        self._onChange = onChange
        self._options = options
        self._mode: Literal["native", "polling"] = "native"
        self._closed = False
        self._controller: AbortController | None = None
        self._request: asyncio.Future[None] | None = None
        self._wake: Callable[[], None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    @staticmethod
    async def open(
        connection: Connection,
        targets: list[WatchTarget],
        onChange: Callable[[WatchChange], None],
        options: RemoteWatchOptions | None = None,
    ) -> RemoteWatcher:
        """Watch ``targets`` (resolved paths); fails like ``NodeFileWatcher.open`` if coverage cannot be established."""
        watcher = RemoteWatcher(connection, targets, onChange, {} if options is None else options)
        await watcher._start()
        return watcher

    @property
    def mode(self) -> Literal["native", "polling"]:
        return self._mode

    async def close(self, _context: Context) -> None:
        if self._closed:
            return
        self._closed = True
        if self._wake is not None:
            self._wake()
        if self._controller is not None:
            self._controller.abort()
        if self._request is not None:
            await self._request

    def _deliver(self, change: WatchChange) -> None:
        if self._closed:
            return
        try:
            self._onChange(change)
        except Exception:  # noqa: BLE001, S110 - a throwing callback must not stop watching
            pass

    def _start(self) -> asyncio.Future[None]:
        """Open the daemon's watcher; resolves once it reports coverage."""
        loop = asyncio.get_running_loop()
        started: asyncio.Future[None] = loop.create_future()
        ready = False
        controller = AbortController()
        self._controller = controller

        def onEvent(event: Json, _payload: bytes) -> None:
            nonlocal ready
            kind = event.get("kind")
            if kind == "ready":
                ready = True
                self._mode = "polling" if event.get("mode") == "polling" else "native"
                if not started.done():
                    started.set_result(None)
            elif kind == "change":
                if event.get("mode") == "polling":
                    self._mode = "polling"
                if event.get("overflow") is True:
                    self._deliver({"overflow": True})
                else:
                    self._deliver({"paths": event.get("paths")})
            elif kind == "error":
                code: Literal["permission_denied", "invalid"] = (
                    "permission_denied" if event.get("code") == "permission_denied" else "invalid"
                )
                self._deliver({"error": FileError(code, jsString(event.get("message")))})
                self._closed = True

        request = self._connection.request(
            "watch",
            {
                "targets": [
                    {
                        "path": target["path"],
                        "recursive": target.get("recursive") is True,
                        "exclude": {
                            "hidden": (target.get("exclude") or {}).get("hidden") is True,
                            "names": list((target.get("exclude") or {}).get("names") or []),
                        },
                    }
                    for target in self._targets
                ],
                **self._options,
            },
            {"signal": controller.signal, "onEvent": onEvent},
        )
        handled: asyncio.Future[None] = loop.create_future()

        def settled(future: asyncio.Future[Any]) -> None:
            if not handled.done():
                handled.set_result(None)
            if future.cancelled():
                return
            error = future.exception()
            if error is None:
                return
            if not ready:
                if not started.done():
                    started.set_exception(error)
                return
            if self._closed:
                return
            if isConnectionLost(error):
                spawn(self._reconnect(), self._tasks)
                return
            self._deliver({"error": toFileError(error)})
            self._closed = True

        request.add_done_callback(settled)
        self._request = handled
        return started

    async def _reconnect(self) -> None:
        delay = RECONNECT_FIRST_MS
        while not self._closed:
            woken: asyncio.Future[None] = asyncio.get_running_loop().create_future()

            def resolve(woken: asyncio.Future[None] = woken) -> None:
                if not woken.done():
                    woken.set_result(None)

            timer: Timeout = setTimeout(resolve, delay)

            def wake(timer: Timeout = timer, resolve: Callable[[], None] = resolve) -> None:
                clearTimeout(timer)
                resolve()

            self._wake = wake
            await woken
            self._wake = None
            if self._closed:
                return
            try:
                await self._start()
                # Changes while disconnected were not seen.
                self._deliver({"overflow": True})
                return
            except Exception as error:  # noqa: BLE001 - lost connections are retried, others end the watcher
                if isConnectionLost(error):
                    delay = min(delay * 2, RECONNECT_MAX_MS)
                    continue
                self._deliver({"error": toFileError(error)})
                self._closed = True
                return


__all__ = ["RemoteWatchOptions", "RemoteWatcher"]
