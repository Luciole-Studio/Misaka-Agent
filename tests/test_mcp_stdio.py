"""pi's ``packages/mcp/test/stdio.test.ts``, plus the cross-spawn port (its escaping is checked against
vectors node produced from cross-spawn 7.0.6 itself) and the framing rules of the transport."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

from misaka.mcp.client import McpClient
from misaka.mcp.transports import _cross_spawn as crossSpawn
from misaka.mcp.transports.stdio import StdioTransport

FIXTURES = Path(__file__).parent / "fixtures" / "mcp"
fixture = str(FIXTURES / "stdio_server.py")
stubborn = str(FIXTURES / "stubborn_server.py")


async def waitFor(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


# -- stdio.test.ts ---------------------------------------------------------------------------


async def test_connects_to_a_newline_delimited_mcp_server_and_captures_stderr():
    stderr: list[str] = []
    transport = StdioTransport({"command": sys.executable, "args": [fixture], "onStderr": stderr.append})
    client = McpClient({"name": "stdio-test", "version": "1.0.0"})
    await client.connect(transport)
    assert await client.listTools() == [{"name": "echo", "inputSchema": {"type": "object"}}]
    assert await client.callTool("echo", {"text": "hello"}) == {"content": [{"type": "text", "text": "hello"}]}
    assert isinstance(transport.pid, int)
    # Upstream waits 10 ms; the stderr line races stdout, so wait until it arrived.
    await waitFor(lambda: "stdio fixture ready" in transport.stderr)
    assert "stdio fixture ready" in "".join(stderr)
    await client.close()
    assert client.connectionState == "closed"


async def test_takes_the_send_options_the_client_passes_and_ignores_them():
    """MISAKA adaptation: the client passes ``send(message, {"signal": ...})`` to every transport (#10598); a
    TypeScript transport may leave the parameter out, a Python one must take it. stdio dispatches at once, so
    there is nothing for the signal to stop, also when it already aborted."""
    from misaka.utils.js import AbortController

    transport = StdioTransport({"command": sys.executable, "args": [fixture]})
    client = McpClient({"name": "stdio-test", "version": "1.0.0"})
    await client.connect(transport)
    aborted = AbortController()
    aborted.abort()
    await transport.send({"jsonrpc": "2.0", "method": "notifications/initialized"}, {"signal": aborted.signal})
    assert await client.callTool("echo", {"text": "hi"}) == {"content": [{"type": "text", "text": "hi"}]}
    await client.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups and signals")
async def test_kills_a_server_that_ignores_shutdown_including_its_children():
    import psutil

    async with asyncio.timeout(10):
        transport = StdioTransport({"command": sys.executable, "args": [stubborn], "closeTimeoutMs": 100})
        client = McpClient({"name": "stdio-test", "version": "1.0.0"})
        await client.connect(transport)
        grandchild = None
        for _ in range(100):
            match = re.search(r"grandchild (\d+)", transport.stderr)
            if match:
                grandchild = int(match.group(1))
                break
            await asyncio.sleep(0.01)
        assert isinstance(grandchild, int)

        startedAt = time.monotonic()
        await client.close()
        assert time.monotonic() - startedAt < 5
        alive = True
        for _ in range(100):
            if not psutil.pid_exists(grandchild) or psutil.Process(grandchild).status() == psutil.STATUS_ZOMBIE:
                alive = False
                break
            await asyncio.sleep(0.02)
        assert alive is False


# -- MISAKA: framing, errors and the cross-spawn port ------------------------------------------


def _server(source: str, tmp_path: Path) -> str:
    path = tmp_path / "server.py"
    path.write_text(source, encoding="utf-8")
    return str(path)


async def test_frames_crlf_lines_and_reports_bad_lines_without_dropping_the_connection(tmp_path):
    source = (
        "import sys, json\n"
        "for line in sys.stdin.buffer:\n"
        "    m = json.loads(line)\n"
        "    if 'id' not in m: continue\n"
        "    out = sys.stdout.buffer\n"
        "    if m['method'] == 'initialize':\n"
        "        out.write(b'not json\\r\\n\\r\\n')\n"
        "        r = {'protocolVersion': '2025-06-18', 'capabilities': {}, 'serverInfo': {'name': 's', 'version': '1'}}\n"
        "    else:\n"
        "        r = {}\n"
        "    out.write(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': r}).encode() + b'\\r\\n')\n"
        "    out.flush()\n"
    )
    transport = StdioTransport({"command": sys.executable, "args": [_server(source, tmp_path)]})
    client = McpClient({"name": "stdio-test", "version": "1.0.0"})
    errors: list = []
    client.onError(errors.append)
    await client.connect(transport)
    await client.ping()
    # The bad line is one error; the blank CRLF line is skipped.
    assert len(errors) == 1
    await client.close()


async def test_a_message_over_the_size_limit_is_reported(tmp_path):
    source = (
        "import sys, json\n"
        "for line in sys.stdin.buffer:\n"
        "    m = json.loads(line)\n"
        "    if m.get('method') != 'initialize': continue\n"
        "    sys.stdout.buffer.write(b'x' * 300 + b'\\n')\n"
        "    r = {'protocolVersion': '2025-06-18', 'capabilities': {}, 'serverInfo': {'name': 's', 'version': '1'}}\n"
        "    sys.stdout.buffer.write(json.dumps({'jsonrpc': '2.0', 'id': m['id'], 'result': r}).encode() + b'\\n')\n"
        "    sys.stdout.buffer.flush()\n"
    )
    transport = StdioTransport({"command": sys.executable, "args": [_server(source, tmp_path)], "maxMessageBytes": 256})
    client = McpClient({"name": "stdio-test", "version": "1.0.0"})
    errors: list = []
    client.onError(errors.append)
    await client.connect(transport)
    assert [str(error) for error in errors] == ["MCP stdio message exceeds 256 bytes"]
    await client.close()


async def test_a_missing_command_fails_the_connection():
    transport = StdioTransport({"command": "misaka-no-such-mcp-server"})
    client = McpClient({"name": "stdio-test", "version": "1.0.0", "requestTimeoutMs": 5000})
    errors: list = []
    client.onError(errors.append)
    if sys.platform == "win32":
        # cross-spawn runs an unknown command through cmd.exe, which starts and exits 1; the ENOENT
        # arrives as an error event while the connection closes.
        with pytest.raises(Exception, match="MCP connection closed"):
            await client.connect(transport)
    else:
        with pytest.raises(Exception, match="spawn misaka-no-such-mcp-server ENOENT"):
            await client.connect(transport)
    assert client.connectionState == "closed"


VECTORS = json.loads((FIXTURES / "cross_spawn_vectors.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(("arg", "single", "double"), VECTORS["argument"])
def test_escape_argument_matches_cross_spawn(arg, single, double):
    assert crossSpawn.escapeArgument(arg, False) == single
    assert crossSpawn.escapeArgument(arg, True) == double


@pytest.mark.parametrize(("command", "escaped"), VECTORS["command"])
def test_escape_command_matches_cross_spawn(command, escaped):
    assert crossSpawn.escapeCommand(command) == escaped


@pytest.mark.parametrize(("text", "command"), VECTORS["shebang"])
def test_shebang_command_matches_cross_spawn(text, command):
    assert crossSpawn._shebangCommand(text) == command


def test_path_key_takes_the_last_path_spelling(monkeypatch):
    monkeypatch.setattr(crossSpawn, "isWin", True)
    assert crossSpawn.pathKey({"PATH": "a", "Path": "b"}) == "Path"
    assert crossSpawn.pathKey({"Path": "a", "PATH": "b"}) == "PATH"
    assert crossSpawn.pathKey({"HOME": "x"}) == "Path"
    monkeypatch.setattr(crossSpawn, "isWin", False)
    assert crossSpawn.pathKey({"Path": "b"}) == "PATH"


async def test_a_failed_write_destroys_stdin_and_the_writer_thread_ends_with_the_server():
    loop = asyncio.get_running_loop()
    source = "import os, sys, time; sys.stdout.write('closed\\n'); sys.stdout.flush(); os.close(0); time.sleep(1)"
    child = crossSpawn.spawn(sys.executable, ["-c", source], {})
    stdinClosed, closed = loop.create_future(), loop.create_future()
    child.stdout.on("data", lambda _chunk: stdinClosed.done() or stdinClosed.set_result(None))  # type: ignore[union-attr]
    child.on("close", lambda *_args: closed.done() or closed.set_result(None))
    errors: list[BaseException] = []
    child.stdin.on("error", errors.append)  # type: ignore[union-attr]
    await asyncio.wait_for(stdinClosed, 10)

    outcomes: list[BaseException | None] = []
    for _ in range(3):
        written = loop.create_future()
        child.stdin.write("x" * 70000, lambda error, written=written: written.set_result(error))  # type: ignore[union-attr]
        outcomes.append(await asyncio.wait_for(written, 5))
    await asyncio.wait_for(closed, 10)

    assert isinstance(outcomes[0], OSError)
    assert [getattr(error, "code", None) for error in outcomes[1:]] == ["ERR_STREAM_DESTROYED"] * 2
    assert errors == [outcomes[0]]
    writer = child._threads[2]
    await waitFor(lambda: not writer.is_alive())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
async def test_a_server_runs_in_the_job_that_ends_it_with_this_process():
    import ctypes
    from ctypes import wintypes

    from misaka.utils import libuv_windows

    transport = StdioTransport({"command": sys.executable, "args": [fixture]})
    client = McpClient({"name": "stdio-test", "version": "1.0.0"})
    await client.connect(transport)
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        inJob = wintypes.BOOL()
        handle = int(transport._child._process._handle)  # type: ignore[union-attr]
        assert kernel32.IsProcessInJob(handle, libuv_windows.job(), ctypes.byref(inJob))
        assert inJob.value
    finally:
        await client.close()


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe shims")
async def test_a_cmd_shim_runs_through_cmd_exe_with_escaped_arguments(tmp_path):
    shim = tmp_path / "server.cmd"
    shim.write_text(f'@"{sys.executable}" "{fixture}" %*\r\n', encoding="utf-8")
    # On PATH: cmd.exe skips the current directory when NoDefaultCurrentDirectoryInExePath is set.
    path = f"{tmp_path};{os.environ.get('PATH', '')}"
    transport = StdioTransport({"command": "server", "args": ["a & b"], "cwd": str(tmp_path), "env": {"PATH": path}})
    client = McpClient({"name": "stdio-test", "version": "1.0.0"})
    await client.connect(transport)
    assert await client.callTool("echo", {"text": "via cmd"}) == {"content": [{"type": "text", "text": "via cmd"}]}
    await client.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows command resolution")
def test_an_executable_resolves_through_the_child_path_not_misakas_directory(tmp_path):
    parsed = crossSpawn.parse("python", [], {"env": {"PATH": str(Path(sys.executable).parent)}, "cwd": str(tmp_path)})
    assert not parsed.verbatim
    assert parsed.executable is not None and Path(parsed.executable).parent == Path(sys.executable).parent
