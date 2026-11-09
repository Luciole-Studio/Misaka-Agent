"""pi env's ``test/differential.test.ts``: ``RemoteExecutionEnv`` against ``NodeExecutionEnv`` on the same machine.

MISAKA fork: the reference is ``misaka.durable.env.node.NodeExecutionEnv``, the port of Durable's Node environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.tools import createBashTool, createReadTool
from misaka.env import Connection, RemoteExecutionEnv
from misaka.utils.js import errorMessage
from tests.env_support import closeConnection, daemonCommand, removeTempDir

pytestmark = pytest.mark.asyncio(loop_scope="module")

context = BACKGROUND_CONTEXT
state: dict[str, Any] = {"dirs": []}


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def connection() -> AsyncIterator[Connection]:
    connection = Connection({"command": daemonCommand()})
    state["connection"] = connection
    yield connection
    await closeConnection(connection)
    for directory in state["dirs"]:
        removeTempDir(directory)


def pair() -> tuple[Any, Any, tuple[str, str]]:
    localRoot = tempfile.mkdtemp(prefix="pi-env-local-")
    remoteRoot = tempfile.mkdtemp(prefix="pi-env-remote-")
    state["dirs"].extend([localRoot, remoteRoot])
    return (
        NodeExecutionEnv({"cwd": localRoot}),
        RemoteExecutionEnv({"connection": state["connection"], "id": "pi-env:test", "cwd": remoteRoot}),
        (localRoot, remoteRoot),
    )


def _int32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value


def _imul(a: int, b: int) -> int:
    return _int32((a & 0xFFFFFFFF) * (b & 0xFFFFFFFF))


def random(seed: int) -> Callable[[], float]:
    """mulberry32, as the upstream test seeds it (``Math.imul`` on 32-bit integers)."""
    current = seed & 0xFFFFFFFF

    def next() -> float:
        nonlocal current
        current = (current + 0x6D2B79F5) & 0xFFFFFFFF
        t = current
        t = _imul(_int32(t) ^ (t >> 15), _int32(t) | 1)
        t = _int32(t ^ _int32(t + _imul(t ^ ((t & 0xFFFFFFFF) >> 7), t | 61)))
        return ((t ^ ((t & 0xFFFFFFFF) >> 14)) & 0xFFFFFFFF) / 4294967296

    return next


def normalize(value: Any, root: str) -> Any:
    """A result with the environment's root replaced and only the error code of failures, for comparison."""

    def walk(field: Any, key: str | None = None) -> Any:
        if isinstance(field, bytes | bytearray | memoryview):
            return {"bytes": bytes(field).hex()}
        if key == "mtimeMs":
            return "mtime"
        if isinstance(field, dict):
            if field.get("ok") is False and "error" in field:
                return {"ok": False, "code": field["error"].code}
            return {name: walk(item, name) for name, item in field.items()}
        if isinstance(field, list | tuple):
            return [walk(item) for item in field]
        return field

    text = json.dumps(walk(value))

    # Roots appear JSON-escaped (backslashes doubled on Windows), and resolved paths spell them without Windows' 8.3
    # short names.
    def escaped(path: str) -> str:
        return json.dumps(path)[1:-1]

    return json.loads(text.replace(escaped(os.path.realpath(root)), "<root>").replace(escaped(root), "<root>"))


NAMES = ["a.txt", "b", "dir", "dir/c.txt", "dir/sub", "dir/sub/d.bin", "missing", "a.txt/x"]
CONTENTS: list[str | bytes] = [
    "",
    "hello\n",
    "﻿bom\r\nline",
    bytes([0xE2, 0x82, 0x0A, 0xFF, 0xEF, 0xBB, 0xBF]),
    "x" * 70_000,
    # Crosses write chunks, read chunks and multi-byte characters split between them.
    "é" * 300_001,
]

type Operation = Callable[[Any], Awaitable[Any]]


def randomOperation(next: Callable[[], float]) -> tuple[str, Operation]:
    """A random operation with its arguments fixed, and a label for failure messages."""
    args: list[Any] = []

    def pick(value: Any) -> Any:
        if isinstance(value, bytes):
            args.append("<bytes>")
        elif isinstance(value, str) and len(value) > 20:
            args.append(f"<{len(value)} chars>")
        else:
            args.append(value)
        return value

    def nameValue() -> str:
        return pick(NAMES[int(next() * len(NAMES))])

    def contentValue() -> str | bytes:
        return pick(CONTENTS[int(next() * len(CONTENTS))])

    def flagValue() -> bool:
        return pick(next() < 0.5)

    def small(limit: int) -> int:
        return pick(int(next() * limit))

    def writeFile() -> Operation:
        n, c = nameValue(), contentValue()
        return lambda env: env.writeFile(n, c, context)

    def appendFile() -> Operation:
        n, c = nameValue(), contentValue()
        return lambda env: env.appendFile(n, c, context)

    def readTextFile() -> Operation:
        n = nameValue()
        return lambda env: env.readTextFile(n, context)

    def readBinaryFile() -> Operation:
        n = nameValue()
        return lambda env: env.readBinaryFile(n, context)

    def readTextLines() -> Operation:
        n, m = nameValue(), small(4)
        return lambda env: env.readTextLines(n, {"maxLines": m}, context)

    def fileInfo() -> Operation:
        n = nameValue()
        return lambda env: env.fileInfo(n, context)

    def exists() -> Operation:
        n = nameValue()
        return lambda env: env.exists(n, context)

    def listDir() -> Operation:
        n = nameValue()
        return lambda env: env.listDir(n, context)

    def createDir() -> Operation:
        n, r = nameValue(), flagValue()
        return lambda env: env.createDir(n, {"recursive": r}, context)

    def remove() -> Operation:
        n, r, f = nameValue(), flagValue(), flagValue()
        return lambda env: env.remove(n, {"recursive": r, "force": f}, context)

    def renameFile() -> Operation:
        a, b = nameValue(), nameValue()
        return lambda env: env.renameFile(a, b, context)

    def truncateFile() -> Operation:
        n, size = nameValue(), small(10)
        return lambda env: env.truncateFile(n, size, context)

    def flushFile() -> Operation:
        n = nameValue()
        return lambda env: env.flushFile(n, context)

    def canonicalPath() -> Operation:
        n = nameValue()
        return lambda env: env.canonicalPath(n, context)

    def openBinaryReader() -> Operation:
        n, noFollow, offset, length = nameValue(), flagValue(), small(8), small(8)

        async def run(env: Any) -> Any:
            reader = await env.openBinaryReader(n, {"noFollow": noFollow}, context)
            if not reader["ok"]:
                return reader
            try:
                return [
                    await reader["value"].read(offset, length, context),
                    await reader["value"].scanLines({"startLine": 0}, context),
                ]
            finally:
                await reader["value"].close(context)

        return run

    choices: list[tuple[str, Callable[[], Operation]]] = [
        ("writeFile", writeFile),
        ("appendFile", appendFile),
        ("readTextFile", readTextFile),
        ("readBinaryFile", readBinaryFile),
        ("readTextLines", readTextLines),
        ("fileInfo", fileInfo),
        ("exists", exists),
        ("listDir", listDir),
        ("createDir", createDir),
        ("remove", remove),
        ("renameFile", renameFile),
        ("truncateFile", truncateFile),
        ("flushFile", flushFile),
        ("canonicalPath", canonicalPath),
        ("openBinaryReader", openBinaryReader),
    ]
    label, make = choices[int(next() * len(choices))]
    run = make()
    return f"{label}({', '.join(json.dumps(arg) for arg in args)})", run


async def test_gives_the_same_results_for_random_file_operation_sequences(connection):
    for seed in range(1, 61):
        next = random(seed)
        local, remote, roots = pair()
        for step in range(25):
            label, run = randomOperation(next)
            expected = normalize(await run(local), roots[0])
            actual = normalize(await run(remote), roots[1])
            assert actual == expected, f"seed {seed} step {step} {label}"


@pytest.mark.skipif(sys.platform == "win32", reason="no FIFOs or /dev/null on Windows")
async def test_reads_devices_fifos_and_directories_like_node(connection):
    local, remote, roots = pair()
    writers: list[subprocess.Popen[bytes]] = []

    async def results(env: Any, root: str) -> Any:
        fifo = os.path.join(root, "fifo")
        made = await env.exec(["mkfifo", fifo], None, context)
        if not made["ok"] or made["value"]["exitCode"] != 0:
            raise RuntimeError("mkfifo failed")
        await env.createDir("dir", None, context)

        # A writer for each blocking open of the FIFO.
        def write(text: str) -> None:
            writers.append(subprocess.Popen(["sh", "-c", 'printf \'%s\' "$1" > "$2"', "sh", text, fifo]))

        write("fifo text\n")
        fifoText = await env.readTextFile("fifo", context)
        write("fifo bytes")
        fifoBytes = await env.readBinaryFile("fifo", context)
        write("x")
        fifoLines = await env.readTextLines("fifo", None, context)
        dirReader = await env.openTextLineReader("dir", context)
        dirLine = await dirReader["value"].readLine(context) if dirReader["ok"] else None
        if dirReader["ok"]:
            await dirReader["value"].close(context)
        return normalize(
            {
                "nullText": await env.readTextFile("/dev/null", context),
                "nullBytes": await env.readBinaryFile("/dev/null", context),
                "nullLines": await env.readTextLines("/dev/null", None, context),
                "fifoText": fifoText,
                "fifoBytes": fifoBytes,
                "fifoLines": fifoLines,
                "dirReaderOpened": dirReader["ok"],
                "dirLine": dirLine,
                "dirText": await env.readTextFile("dir", context),
                "dirBytes": await env.readBinaryFile("dir", context),
            },
            root,
        )

    try:
        assert await results(remote, roots[1]) == await results(local, roots[0])
    finally:
        for writer in writers:
            writer.wait(10)


def writeBytes(path: bytes, data: bytes) -> None:
    with open(path, "wb") as file:
        file.write(data)


# macOS file systems refuse names that are not UTF-8.
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="names that are not UTF-8 need Linux")
async def test_lists_names_that_are_not_utf_8_like_node(connection):
    local, remote, roots = pair()
    for root in roots:
        for name in [[0x62], [0x61, 0xFF], [0xC3, 0xA9], [0x61, 0xFE, 0x7A], [0x7A]]:
            writeBytes(os.fsencode(root) + b"/" + bytes(name), b"x")

    async def results(env: Any, root: str) -> Any:
        reader = await env.openDirReader(".", context)
        pages: list[Any] = []
        if reader["ok"]:
            done = False
            while not done:
                page = await reader["value"].next(2, context)
                pages.append(page)
                done = not page["ok"] or page["value"]["done"]
            await reader["value"].close(context)
        return normalize({"list": await env.listDir(".", context), "pages": pages}, root)

    assert await results(remote, roots[1]) == await results(local, roots[0])


async def test_gives_the_same_read_and_bash_tool_results(connection):
    local, remote, roots = pair()
    files: list[tuple[str, str | bytes]] = [
        ("text.txt", "one\ntwo\n\nthree"),
        ("big.txt", "line\n" * 3000),
        ("long.txt", f"{'é' * 40_000}\nend"),
        ("bom.txt", "﻿hello"),
        ("bad.txt", bytes([0x61, 0xE2, 0x82, 0x0A, 0x62])),
    ]
    for path, content in files:
        await local.writeFile(path, content, context)
        await remote.writeFile(path, content, context)

    def api(env: Any, output: list[str]) -> Any:
        async def details(*_: Any) -> None:
            return None

        return SimpleNamespace(
            env=env,
            outputWindow=None,
            output=lambda chunk, skipped=None: output.append(chunk if isinstance(chunk, str) else chunk.decode()),
            diagnostic=lambda _diagnostic: None,
            details=details,
        )

    async def run(env: Any, tool: dict[str, Any], args: dict[str, Any]) -> Any:
        output: list[str] = []
        try:
            result = await tool["execute"](args, api(env, output), context)
            return {"result": result, "output": "".join(output)}
        except Exception as error:  # noqa: BLE001 - the tool's failure is compared
            return {"error": errorMessage(error), "output": "".join(output)}

    cases: list[tuple[dict[str, Any], dict[str, Any]]] = [
        (createReadTool(), {"path": "text.txt"}),
        (createReadTool(), {"path": "text.txt", "offset": 2, "limit": 1}),
        (createReadTool(), {"path": "big.txt"}),
        (createReadTool(), {"path": "long.txt"}),
        (createReadTool(), {"path": "bom.txt"}),
        (createReadTool(), {"path": "bad.txt"}),
        (createReadTool(), {"path": "missing.txt"}),
        (createBashTool(), {"command": "cat text.txt; echo err >&2; exit 0"}),
        (createBashTool(), {"command": "exit 3"}),
        (createBashTool(), {"command": "printf 'a\\nb'; ls"}),
    ]
    for tool, args in cases:
        expected = normalize(await run(local, tool, args), roots[0])
        actual = normalize(await run(remote, tool, args), roots[1])
        assert actual == expected, json.dumps(args)
