"""Terminals of a pi-env daemon: a program in a pseudo-terminal, with input, resizing and the foreground job.

Translated from pi's ``packages/env/src/terminal.ts`` (v1.3.0). ``TerminalOptions`` is a ``dict`` with the upstream keys
(``cwd``, ``argv``, ``env``, ``inheritEnv``, ``cols``, ``rows``, ``onData``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, NotRequired, TypedDict

from misaka.utils.js import AbortController, toWellFormed

from .connection import Connection, Json

WRITE_CHUNK = 1024 * 1024
"""Input larger than this goes to the daemon in several writes; frames are at most 16 MiB."""


class TerminalOptions(TypedDict):
    """``cwd``: working directory of the program; it must exist. ``argv``: program and arguments; default the user's
    shell (``SHELL`` in ``env`` or the daemon's environment) as a login shell. ``env``: added to the daemon's environment,
    or the whole environment with ``inheritEnv: False``; set ``TERM`` here. ``onData``: the terminal's raw output, in
    order."""

    cwd: str
    argv: NotRequired[Sequence[str]]
    env: NotRequired[dict[str, str]]
    inheritEnv: NotRequired[bool]
    cols: int
    rows: int
    onData: Callable[[bytes], None]


class TerminalForeground(TypedDict):
    """The foreground job of a terminal: its process group id, and its leader's name if the system tells."""

    pid: int
    name: NotRequired[str]


def _consume(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        future.exception()


class RemoteTerminal:
    """A program running in a pseudo-terminal of a pi-env daemon. It ends when the program exits, ``close()`` kills it,
    or the connection is lost (the daemon then kills it). ``exited``: the exit code; rejects with the daemon's error,
    with code ``aborted`` after ``close()``."""

    def __init__(
        self,
        connection: Connection,
        id: int,
        session: int,
        pid: int,
        exited: asyncio.Future[int],
        abort: AbortController,
    ) -> None:
        self._connection = connection
        self._id = id
        self._session = session
        self.pid = pid
        self.exited = exited
        self._abort = abort

    def write(self, data: bytes | str) -> None:
        """Send input; once the terminal has ended, input is dropped."""
        # `new TextEncoder().encode(data)`: a lone surrogate becomes one U+FFFD.
        encoded = toWellFormed(data).encode("utf-8") if isinstance(data, str) else bytes(data)
        for offset in range(0, len(encoded), WRITE_CHUNK):
            self._request("ptyWrite", {}, encoded[offset : offset + WRITE_CHUNK])

    def resize(self, cols: int, rows: int) -> None:
        """Change the size; the program receives ``SIGWINCH``."""
        self._request("ptyResize", {"cols": cols, "rows": rows})

    def close(self) -> None:
        """Kill the program and everything in its session."""
        self._abort.abort()

    async def foreground(self) -> TerminalForeground | None:
        """The job in the terminal's foreground when it is not the program itself: what a shell runs, such as an
        editor. ``None`` while the program is in front (a shell at its prompt). Rejects once the terminal has ended."""
        reply = await self._connection.request("ptyForeground", {"target": self._id}, {"session": self._session})
        foreground = reply["json"].get("foreground")
        if not foreground:
            return None
        return {"pid": foreground["pid"], **({"name": foreground["name"]} if foreground.get("name") else {})}  # type: ignore[typeddict-item]

    def _request(self, op: str, json: Json, payload: bytes | None = None) -> None:
        options: dict[str, Any] = {"session": self._session, **({} if payload is None else {"payload": payload})}
        request = asyncio.ensure_future(self._connection.request(op, {**json, "target": self._id}, options))  # type: ignore[arg-type]
        # A terminal that ended or a lost connection: `exited` reports it.
        request.add_done_callback(_consume)


def openTerminal(connection: Connection, options: TerminalOptions) -> asyncio.Future[RemoteTerminal]:
    """Start a program in a new pseudo-terminal of the daemon behind ``connection``."""
    loop = asyncio.get_running_loop()
    abort = AbortController()
    started: asyncio.Future[RemoteTerminal] = loop.create_future()
    request: dict[str, int] = {}

    def onStart(id: int, session: int) -> None:
        request["id"] = id
        request["session"] = session

    def onEvent(json: Json, payload: bytes) -> None:
        if json.get("kind") == "output":
            options["onData"](payload)
        elif json.get("kind") == "started" and "id" in request and not started.done():
            started.set_result(
                RemoteTerminal(connection, request["id"], request["session"], int(json["pid"]), exited, abort)
            )

    async def run() -> int:
        reply = await connection.request(
            "pty",
            {
                "cwd": options["cwd"],
                **({} if options.get("argv") is None else {"argv": list(options["argv"])}),
                **({} if options.get("env") is None else {"env": options["env"]}),
                **({} if options.get("inheritEnv") is None else {"inheritEnv": options["inheritEnv"]}),
                "cols": options["cols"],
                "rows": options["rows"],
            },
            {"signal": abort.signal, "onStart": onStart, "onEvent": onEvent},
        )
        return int(reply["json"]["exitCode"])

    exited: asyncio.Future[int] = asyncio.ensure_future(run())

    # Failing before it started rejects the open; afterwards, `exited` carries it.
    def failed(done: asyncio.Future[int]) -> None:
        if done.cancelled():
            if not started.done():
                started.cancel()
            return
        error = done.exception()
        if error is not None and not started.done():
            started.set_exception(error)

    exited.add_done_callback(failed)
    return started


__all__ = ["WRITE_CHUNK", "RemoteTerminal", "TerminalForeground", "TerminalOptions", "openTerminal"]
