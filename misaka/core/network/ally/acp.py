"""Agent Client Protocol, client side: misaka driving another agent over its stdio.

Newline-delimited JSON-RPC 2.0 in both directions, ``protocolVersion`` 1. misaka starts the
agent, opens a session (``session/new`` or ``session/load``), sends prompts, and reads the
agent's ``session/update`` notifications as the turn goes. The agent may ask the client
things; every such request is answered -- one left hanging stalls the agent, and codex's
adapter answers an error on a permission request by cancelling the whole turn (raven,
measured). misaka lends the agent no file system and no terminal: it uses its own.

The schema has no way to put text into a turn that is running: ``session/prompt`` is the only
door, and a second prompt on a busy session is refused. What arrives mid-turn waits for the
turn to end (see ``card``).
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import itertools
import json
from collections import deque

from misaka.core.platform import processes
from misaka.utils.streams import STREAM_LIMIT

PROTOCOL_VERSION = 1
CLIENT_CAPABILITIES = {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603
STDERR_TAIL_CHARS = 8000


class AcpError(RuntimeError):
    """The conversation with the agent failed."""


class AcpRemoteError(AcpError):
    """The agent answered a request with an error."""

    def __init__(self, error):
        error = error if isinstance(error, dict) else {}
        self.code = error.get("code")
        self.data = error.get("data")
        super().__init__(str(error.get("message") or "error") + (f" ({self.data})" if self.data else ""))


class AcpClosed(AcpError):
    """The agent's process is gone."""


class AcpClient:
    """One agent process and the JSON-RPC conversation with it.

    ``on_update(params)`` receives each ``session/update`` in order. ``on_request(method,
    params)`` answers what the agent asks; it raises ``NotImplementedError`` for a method this
    client does not serve, which goes back as "method not found" rather than as silence.
    """

    def __init__(self, argv, *, cwd, env, on_update, on_request, detach=False):
        self.argv, self.cwd, self.env = list(argv), cwd, env
        self.on_update, self.on_request = on_update, on_request
        self.detach = detach
        self.proc = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._stderr: deque[str] = deque()
        self._stderr_size = 0
        self._tasks: list[asyncio.Task] = []
        self._answers: set[asyncio.Task] = set()
        self.closed = asyncio.Event()

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, cwd=self.cwd, env=self.env, limit=STREAM_LIMIT, start_new_session=self.detach,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self._tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._read_stderr())]

    @property
    def stderr_tail(self):
        return "".join(self._stderr)[-STDERR_TAIL_CHARS:].strip()

    async def request(self, method, params, *, timeout=None):
        if self.closed.is_set():
            raise AcpClosed(self._gone())
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method, params):
        if not self.closed.is_set():
            await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, message):
        line = (json.dumps(message, ensure_ascii=False) + "\n").encode()
        async with self._write_lock:
            try:
                self.proc.stdin.write(line)
                await self.proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                raise AcpClosed(self._gone()) from error

    def _gone(self):
        tail = self.stderr_tail
        return "the agent's process exited" + (f": {tail}" if tail else "")

    async def _read(self):
        try:
            while True:
                try:
                    line = await self.proc.stdout.readline()
                except ValueError:
                    self._stderr.append(f"\n(misaka: the agent sent a line over {STREAM_LIMIT} bytes)")
                    break
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                method = message.get("method")
                if method is None:
                    future = self._pending.get(message.get("id"))
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(AcpRemoteError(message["error"]))
                        else:
                            future.set_result(message.get("result"))
                elif "id" in message:
                    task = asyncio.create_task(self._answer(message))
                    self._answers.add(task)
                    task.add_done_callback(self._answers.discard)
                elif method == "session/update":
                    result = self.on_update(message.get("params") or {})
                    if inspect.isawaitable(result):
                        await result
        finally:
            await self._drain_stderr()
            self.closed.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(AcpClosed(self._gone()))

    async def _answer(self, message):
        try:
            result = await self.on_request(message["method"], message.get("params") or {})
            reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        except NotImplementedError:
            reply = {"jsonrpc": "2.0", "id": message["id"],
                     "error": {"code": METHOD_NOT_FOUND, "message": f"{message['method']} is not served"}}
        except Exception as error:  # noqa: BLE001 - an answer, even an error, keeps the agent moving
            reply = {"jsonrpc": "2.0", "id": message["id"],
                     "error": {"code": INTERNAL_ERROR, "message": str(error)[:500]}}
        with contextlib.suppress(AcpClosed):
            await self._send(reply)

    async def _read_stderr(self):
        while True:
            chunk = await self.proc.stderr.read(4096)
            if not chunk:
                return
            text = chunk.decode("utf-8", errors="replace")
            self._stderr.append(text)
            self._stderr_size += len(text)
            while self._stderr_size > STDERR_TAIL_CHARS and len(self._stderr) > 1:
                self._stderr_size -= len(self._stderr.popleft())

    async def _drain_stderr(self):
        if len(self._tasks) > 1:
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(self._tasks[1]), 1)

    async def close(self):
        """End the agent and everything it started."""
        if self.proc is None:
            return
        if self.proc.returncode is None:
            with contextlib.suppress(Exception):
                self.proc.stdin.close()
            await asyncio.to_thread(processes.terminate, self.proc.pid, reap_root=False)
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()
        for task in [*self._tasks, *self._answers]:
            task.cancel()
        await asyncio.gather(*self._tasks, *self._answers, return_exceptions=True)
        self.closed.set()


def choose_permission(options):
    """The answer to ``session/request_permission`` for an unattended ally: an option the agent
    offered, chosen by its ``kind`` (the protocol's words), never by its id (the agent's own)."""
    for kind in ("allow_always", "allow_once"):
        chosen = next((o for o in options or [] if isinstance(o, dict) and o.get("kind") == kind), None)
        if chosen is not None:
            return {"outcome": {"outcome": "selected", "optionId": chosen.get("optionId")}}
    return {"outcome": {"outcome": "cancelled"}}
