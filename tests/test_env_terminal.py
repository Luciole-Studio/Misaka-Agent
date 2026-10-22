"""pi env's ``test/terminal.test.ts``: ``openTerminal`` against the daemon, skipped on Windows as upstream (``pty`` is Unix
only). Each test runs within vitest's timeout of its upstream ``it`` (20 seconds where it names one, else the default 5
seconds)."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from misaka.env import Connection, RemoteError, openTerminal
from tests.env_support import closeConnection, daemonCommand, removeTempDir

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pi-env terminals are Unix only")


@pytest.fixture
async def connect() -> AsyncIterator[Callable[[], tuple[Connection, str]]]:
    connections: list[Connection] = []
    dirs: list[str] = []

    def create() -> tuple[Connection, str]:
        cwd = tempfile.mkdtemp(prefix="pi-env-terminal-")
        dirs.append(cwd)
        connection = Connection({"command": daemonCommand()})
        connections.append(connection)
        return connection, cwd

    yield create
    for connection in connections:
        await closeConnection(connection)
    for directory in dirs:
        removeTempDir(directory)


class Collector:
    """Collects output and waits for text to appear in it."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._waiting: list[tuple[str, asyncio.Future[None]]] = []

    def bytes(self) -> bytes:
        return b"".join(self._chunks)

    def onData(self, data: bytes) -> None:
        self._chunks.append(bytes(data))
        everything = self.bytes().decode("latin-1")
        waiting, self._waiting = self._waiting, []
        for text, future in waiting:
            if text in everything:
                if not future.done():
                    future.set_result(None)
            else:
                self._waiting.append((text, future))

    def waitFor(self, text: str) -> asyncio.Future[None]:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        if text in self.bytes().decode("latin-1"):
            future.set_result(None)
        else:
            self._waiting.append((text, future))
        return future


async def test_runs_a_program_in_a_terminal_with_input_resizing_and_its_exit_code(connect):
    async with asyncio.timeout(20):
        connection, cwd = connect()
        output = Collector()
        terminal = await openTerminal(
            connection,
            {
                "cwd": cwd,
                "argv": ["/bin/sh"],
                "env": {"PS1": "$ ", "TERM": "xterm-256color"},
                "cols": 80,
                "rows": 24,
                "onData": output.onData,
            },
        )
        assert terminal.pid > 0
        terminal.write("test -t 0 && echo IS-A-TTY; stty size; echo $TERM\n")
        await output.waitFor("IS-A-TTY")
        await output.waitFor("24 80")
        await output.waitFor("xterm-256color")
        terminal.resize(100, 30)
        terminal.write("stty size\n")
        await output.waitFor("30 100")
        terminal.write("exit 3\n")
        assert await terminal.exited == 3


async def test_reports_the_job_in_the_foreground_not_the_shell_at_its_prompt(connect):
    async with asyncio.timeout(20):
        connection, cwd = connect()
        output = Collector()
        terminal = await openTerminal(
            connection,
            {
                "cwd": cwd,
                # Interactive, for job control: the shell puts each job in front of the terminal.
                "argv": ["/bin/sh", "-i"],
                "env": {"PS1": "$ ", "TERM": "xterm-256color"},
                "cols": 80,
                "rows": 24,
                "onData": output.onData,
            },
        )
        terminal.write("echo READY\n")
        await output.waitFor("READY")
        assert await terminal.foreground() is None
        terminal.write("sleep 100\n")
        # The shell forks before it execs: wait until the foreground job is the command itself, not the forked shell.
        named = sys.platform == "darwin" or sys.platform.startswith("linux")
        foreground = await terminal.foreground()
        tries = 0
        while (not foreground or (named and foreground.get("name") != "sleep")) and tries < 100:
            await asyncio.sleep(0.02)
            foreground = await terminal.foreground()
            tries += 1
        assert foreground is not None
        assert foreground["pid"] > 0
        assert foreground["pid"] != terminal.pid
        if named:
            assert foreground.get("name") == "sleep"
        terminal.close()
        with pytest.raises(RemoteError) as exited:
            await terminal.exited
        assert exited.value.code == "aborted"
        with pytest.raises(RemoteError) as ended:
            await terminal.foreground()
        assert ended.value.code == "EBADF"


async def test_delivers_raw_bytes_not_decoded_text(connect):
    async with asyncio.timeout(5):
        connection, cwd = connect()
        output = Collector()
        terminal = await openTerminal(
            connection,
            {"cwd": cwd, "argv": ["sh", "-c", "printf 'a\\377b'"], "cols": 80, "rows": 24, "onData": output.onData},
        )
        assert await terminal.exited == 0
        assert list(output.bytes()) == [0x61, 0xFF, 0x62]


async def test_keeps_input_in_order(connect):
    async with asyncio.timeout(20):
        connection, cwd = connect()
        terminal = await openTerminal(
            connection,
            {
                "cwd": cwd,
                "argv": ["sh", "-c", "head -c 40000 > input.txt"],
                "cols": 80,
                "rows": 24,
                "onData": lambda data: None,
            },
        )
        text = ""
        for index in range(4000):
            piece = f"{index:09d}\n"
            text += piece
            terminal.write(piece)
        assert await terminal.exited == 0
        assert Path(cwd, "input.txt").read_text(encoding="utf-8") == text


async def test_kills_the_program_on_close(connect):
    async with asyncio.timeout(5):
        connection, cwd = connect()
        terminal = await openTerminal(
            connection, {"cwd": cwd, "argv": ["sleep", "100"], "cols": 80, "rows": 24, "onData": lambda data: None}
        )
        terminal.close()
        with pytest.raises(RemoteError) as exited:
            await terminal.exited
        assert exited.value.code == "aborted"


async def test_fails_to_open_in_a_missing_directory(connect):
    async with asyncio.timeout(5):
        connection, cwd = connect()
        with pytest.raises(RemoteError):
            await openTerminal(
                connection, {"cwd": os.path.join(cwd, "missing"), "cols": 80, "rows": 24, "onData": lambda data: None}
            )


async def test_rejects_writes_to_a_terminal_that_does_not_exist(connect):
    async with asyncio.timeout(5):
        connection, _ = connect()
        with pytest.raises(RemoteError) as rejected:
            await connection.request("ptyWrite", {"target": 999})
        assert rejected.value.code == "EBADF"
