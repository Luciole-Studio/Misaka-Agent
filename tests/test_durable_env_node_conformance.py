"""pi durable's ``test/env-node-conformance.test.ts``."""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from misaka.chord.context import BACKGROUND_CONTEXT
from misaka.durable.env import getOrThrow
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.testing import createAssertions, createEnvConformance

windows = sys.platform == "win32"
gitBash = os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "Git", "bin", "bash.exe")
context = BACKGROUND_CONTEXT


def removeTempDir(directory: str) -> None:
    """Remove a test directory. On Windows, ``taskkill /T`` runs asynchronously and can miss descendants such as Git
    Bash's ``sleep``, so a killed command's processes can hold the directory for a while after ``exec`` settles; retry
    until they exit."""
    attempt = 0
    while True:
        try:
            shutil.rmtree(directory)
            return
        except FileNotFoundError:
            return
        except OSError as error:
            if attempt >= 80 or error.errno not in (errno.EBUSY, errno.EPERM, errno.EACCES, errno.ENOTEMPTY):
                raise
            attempt += 1
            time.sleep(0.1)


def withEnv(options: dict[str, Any]) -> Callable[[Any], Any]:
    async def provide(use: Any) -> None:
        cwd = tempfile.mkdtemp(prefix="pi-durable-env-conformance-")
        try:
            await use(NodeExecutionEnv({"cwd": cwd, **options}))
        finally:
            removeTempDir(cwd)

    return provide


# Git Bash's `ln -s` copies instead of linking unless native symlinks are enabled.
SHELL_OPTIONS: dict[str, Any] = {"shell": [gitBash, "-c"], "symlinks": False} if windows else {}

nativeCases = createEnvConformance({**SHELL_OPTIONS, "assertions": createAssertions(), "withEnv": withEnv({})})
pollingCases = createEnvConformance(
    {
        **SHELL_OPTIONS,
        "assertions": createAssertions(),
        "withEnv": withEnv({"watch": {"mode": "polling", "pollIntervalMs": 100}}),
    }
)


@pytest.mark.parametrize("case", nativeCases, ids=[case["name"] for case in nativeCases])
async def test_node_execution_env_conformance(case):
    await case["run"]()


@pytest.mark.parametrize("case", pollingCases, ids=[case["name"] for case in pollingCases])
async def test_node_execution_env_conformance_with_polling_watches(case):
    await case["run"]()


async def test_watch_limits_refuse_a_tree_over_the_directory_budget_and_stop_with_an_error_when_one_grows_past_it():
    cwd = tempfile.mkdtemp(prefix="pi-durable-env-watch-")
    try:
        env = NodeExecutionEnv({"cwd": cwd, "watch": {"maxDirectories": 3}})
        getOrThrow(await env.createDir("tree/a/b", None, context))
        getOrThrow(await env.createDir("tree/c", None, context))
        refused = await env.watch([{"path": "tree", "recursive": True}], lambda change: None, context)
        assert refused["ok"] is False
        assert refused["error"].code == "invalid"

        getOrThrow(await env.remove("tree/c", {"recursive": True}, context))
        changes: list[dict[str, Any]] = []
        watcher = getOrThrow(await env.watch([{"path": "tree", "recursive": True}], changes.append, context))
        getOrThrow(await env.createDir("tree/d", None, context))
        deadline = time.monotonic() + 3
        while not any("error" in change for change in changes) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert changes[-1]["error"].code == "invalid"
        await watcher.close(context)
    finally:
        removeTempDir(cwd)


@pytest.fixture
def tempDir() -> Iterator[Callable[[], str]]:
    dirs: list[str] = []

    def create() -> str:
        directory = tempfile.mkdtemp(prefix="pi-durable-env-readers-")
        dirs.append(directory)
        return directory

    yield create
    for directory in dirs:
        removeTempDir(directory)


async def test_readers_read_ranges_spanning_several_internal_chunks_exactly(tempDir):
    cwd = tempDir()
    data = bytes((index * 31) % 251 for index in range(int(2.5 * 1024 * 1024)))
    Path(cwd, "big.bin").write_bytes(data)
    reader = getOrThrow(await NodeExecutionEnv({"cwd": cwd}).openBinaryReader("big.bin", None, context))
    try:
        whole = getOrThrow(await reader.read(0, len(data) + 10, context))
        assert len(whole) == len(data)
        assert bytes(whole) == data
        middle = getOrThrow(await reader.read(1024 * 1024 - 3, 7, context))
        assert list(middle) == list(data[1024 * 1024 - 3 : 1024 * 1024 + 4])
    finally:
        await reader.close(context)


@pytest.mark.skipif(windows, reason="no FIFOs on Windows")
async def test_readers_refuse_a_fifo_without_waiting_for_a_writer(tempDir):
    cwd = tempDir()
    os.mkfifo(os.path.join(cwd, "pipe"))
    result = await NodeExecutionEnv({"cwd": cwd}).openBinaryReader("pipe", None, context)
    assert result["ok"] is False
    assert result["error"].code == "invalid"
