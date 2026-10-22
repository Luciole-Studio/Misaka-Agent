"""Regression tests for the FX6 fixes of the pi-env ``pty`` operation (V6-ENV-B03, B04, B07, B08, B09, B10): the
daemon's ``pty.rs`` starts a program the way Rust's ``Command::spawn`` does, and the client encodes input as
``TextEncoder`` does. Unix only, as ``pty`` is."""

from __future__ import annotations

import asyncio
import builtins
import errno
import io
import os
import struct
import sys
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable

import pytest

from misaka.env import Connection, RemoteError, openTerminal
from misaka.env.daemon import exec as daemon_exec
from misaka.env.daemon import main as daemon_main
from misaka.env.daemon import pty as daemon_pty
from misaka.env.daemon.frame import ERROR, REQUEST, Frame, write_frame
from misaka.env.daemon.output import Output
from misaka.env.terminal import RemoteTerminal
from misaka.utils.js import AbortController
from tests.env_support import closeConnection, daemonCommand, removeTempDir

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pi-env terminals are Unix only")


@pytest.fixture
async def connect() -> AsyncIterator[Callable[[], tuple[Connection, str]]]:
    connections: list[Connection] = []
    dirs: list[str] = []

    def create() -> tuple[Connection, str]:
        cwd = tempfile.mkdtemp(prefix="pi-env-terminal-fx6-")
        dirs.append(cwd)
        connection = Connection({"command": daemonCommand()})
        connections.append(connection)
        return connection, cwd

    yield create
    for connection in connections:
        await closeConnection(connection)
    for directory in dirs:
        removeTempDir(directory)


async def run(connection: Connection, options: dict) -> tuple[int, bytes]:
    output = bytearray()
    terminal = await openTerminal(connection, {"cols": 200, "rows": 24, "onData": output.extend, **options})
    return await terminal.exited, bytes(output)


def writeFile(path: str, text: str, mode: int = 0o644) -> None:
    with open(path, "w", encoding="utf-8") as file:
        file.write(text)
    os.chmod(path, mode)


def osError(code: int) -> str:
    """Rust's ``io::Error`` text for an error number."""
    return f"{os.strerror(code)} (os error {code})"


# V6-ENV-B03: a program that cannot be started fails the open, before ``started``, with Rust's text.


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["/nonexistent/program"], errno.ENOENT),
        (["pi-env-no-such-program-anywhere"], errno.ENOENT),
        (None, errno.EACCES),
    ],
    ids=["missing", "not-on-path", "not-executable"],
)
async def test_b03_a_program_that_cannot_be_started_fails_the_open(connect, argv, code):
    async with asyncio.timeout(10):
        connection, cwd = connect()
        if argv is None:
            path = os.path.join(cwd, "plain")
            writeFile(path, "#!/bin/sh\n")
            argv = [path]
        with pytest.raises(RemoteError) as rejected:
            await run(connection, {"cwd": cwd, "argv": argv})
        assert (rejected.value.code, str(rejected.value)) == ("spawn_error", osError(code))


async def test_b03_a_working_directory_that_is_a_file_fails_with_rusts_text(connect):
    async with asyncio.timeout(10):
        connection, cwd = connect()
        path = os.path.join(cwd, "file")
        writeFile(path, "")
        with pytest.raises(RemoteError) as rejected:
            await run(connection, {"cwd": path, "argv": ["true"]})
        assert (rejected.value.code, str(rejected.value)) == ("spawn_error", osError(errno.ENOTDIR))


async def test_b03_a_script_without_an_interpreter_line_runs_through_sh_as_execvp_does(connect):
    async with asyncio.timeout(10):
        connection, cwd = connect()
        path = os.path.join(cwd, "script")
        writeFile(path, "echo RAN $1\n", 0o755)
        code, output = await run(connection, {"cwd": cwd, "argv": [path, "here"]})
        assert code == 0
        assert b"RAN here" in output


async def test_b03_an_empty_path_entry_is_the_working_directory_as_execvp_names_it(connect):
    async with asyncio.timeout(10):
        connection, cwd = connect()
        writeFile(os.path.join(cwd, "hello"), "#!/bin/sh\necho ZERO=$0\n", 0o755)
        code, output = await run(connection, {"cwd": cwd, "argv": ["hello"], "env": {"PATH": ""}})
        assert code == 0
        # macOS's execvp runs `./hello`, glibc's `hello`.
        assert (b"ZERO=./hello" if sys.platform == "darwin" else b"ZERO=hello") in output


async def test_b03_a_nul_byte_fails_the_open_as_rust_refuses_it(connect):
    async with asyncio.timeout(10):
        connection, cwd = connect()
        with pytest.raises(RemoteError) as rejected:
            await run(connection, {"cwd": cwd, "argv": ["true"], "env": {"A": "x\u0000y"}})
        assert (rejected.value.code, str(rejected.value)) == ("spawn_error", "nul byte found in provided data")


# V6-ENV-B04: the program's environment is exactly the one the daemon computed.


async def test_b04_without_inherit_env_the_program_gets_exactly_the_given_environment_ordered_by_name(connect):
    async with asyncio.timeout(10):
        connection, cwd = connect()
        code, output = await run(
            connection, {"cwd": cwd, "argv": ["/usr/bin/env"], "env": {"Z": "1", "A": "2", "M": "3"}, "inheritEnv": False}
        )
        assert code == 0
        # Rust's `Command` keeps changed variables in a `BTreeMap`.
        assert output == b"A=2\r\nM=3\r\nZ=1\r\n"


async def test_b04_a_daemon_started_without_a_locale_does_not_hand_its_programs_one(connect, monkeypatch):
    for key in [key for key in os.environ if key == "LANG" or key.startswith("LC_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("PI_ENV_FX6_MARK", "1")
    expected = dict(os.environ)
    async with asyncio.timeout(10):
        connection, cwd = connect()
        code, output = await run(connection, {"cwd": cwd, "argv": ["/usr/bin/env"]})
    assert code == 0
    seen = dict(line.split("=", 1) for line in output.decode("utf-8", "replace").split("\r\n") if "=" in line)
    assert "LC_CTYPE" not in seen
    # What the daemon was started with reaches the program unchanged; nothing its interpreter's startup adds does. A
    # daemon named by PI_ENV_DAEMON may start through a trampoline (macOS's /usr/bin/python3 changes the environment
    # before the interpreter runs), so only the locale is checked then.
    if "PI_ENV_DAEMON" not in os.environ:
        assert seen == expected


def test_b04_the_daemon_restores_the_environment_it_was_started_with():
    import subprocess

    probe = (
        "import os\n"
        "from misaka.env.daemon.main import restore_environment, startup_environment\n"
        "restore_environment()\n"
        "print(sorted(os.environ.items()), sorted(startup_environment().items()))\n"
    )
    # Started without a locale, the interpreter coerces one into its own environment (PEP 538).
    environment = {"A": "1", "B": "x=y"}
    done = subprocess.run([sys.executable, "-c", probe], env=environment, capture_output=True, text=True, encoding="utf-8", check=True)
    expected = sorted(environment.items())
    assert done.stdout.strip() == f"{expected} {sorted((key.encode(), value.encode()) for key, value in expected)}"


# V6-ENV-B07: a descriptor past FD_SETSIZE does not kill the terminal's reader.


def test_b07_reads_from_a_descriptor_past_fd_setsize():
    import fcntl
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < 1600:
        if hard != resource.RLIM_INFINITY and hard < 1600:
            pytest.skip("cannot open descriptors past 1600")
        resource.setrlimit(resource.RLIMIT_NOFILE, (1600, hard))
    master, slave = os.openpty()
    high = 1500
    try:
        os.dup2(master, high)
        flags = fcntl.fcntl(high, fcntl.F_GETFL)
        fcntl.fcntl(high, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        received: list[bytes] = []
        os.write(slave, b"hello\n")
        stop = threading.Event()

        def on_data(data: bytes) -> bool:
            received.append(data)
            return False

        daemon_pty.read(high, stop, on_data)
        assert b"".join(received).startswith(b"hello")
    finally:
        os.close(master)
        os.close(slave)
        try:
            os.close(high)
        except OSError:
            pass


def test_b07_a_wait_that_raises_value_error_ends_reading_and_writing(monkeypatch):
    def broken(fd: int, writable: bool) -> bool:
        raise ValueError("filedescriptor out of range in select()")

    monkeypatch.setattr(daemon_pty, "_poll", broken)
    daemon_pty.read(0, threading.Event(), lambda data: True)
    reader, writer = os.pipe()
    try:
        os.set_blocking(writer, False)
        # Input nobody reads makes the writer wait, which now fails.
        assert daemon_pty.write_all(writer, b"x" * (4 << 20), threading.Event()) is False
    finally:
        os.close(reader)
        os.close(writer)


# V6-ENV-B08: a failure in the in-order fast path answers the request instead of stopping the daemon.


def test_b08_an_unexpected_failure_in_the_fast_path_is_answered_and_serving_goes_on(monkeypatch):
    def failing(terminals: object, json: object) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(daemon_pty, "foreground", failing)
    output = Output()
    server = daemon_main.Server(output, tempfile.gettempdir())
    stream = io.BytesIO()
    write_frame(stream, Frame(REQUEST, 1, {"op": "ptyForeground", "target": 5}))
    write_frame(stream, Frame(REQUEST, 2, {"op": "ptyResize", "target": 6, "cols": 1, "rows": 1}))
    stream.seek(0)
    daemon_main.serve_frames(server, stream)
    replies = {frame.id: (frame.kind, frame.json) for frame in output.control_frames}
    assert replies[1] == (ERROR, {"code": "unknown", "message": "pi-env: boom"})
    assert replies[2] == (ERROR, {"code": "EBADF", "message": "no running terminal with this id"})


@pytest.mark.skipif(sys.platform != "darwin", reason="process names come from libproc on macOS only")
def test_b08_a_python_without_ctypes_does_not_know_the_process_name(monkeypatch):
    real = builtins.__import__

    def without_ctypes(name: str, *args: object, **kwargs: object) -> object:
        if name == "ctypes" or name.startswith("ctypes."):
            raise ImportError("No module named '_ctypes'")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_ctypes)
    assert daemon_pty.process_name(os.getpid()) is None


# V6-ENV-B09: a resize that found the terminal before it ended works on that terminal's descriptor.


def windowSize(fd: int) -> tuple[int, int]:
    import fcntl
    import termios

    rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))
    return cols, rows


def test_b09_a_resize_racing_the_end_of_its_terminal_never_reaches_another_descriptor(monkeypatch, tmp_path):
    terminals = daemon_pty.Terminals()
    request = daemon_pty.PtyRequest(
        {"argv": ["/bin/sh", "-c", "read line"], "cwd": str(tmp_path), "cols": 80, "rows": 24}
    )
    finished = threading.Event()
    result: dict = {}

    def running() -> None:
        try:
            result["value"] = daemon_pty.run(
                1, request, Output(), daemon_exec.Control(), daemon_exec.Groups(), terminals
            )
        except BaseException as error:  # noqa: BLE001 - reported by the assertion below
            result["error"] = error
        finally:
            finished.set()

    threading.Thread(target=running, daemon=True).start()
    deadline = time.monotonic() + 5
    while 1 not in terminals._terminals:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    master = terminals._terminals[1].master
    real = daemon_pty.set_size
    opened: list[int] = []
    sizes: list[tuple[int, int]] = []

    def racing(fd: int, cols: int, rows: int) -> None:
        # The terminal ends between finding it and resizing it, and its descriptor number becomes free for reuse.
        daemon_pty.write(terminals, {"target": 1}, b"\n")
        assert finished.wait(5)
        opened.extend(os.openpty())
        sizes.append(windowSize(opened[0]))
        real(fd, cols, rows)

    monkeypatch.setattr(daemon_pty, "set_size", racing)
    try:
        assert daemon_pty.resize(terminals, {"target": 1, "cols": 33, "rows": 11}) == {}
        # Another terminal kept its size; the ended one's descriptor closed once the resize let it go.
        assert windowSize(opened[0]) == sizes[0] != (33, 11)
        assert master not in opened
        with pytest.raises(OSError) as closed:
            os.fstat(master)
        assert closed.value.errno == errno.EBADF
    finally:
        for fd in opened:
            os.close(fd)
    assert result == {"value": {"exitCode": 0}}


# V6-ENV-B10: input text is encoded as TextEncoder encodes it.


async def test_b10_a_lone_surrogate_in_input_is_one_replacement_character():
    sent: list[bytes] = []
    terminal = RemoteTerminal(None, 1, 1, 1, asyncio.get_running_loop().create_future(), AbortController())  # type: ignore[arg-type]
    terminal._request = lambda op, json, payload=b"": sent.append(payload)  # type: ignore[method-assign]
    terminal.write("a\ud800b😀")
    assert sent == [b"a\xef\xbf\xbdb\xf0\x9f\x98\x80"]


def test_the_launcher_stays_python_3_9():
    import ast

    ast.parse(daemon_pty.LAUNCHER, feature_version=(3, 9))
