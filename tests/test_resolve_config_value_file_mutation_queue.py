"""Translated from pi's ``packages/coding-agent/test/resolve-config-value.test.ts`` and
``file-mutation-queue.test.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.core import resolve_config_value
from misaka.core.resolve_config_value import (
    clearConfigValueCache,
    resolveConfigValue,
    resolveConfigValueUncached,
)
from misaka.core.tools.edit import createEditTool
from misaka.core.tools.file_mutation_queue import withFileMutationQueue
from misaka.core.tools.write import createWriteTool
from misaka.utils.js import AbortController
from misaka.utils.shell import ShellConfig

# --- resolveConfigValue -------------------------------------------------------------------------


@pytest.fixture
def configValueCache():
    clearConfigValueCache()
    yield
    clearConfigValueCache()


def test_resolves_literals_environment_templates_and_escapes(monkeypatch: pytest.MonkeyPatch, configValueCache: None):
    monkeypatch.setenv("TEST_CONFIG_LEFT", "left")
    monkeypatch.setenv("TEST_CONFIG_RIGHT", "right")
    assert resolveConfigValue("literal-key") == "literal-key"
    assert resolveConfigValue("$TEST_CONFIG_LEFT") == "left"
    assert resolveConfigValue("$" + "{TEST_CONFIG_LEFT}_$TEST_CONFIG_RIGHT") == "left_right"
    assert resolveConfigValue("$$TEST_CONFIG_LEFT") == "$TEST_CONFIG_LEFT"
    assert resolveConfigValue("$!literal-$TEST_CONFIG_RIGHT") == "!literal-right"


def test_uses_credential_scoped_environment_before_process_env(monkeypatch: pytest.MonkeyPatch, configValueCache: None):
    monkeypatch.setenv("TEST_CONFIG_SCOPED", "process")
    assert resolveConfigValue("$TEST_CONFIG_SCOPED", {"TEST_CONFIG_SCOPED": "credential"}) == "credential"


def test_executes_shell_commands_and_trims_their_output(configValueCache: None):
    assert resolveConfigValue("!echo '  spaced-key  '") == "spaced-key"
    assert resolveConfigValue("!printf 'line1\\nline2'") == "line1\nline2"
    assert resolveConfigValue("!echo 'hello world' | tr ' ' '-'") == "hello-world"


@pytest.mark.parametrize("command", ["!exit 1", "!nonexistent-command-12345", "!printf ''"])
def test_returns_undefined_when_command_resolution_fails(command: str, configValueCache: None):
    assert resolveConfigValue(command) is None


def _countingCommand(counterFile: Path, tail: str) -> str:
    escapedPath = str(counterFile).replace("\\", "/").replace('"', '\\"')
    return f"!sh -c 'count=$(cat \"{escapedPath}\"); echo $((count + 1)) > \"{escapedPath}\"; {tail}'"


def test_caches_successful_and_failed_commands_until_explicitly_cleared(tmp_path: Path, configValueCache: None):
    counterFile = tmp_path / "counter"
    counterFile.write_text("0", encoding="utf-8")
    success = _countingCommand(counterFile, "echo value")

    assert resolveConfigValue(success) == "value"
    assert resolveConfigValue(success) == "value"
    assert counterFile.read_text(encoding="utf-8").strip() == "1"

    clearConfigValueCache()
    assert resolveConfigValue(success) == "value"
    assert counterFile.read_text(encoding="utf-8").strip() == "2"

    failure = _countingCommand(counterFile, "exit 1")
    assert resolveConfigValue(failure) is None
    assert resolveConfigValue(failure) is None
    assert counterFile.read_text(encoding="utf-8").strip() == "3"


def test_does_not_cache_environment_values(monkeypatch: pytest.MonkeyPatch, configValueCache: None):
    monkeypatch.setenv("TEST_CONFIG_DYNAMIC", "first")
    assert resolveConfigValue("$TEST_CONFIG_DYNAMIC") == "first"
    monkeypatch.setenv("TEST_CONFIG_DYNAMIC", "second")
    assert resolveConfigValue("$TEST_CONFIG_DYNAMIC") == "second"


def test_uncached_resolution_executes_a_command_on_every_call(tmp_path: Path, configValueCache: None):
    counterFile = tmp_path / "uncached-counter"
    counterFile.write_text("0", encoding="utf-8")
    command = _countingCommand(counterFile, "echo value")
    assert resolveConfigValueUncached(command) == "value"
    assert resolveConfigValueUncached(command) == "value"
    assert counterFile.read_text(encoding="utf-8").strip() == "2"


@pytest.mark.skipif(sys.platform == "win32", reason="pi's case fakes a Windows shell from a POSIX host")
def test_uses_stdin_when_the_configured_windows_shell_requires_it(
    monkeypatch: pytest.MonkeyPatch, configValueCache: None
):
    monkeypatch.setattr(
        resolve_config_value, "getShellConfig", lambda: ShellConfig(shell="/bin/bash", args=["-s"], commandTransport="stdin")
    )
    monkeypatch.setattr(resolve_config_value.os, "name", "nt")
    expansion = "$" + "{name}"
    assert resolveConfigValueUncached(f"!name='World'; echo \"Hello, {expansion}!\"") == "Hello, World!"


# --- withFileMutationQueue ----------------------------------------------------------------------


async def delay(ms: float) -> None:
    await asyncio.sleep(ms / 1000)


async def resolvesWithin(future: asyncio.Future[Any], ms: float) -> bool:
    try:
        await asyncio.wait_for(asyncio.shield(future), ms / 1000)
        return True
    except TimeoutError:
        return False


def start(awaitable: Any) -> asyncio.Task[Any]:
    """A JavaScript async call: it runs at once, up to its first suspension."""
    return asyncio.Task(awaitable, loop=asyncio.get_running_loop(), eager_start=True)


async def test_serializes_operations_for_the_same_file():
    order: list[str] = []
    path = "/tmp/file-mutation-queue-same"

    async def first() -> None:
        order.append("first:start")
        await delay(30)
        order.append("first:end")

    async def second() -> None:
        order.append("second:start")
        order.append("second:end")

    await asyncio.gather(start(withFileMutationQueue(path, first)), start(withFileMutationQueue(path, second)))
    assert order == ["first:start", "first:end", "second:start", "second:end"]


async def test_allows_different_files_to_proceed_in_parallel():
    order: list[str] = []

    async def run(name: str) -> None:
        order.append(f"{name}:start")
        await delay(30)
        order.append(f"{name}:end")

    await asyncio.gather(
        start(withFileMutationQueue("/tmp/file-mutation-queue-a", lambda: run("a"))),
        start(withFileMutationQueue("/tmp/file-mutation-queue-b", lambda: run("b"))),
    )

    assert order.index("a:start") < order.index("a:end")
    assert order.index("b:start") < order.index("b:end")
    assert order.index("b:start") < order.index("a:end")


@pytest.mark.skipif(sys.platform == "win32", reason="file symlinks need privileges on Windows")
async def test_uses_the_same_queue_for_symlink_aliases(tmp_path: Path):
    targetPath = tmp_path / "target.txt"
    symlinkPath = tmp_path / "alias.txt"
    targetPath.write_text("hello\n", encoding="utf-8")
    os.symlink(targetPath, symlinkPath)

    order: list[str] = []

    async def target() -> None:
        order.append("target:start")
        await delay(30)
        order.append("target:end")

    async def alias() -> None:
        order.append("alias:start")
        order.append("alias:end")

    await asyncio.gather(
        start(withFileMutationQueue(str(targetPath), target)),
        start(withFileMutationQueue(str(symlinkPath), alias)),
    )

    assert order == ["target:start", "target:end", "alias:start", "alias:end"]


async def _access(path: str) -> None:
    if not os.path.exists(path):
        raise FileNotFoundError(path)


async def _readFile(path: str) -> bytes:
    return Path(path).read_bytes()


async def _writeFile(path: str, content: str) -> None:
    Path(path).write_text(content, encoding="utf-8", newline="")


def slowEditOperations() -> SimpleNamespace:
    async def readFile(path: str) -> bytes:
        buffer = await _readFile(path)
        await delay(30)
        return buffer

    async def writeFile(path: str, content: str) -> None:
        await delay(30)
        await _writeFile(path, content)

    return SimpleNamespace(access=_access, readFile=readFile, writeFile=writeFile)


async def test_preserves_both_parallel_edits_on_the_same_file(tmp_path: Path):
    filePath = tmp_path / "parallel-edit.txt"
    filePath.write_text("alpha\nbeta\ngamma\n", encoding="utf-8", newline="")

    editTool = createEditTool(str(tmp_path), {"operations": slowEditOperations()})

    await asyncio.gather(
        start(editTool.execute("call-1", {"path": str(filePath), "edits": [{"oldText": "alpha", "newText": "ALPHA"}]}, None, None)),
        start(editTool.execute("call-2", {"path": str(filePath), "edits": [{"oldText": "beta", "newText": "BETA"}]}, None, None)),
    )

    assert filePath.read_text(encoding="utf-8") == "ALPHA\nBETA\ngamma\n"


async def test_shares_the_queue_between_edit_and_write(tmp_path: Path):
    filePath = tmp_path / "mixed.txt"
    filePath.write_text("original\n", encoding="utf-8", newline="")

    editTool = createEditTool(str(tmp_path), {"operations": slowEditOperations()})

    async def mkdir(_path: str) -> None:
        pass

    async def writeFile(path: str, content: str) -> None:
        await delay(10)
        await _writeFile(path, content)

    writeTool = createWriteTool(str(tmp_path), {"operations": SimpleNamespace(mkdir=mkdir, writeFile=writeFile)})

    editPromise = start(editTool.execute(
        "call-1", {"path": str(filePath), "edits": [{"oldText": "original", "newText": "edited"}]}, None, None
    ))
    await delay(5)
    writePromise = start(writeTool.execute("call-2", {"path": str(filePath), "content": "replacement\n"}, None, None))

    await asyncio.gather(editPromise, writePromise)

    assert filePath.read_text(encoding="utf-8") == "replacement\n"


async def test_keeps_write_queue_locked_while_an_aborted_write_is_still_in_flight(tmp_path: Path):
    loop = asyncio.get_running_loop()
    filePath = tmp_path / "abort-write.txt"
    firstWriteStarted: asyncio.Future[None] = loop.create_future()
    finishFirstWrite: asyncio.Future[None] = loop.create_future()
    secondWriteStarted: asyncio.Future[None] = loop.create_future()
    state = {"firstWriteSettled": False}

    async def mkdir(_path: str) -> None:
        pass

    async def writeFile(path: str, content: str) -> None:
        if content == "first\n":
            firstWriteStarted.set_result(None)
            await finishFirstWrite
            await _writeFile(path, content)
            state["firstWriteSettled"] = True
            return
        if content == "second\n":
            assert state["firstWriteSettled"] is True
            secondWriteStarted.set_result(None)
        await _writeFile(path, content)

    writeTool = createWriteTool(str(tmp_path), {"operations": SimpleNamespace(mkdir=mkdir, writeFile=writeFile)})

    controller = AbortController()
    firstWrite = start(writeTool.execute("call-1", {"path": str(filePath), "content": "first\n"}, controller.signal, None))
    await firstWriteStarted
    controller.abort()

    secondWrite = start(writeTool.execute("call-2", {"path": str(filePath), "content": "second\n"}, None, None))
    assert await resolvesWithin(secondWriteStarted, 20) is False

    finishFirstWrite.set_result(None)
    with pytest.raises(Exception, match="Operation aborted"):
        await firstWrite
    await secondWrite

    assert filePath.read_text(encoding="utf-8") == "second\n"


async def test_keeps_edit_queue_locked_while_an_aborted_edit_write_is_still_in_flight(tmp_path: Path):
    loop = asyncio.get_running_loop()
    filePath = tmp_path / "abort-edit.txt"
    filePath.write_text("alpha\nbeta\n", encoding="utf-8", newline="")
    firstWriteStarted: asyncio.Future[None] = loop.create_future()
    finishFirstWrite: asyncio.Future[None] = loop.create_future()
    secondWriteStarted: asyncio.Future[None] = loop.create_future()
    state = {"firstWriteSettled": False}

    async def writeFile(path: str, content: str) -> None:
        if content == "ALPHA\nbeta\n":
            firstWriteStarted.set_result(None)
            await finishFirstWrite
            await _writeFile(path, content)
            state["firstWriteSettled"] = True
            return
        if content in ("ALPHA\nBETA\n", "alpha\nBETA\n"):
            assert state["firstWriteSettled"] is True
            secondWriteStarted.set_result(None)
        await _writeFile(path, content)

    editTool = createEditTool(
        str(tmp_path), {"operations": SimpleNamespace(access=_access, readFile=_readFile, writeFile=writeFile)}
    )

    controller = AbortController()
    firstEdit = start(editTool.execute(
        "call-1", {"path": str(filePath), "edits": [{"oldText": "alpha", "newText": "ALPHA"}]}, controller.signal, None
    ))
    await firstWriteStarted
    controller.abort()

    secondEdit = start(editTool.execute(
        "call-2", {"path": str(filePath), "edits": [{"oldText": "beta", "newText": "BETA"}]}, None, None
    ))
    assert await resolvesWithin(secondWriteStarted, 20) is False

    finishFirstWrite.set_result(None)
    with pytest.raises(Exception, match="Operation aborted"):
        await firstEdit
    await secondEdit

    assert filePath.read_text(encoding="utf-8") == "ALPHA\nBETA\n"
