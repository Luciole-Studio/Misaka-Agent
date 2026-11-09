"""Translated from pi's ``packages/coding-agent/src/utils/child-process.ts`` (v1.1.0).

MISAKA fork: Node's ``ChildProcess`` events are an asyncio subprocess protocol here. ``spawn_child_process`` is
Node's ``spawn`` with piped stdout/stderr, whose chunks go to ``on_data``; ``waitForChildProcess`` is upstream's
wait, run by the protocol (exit, then the pipes' end or ``EXIT_STDIO_GRACE_MS`` of quiet), and a consumer still
draining what it read can hold the pipes open (``is_draining``, as durable's ``waitForChildProcess(child,
spillIsDraining)``). ``spawnProcess``/``spawnProcessSync`` are not ported: their callers (the package manager and the
npm prefix probe) are not either.
"""

from __future__ import annotations

import asyncio
import select
import subprocess
import sys
from collections.abc import Callable, Mapping
from typing import Any

EXIT_STDIO_GRACE_MS = 100


class _ChildProcessProtocol(asyncio.SubprocessProtocol):
    def __init__(
        self,
        on_data: Callable[[int, bytes], None],
        is_draining: Callable[[], bool] | None = None,
        on_spawn: Callable[[ChildProcess], None] | None = None,
    ) -> None:
        self._loop = asyncio.get_running_loop()
        self._on_data = on_data
        self._on_spawn = on_spawn
        self.child: ChildProcess | None = None
        self._transport: asyncio.SubprocessTransport | None = None
        self._open_output_pipes = {1, 2}
        self._exited = False
        self._returncode: int | None = None
        self._idle_handle: asyncio.TimerHandle | None = None
        self._error: BaseException | None = None
        self._settled = False
        # Asked when the post-exit grace period ends: true keeps the pipes open another period (a consumer is still
        # draining what it read).
        self.is_draining: Callable[[], bool] | None = is_draining
        self.done: asyncio.Future[int | None] = self._loop.create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]
        # Before any data or exit callback: asyncio calls `connection_made` first.
        self.child = ChildProcess(transport, self)  # type: ignore[arg-type]
        if self._on_spawn is not None:
            self._on_spawn(self.child)

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        if self._settled:
            return
        if self._error is None:
            try:
                self._on_data(fd, data)
            except BaseException as error:  # noqa: BLE001 - wait() must report callback failures
                self._error = error
        if self._exited:
            self.armIdleTimer()

    def pipe_connection_lost(self, fd: int, exc: Exception | None) -> None:
        if fd not in self._open_output_pipes:
            return
        self._open_output_pipes.discard(fd)
        if self._exited and not self._open_output_pipes:
            self._finish()

    def process_exited(self) -> None:
        self._exited = True
        if self._transport is not None:
            self._returncode = self._transport.get_returncode()
        if not self._open_output_pipes:
            self._finish()
        else:
            self.armIdleTimer()

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None and self._error is None:
            self._error = exc
        if self._exited:
            self._finish()

    def armIdleTimer(self) -> None:
        if self._settled:
            return
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        self._idle_handle = self._loop.call_later(
            EXIT_STDIO_GRACE_MS / 1000,
            self._idle_expired,
        )

    def _idle_expired(self) -> None:
        self._idle_handle = None
        if (self.is_draining is not None and self.is_draining()) or self._unread_output():
            self.armIdleTimer()
        else:
            self._finish()

    def _unread_output(self) -> bool:
        """Whether a pipe that is being read holds bytes not read yet. MISAKA fork: Node's paused stream keeps reading
        from the pipe into its buffer and, on ``resume()``, emits what it holds on the next tick, before any timer, so
        a chunk that came while paused re-arms the grace (`onData`) once the stream resumes. ``pause_reading()`` leaves
        those bytes in the pipe, and a reader resumed in the loop turn where the grace ends would only read them on the
        next poll; they count as output here, so the grace goes on and they are read. A pipe still paused counts
        nothing, as a paused Node stream emits nothing. ``poll``, as env's pty; ``select`` only where Python has no
        ``poll``, since it raises ``ValueError`` for a descriptor of ``FD_SETSIZE`` or more."""
        if self._transport is None or sys.platform == "win32":
            return False
        descriptors = []
        for fd in (1, 2):
            pipe: Any = self._transport.get_pipe_transport(fd)
            if pipe is not None and not pipe.is_closing() and pipe.is_reading():
                handle = pipe.get_extra_info("pipe")
                if handle is not None:
                    descriptors.append(handle.fileno())
        if len(descriptors) == 0:
            return False
        try:
            if hasattr(select, "poll"):
                poller = select.poll()
                for descriptor in descriptors:
                    poller.register(descriptor, select.POLLIN)
                return len(poller.poll(0)) > 0
            return len(select.select(descriptors, [], [], 0)[0]) > 0
        except (OSError, ValueError):
            return False

    def _finish(self) -> None:
        if self._settled:
            return
        self._settled = True
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        if self._transport is not None:
            self._transport.close()
        if self._error is not None:
            self.done.set_exception(self._error)
        else:
            self.done.set_result(self._returncode)

    def close(self) -> None:
        self._finish()


class ChildProcess:
    """A subprocess whose exit is independent from inherited stdout/stderr pipes."""

    def __init__(
        self,
        transport: asyncio.SubprocessTransport,
        protocol: _ChildProcessProtocol,
    ) -> None:
        self._transport = transport
        self._protocol = protocol

    @property
    def pid(self) -> int | None:
        return self._transport.get_pid()

    @property
    def returncode(self) -> int | None:
        return self._transport.get_returncode()

    def write_stdin(self, data: bytes) -> None:
        pipe: Any = self._transport.get_pipe_transport(0)
        if pipe is not None:
            pipe.write(data)

    def close_stdin(self) -> None:
        pipe = self._transport.get_pipe_transport(0)
        if pipe is not None:
            pipe.close()

    def terminate(self) -> None:
        self._transport.terminate()

    def kill(self) -> None:
        self._transport.kill()

    def close(self) -> None:
        self._protocol.close()

    def set_draining_check(self, is_draining: Callable[[], bool] | None) -> None:
        """Racy: by the time ``spawn_child_process`` returns, the child may have exited and armed the post-exit grace,
        which can end before this is called. Pass ``is_draining`` to ``spawn_child_process`` instead."""
        self._protocol.is_draining = is_draining

    def pause_output(self) -> None:
        """Stop reading stdout and stderr until ``resume_output()``; the child blocks once its pipes fill."""
        for fd in (1, 2):
            pipe: Any = self._transport.get_pipe_transport(fd)
            if pipe is not None and not pipe.is_closing():
                pipe.pause_reading()

    def resume_output(self) -> None:
        for fd in (1, 2):
            pipe: Any = self._transport.get_pipe_transport(fd)
            if pipe is not None and not pipe.is_closing():
                pipe.resume_reading()

    async def wait(self) -> int | None:
        return await asyncio.shield(self._protocol.done)


async def spawn_child_process(
    program: str,
    *args: str,
    cwd: str,
    env: Mapping[str, str] | None = None,
    stdin: int = subprocess.DEVNULL,
    start_new_session: bool = False,
    creationflags: int = 0,
    on_data: Callable[[int, bytes], None] | None = None,
    is_draining: Callable[[], bool] | None = None,
    on_spawn: Callable[[ChildProcess], None] | None = None,
) -> ChildProcess:
    """Start ``program``. ``is_draining`` is asked when the post-exit grace ends (true holds the pipes open another
    period); it is in place before the child can exit, as upstream passes it to ``waitForChildProcess`` right after
    its synchronous ``spawn``. ``on_spawn`` gets the ``ChildProcess`` once the process exists, before its first chunk
    or its exit is delivered (this function returns a loop turn later)."""
    callback = on_data if on_data is not None else (lambda _fd, _data: None)
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.subprocess_exec(
        lambda: _ChildProcessProtocol(callback, is_draining, on_spawn),
        program,
        *args,
        cwd=cwd,
        env=env,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=start_new_session,
        creationflags=creationflags,
    )
    return protocol.child if protocol.child is not None else ChildProcess(transport, protocol)


async def waitForChildProcess(child: ChildProcess) -> int | None:
    """Wait for a child process to terminate without hanging on inherited stdio handles.

    A short-lived child can `exit` while a detached descendant keeps its stdout/stderr
    pipe open. We must not resolve and destroy the streams on a fixed deadline measured
    from `exit`, or output still being written past that deadline is silently lost
    (earendil-works/pi#5303). Instead, after `exit` we wait for the pipes to fall idle:
    the grace timer is re-armed on every chunk, so an actively writing descendant keeps
    us reading, while a quiet inherited handle (e.g. a Windows daemonized descendant
    that never lets `close` fire) still releases us after the grace elapses.
    """
    return await child.wait()


__all__ = [
    "ChildProcess",
    "spawn_child_process",
    "waitForChildProcess",
]
