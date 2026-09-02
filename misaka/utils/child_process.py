"""Subprocess helpers shared across coding-agent modules."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable, Mapping
from typing import Any

_EXIT_STDIO_GRACE_SECONDS = 0.1


class _ChildProcessProtocol(asyncio.SubprocessProtocol):
    def __init__(self, on_data: Callable[[int, bytes], None]) -> None:
        self._loop = asyncio.get_running_loop()
        self._on_data = on_data
        self._transport: asyncio.SubprocessTransport | None = None
        self._open_output_pipes = {1, 2}
        self._exited = False
        self._returncode: int | None = None
        self._idle_handle: asyncio.TimerHandle | None = None
        self._error: BaseException | None = None
        self._settled = False
        self.done: asyncio.Future[int | None] = self._loop.create_future()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        if self._settled:
            return
        if self._error is None:
            try:
                self._on_data(fd, data)
            except BaseException as error:  # noqa: BLE001 - wait() must report callback failures
                self._error = error
        if self._exited:
            self._arm_idle_timer()

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
            self._arm_idle_timer()

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None and self._error is None:
            self._error = exc
        if self._exited:
            self._finish()

    def _arm_idle_timer(self) -> None:
        if self._settled:
            return
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        self._idle_handle = self._loop.call_later(
            _EXIT_STDIO_GRACE_SECONDS,
            self._finish,
        )

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
) -> ChildProcess:
    callback = on_data if on_data is not None else (lambda _fd, _data: None)
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.subprocess_exec(
        lambda: _ChildProcessProtocol(callback),
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
    return ChildProcess(transport, protocol)


async def wait_for_child_process(child: ChildProcess) -> int | None:
    return await child.wait()


__all__: list[str] = []
