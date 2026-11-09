"""pi durable's ``test/tools.test.ts``.

Ported from packages/agent/test/harness/tools.test.ts and adapted to tool registrations: tools take the environment
from ``api.env``, stream through ``api.output()``, and report notices as diagnostics instead of content text.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from misaka.chord.context import BACKGROUND_CONTEXT, withAbortSignal
from misaka.durable.env import ExecutionError, err, getOrThrow
from misaka.durable.env.node import NodeExecutionEnv
from misaka.durable.tools import (
    createBashTool,
    createEditTool,
    createPowerShellTool,
    createReadTool,
    createWriteTool,
)
from misaka.durable.tools.file_mutation_queue import withFileMutationQueue
from misaka.durable.tools.image import detectSupportedImageMimeType
from misaka.durable.truncate import DEFAULT_MAX_LINES
from misaka.utils.js import AbortController, jsonStringify
from tests.durable_support import toMatchObject
from tests.task_support import deferred

tempDirs: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def cleanTempDirs() -> Iterator[None]:
    yield
    for directory in tempDirs:
        shutil.rmtree(directory, ignore_errors=True)


def createTempDir() -> str:
    directory = tempfile.mkdtemp(prefix="pi-durable-tools-")
    tempDirs.append(directory)
    return directory


def createEnv() -> NodeExecutionEnv:
    return NodeExecutionEnv({"cwd": createTempDir()})


def symlink(target: str, path: str) -> None:
    try:
        os.symlink(target, path)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"symbolic links are not available here: {error}")


def applyPatch(source: str, patch: str) -> str:
    """Apply a single-file unified diff to ``source``. The port of jsdiff holds the producing half of the library only;
    this strict applier is what the test needs to see that the patch it returns reproduces the edit."""
    sourceLines = source.splitlines(keepends=True)
    result: list[str] = []
    position = 0
    lines = patch.split("\n")
    index = 0
    while index < len(lines):
        header = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", lines[index])
        index += 1
        if header is None:
            continue
        oldStart = int(header.group(1))
        oldCount = 1 if header.group(2) is None else int(header.group(2))
        newCount = 1 if header.group(4) is None else int(header.group(4))
        seenOld = seenNew = 0
        start = oldStart - 1 if oldCount > 0 else oldStart
        result.extend(sourceLines[position:start])
        position = start
        while index < len(lines) and not lines[index].startswith("@@") and lines[index] != "":
            tag, text = lines[index][0], lines[index][1:]
            if tag == " ":
                assert sourceLines[position].rstrip("\r\n") == text
                result.append(sourceLines[position])
                position += 1
                seenOld += 1
                seenNew += 1
            elif tag == "-":
                assert sourceLines[position].rstrip("\r\n") == text
                position += 1
                seenOld += 1
            elif tag == "+":
                result.append(text + "\n")
                seenNew += 1
            index += 1
        # jsdiff's ``applyPatch`` reads exactly the lines a hunk header announces and throws on a mismatch.
        assert (seenOld, seenNew) == (oldCount, newCount), f"hunk header counts {oldCount},{newCount} but the hunk has {seenOld},{seenNew}"
    result.extend(sourceLines[position:])
    return "".join(result)


def fakeApi(env: Any) -> SimpleNamespace:
    """A minimal execution API: the environment, collected output and diagnostics, and nothing durable."""
    output: list[str] = []
    diagnostics: list[dict[str, Any]] = []

    async def details(*_: Any) -> None:
        return None

    # No model: `read` treats it as one that sees images.
    async def agent(*_: Any) -> dict[str, Any]:
        return {}

    # MISAKA fork: `outputWindow` is None here because an absent attribute does not read as `undefined` in Python.
    api = SimpleNamespace(
        taskId=1,
        conversationId=1,
        callId="call",
        env=env,
        output=lambda chunk, skipped=None: output.append(chunk if isinstance(chunk, str) else chunk.decode()),
        retainedOutput=lambda: {"text": "".join(output), "truncated": False},
        outputWindow=None,
        diagnostic=lambda diagnostic: diagnostics.append(diagnostic),
        details=details,
        agent=agent,
    )
    return SimpleNamespace(api=api, output=output, diagnostics=diagnostics)


async def run(tool: dict[str, Any], args: Any, env: Any, context: Any = BACKGROUND_CONTEXT) -> dict[str, Any]:
    fake = fakeApi(env)
    result = await tool["execute"](args, fake.api, context)
    return {**result, "streamed": fake.output, "reported": fake.diagnostics}


async def runFailing(tool: dict[str, Any], args: Any, env: Any) -> SimpleNamespace:
    """Run a tool expected to throw; returns the error with what it streamed and reported first."""
    fake = fakeApi(env)
    try:
        await tool["execute"](args, fake.api, BACKGROUND_CONTEXT)
    except Exception as error:  # noqa: BLE001 - the error is the result
        return SimpleNamespace(error=error, streamed=fake.output, reported=fake.diagnostics)
    raise AssertionError("Expected the tool to throw")


def textOutput(result: dict[str, Any]) -> str:
    return "\n".join(part["text"] for part in result.get("output") or [] if part["type"] == "text")


def diagnosticText(result: dict[str, Any]) -> str:
    return "\n".join(diagnostic["message"] for diagnostic in result.get("diagnostics") or [])


async def delay(ms: int) -> None:
    await asyncio.sleep(ms / 1000)


class SlowReadExecutionEnv(NodeExecutionEnv):
    async def readTextFile(self, path: str, context: Any) -> Any:
        await delay(20)
        return await super().readTextFile(path, context)


class BlockingWriteExecutionEnv(NodeExecutionEnv):
    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.firstWriteStarted = deferred()
        self.finishFirstWrite = deferred()
        self.secondWriteStarted = False

    async def writeFile(self, path: str, content: Any, context: Any) -> Any:
        if content == "first\n":
            self.firstWriteStarted.resolve()
            await self.finishFirstWrite.promise
        elif content == "second\n":
            self.secondWriteStarted = True
        return await super().writeFile(path, content, context)


class BlockingEditExecutionEnv(NodeExecutionEnv):
    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self.firstEditWriteStarted = deferred()
        self.finishFirstEditWrite = deferred()
        self.firstEditWriteSettled = False
        self.secondEditWriteStarted = False

    async def writeFile(self, path: str, content: Any, context: Any) -> Any:
        if content == "ALPHA\nbeta\n":
            self.firstEditWriteStarted.resolve()
            await self.finishFirstEditWrite.promise
            result = await super().writeFile(path, content, BACKGROUND_CONTEXT)
            self.firstEditWriteSettled = True
            return result
        if content in ("ALPHA\nBETA\n", "alpha\nBETA\n"):
            self.secondEditWriteStarted = True
        return await super().writeFile(path, content, context)


TRUNCATED_OUTPUT_LINES = DEFAULT_MAX_LINES + 1


class TimeoutOutputExecutionEnv(NodeExecutionEnv):
    async def exec(self, command: str | list[str], options: dict[str, Any] | None, context: Any) -> Any:
        output = "\n".join(f"line-{index + 1}" for index in range(TRUNCATED_OUTPUT_LINES)) + "\n"
        spillPath = getOrThrow(await self.createTempFile({"prefix": "timeout-", "suffix": ".log"}, context))
        getOrThrow(await self.writeFile(spillPath, output, context))
        onOutput = (options or {}).get("onOutput")
        if onOutput is not None:
            onOutput(output, context, {"stream": "stdout"})
        error = ExecutionError("timeout", f"timeout:{(options or {}).get('timeout')}")
        error.spillPath = spillPath
        return err(error)


def test_tool_parameters_serialize_byte_for_byte_like_pi():
    # tests/fixtures/durable_tool_schemas.txt is JSON.stringify(tool.parameters) of pi-durable 1.1.0, one tool per line:
    # TypeBox writes `required` before `properties`, and the request bytes depend on it.
    fixture = Path(__file__).parent / "fixtures" / "durable_tool_schemas.txt"
    expected = fixture.read_text(encoding="utf-8").splitlines()
    tools = [createReadTool(), createWriteTool(), createEditTool(), createBashTool(), createPowerShellTool()]
    assert [f"{tool['name']} {jsonStringify(tool['parameters'])}" for tool in tools] == expected


async def test_fail_with_an_ordinary_error_when_no_environment_is_configured():
    with pytest.raises(Exception, match="No execution environment"):
        await run(createReadTool(), {"path": "x"}, None)


@pytest.mark.parametrize("signature", ["GIF87a", "GIF89a"])
def test_read_detects_the_complete_signature(signature):
    assert detectSupportedImageMimeType(signature.encode("ascii")) == "image/gif"


async def test_read_reads_text_with_offsets_and_limits_and_reports_continuation_as_a_diagnostic():
    env = createEnv()
    getOrThrow(
        await env.writeFile("test.txt", "\n".join(f"Line {index + 1}" for index in range(100)), BACKGROUND_CONTEXT)
    )
    result = await run(createReadTool(), {"path": "test.txt", "offset": 41, "limit": 20}, env)
    output = textOutput(result)
    assert "Line 40" not in output
    assert "Line 41" in output
    assert "Line 60" in output
    assert "Line 61" not in output
    assert "more lines" not in output
    assert diagnosticText(result) == "40 more lines in file. Use offset=61 to continue."


async def test_read_truncates_large_text_by_line_count():
    env = createEnv()
    getOrThrow(
        await env.writeFile("large.txt", "\n".join(f"Line {index + 1}" for index in range(2500)), BACKGROUND_CONTEXT)
    )
    result = await run(createReadTool(), {"path": "large.txt"}, env)
    assert diagnosticText(result) == "Showing lines 1-2000 of 2500. Use offset=2001 to continue."
    assert result["diagnostics"][0]["code"] == "truncated"
    toMatchObject(
        result["details"]["truncation"],
        {"truncated": True, "truncatedBy": "lines", "totalLines": 2500, "outputLines": 2000},
    )


async def test_read_does_not_count_a_trailing_newline_as_an_extra_line_at_the_truncation_limit():
    env = createEnv()
    getOrThrow(await env.writeFile("exact.txt", "\n".join("x" for _ in range(2000)) + "\n", BACKGROUND_CONTEXT))
    result = await run(createReadTool(), {"path": "exact.txt"}, env)
    assert result.get("details") is None
    assert result["diagnostics"] == []


async def test_read_shows_the_start_of_a_line_longer_than_the_byte_limit():
    env = createEnv()
    getOrThrow(await env.writeFile("long.txt", "é" * 40_000 + "\nnext\n", BACKGROUND_CONTEXT))
    result = await run(createReadTool(), {"path": "long.txt"}, env)
    text = textOutput(result)
    # Two-byte characters: the cut lands on a character boundary at or below the limit.
    assert text == "é" * 25_600
    assert diagnosticText(result) == (
        "Line 1 is 78.1KB, exceeds the 50.0KB limit; showing its first 50.0KB. "
        "Use bash: sed -n '1p' long.txt | tail -c +51201"
    )
    toMatchObject(
        result["details"]["truncation"],
        {"truncated": True, "firstLineExceedsLimit": True, "outputBytes": 51_200, "outputLines": 1},
    )
    assert "content" not in result["details"]["truncation"]


async def test_read_rejects_offsets_beyond_the_file():
    env = createEnv()
    getOrThrow(await env.writeFile("short.txt", "one\ntwo\nthree", BACKGROUND_CONTEXT))
    with pytest.raises(Exception, match=re.escape("Offset 100 is beyond end of file (3 lines total)")):
        await run(createReadTool(), {"path": "short.txt", "offset": 100}, env)


async def test_read_reads_a_log_that_grows_while_it_is_read():
    env = createEnv()
    getOrThrow(await env.writeFile("app.log", "one\ntwo\n", BACKGROUND_CONTEXT))
    logPath = os.path.join(env.cwd, "app.log")

    def appendLine() -> None:
        with open(logPath, "a", encoding="utf-8", newline="") as file:
            file.write("more\n")

    # Every read appends, as a busy writer would; the scanned lines stay as they were.
    class GrowingEnv(NodeExecutionEnv):
        async def openBinaryReader(self, path: str, options: Any, context: Any) -> Any:
            opened = await env.openBinaryReader(path, options, context)
            if not opened["ok"]:
                return opened
            reader = opened["value"]
            read = reader.read

            async def growingRead(position: Any, length: Any, readContext: Any) -> Any:
                appendLine()
                return await read(position, length, readContext)

            reader.read = growingRead
            return opened

    growing = GrowingEnv({"cwd": env.cwd})
    result = await run(createReadTool(), {"path": "app.log", "limit": 2}, growing)
    assert textOutput(result) == "one\ntwo"


async def test_read_returns_images_by_content_as_one_image_block():
    env = createEnv()
    data = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAX+XDSwAAAABJRU5ErkJggg=="
    getOrThrow(await env.writeFile("image.txt", base64.b64decode(data), BACKGROUND_CONTEXT))
    result = await run(createReadTool(), {"path": "image.txt"}, env)
    assert result["output"] == [{"type": "image", "data": data, "mimeType": "image/png"}]
    assert "isError" not in result
    assert diagnosticText(result) == "Read image file [image/png]."


async def test_write_writes_files_and_creates_parent_directories():
    env = createEnv()
    result = await run(createWriteTool(), {"path": "nested/dir/file.txt", "content": "hello"}, env)
    assert textOutput(result) == "Successfully wrote to nested/dir/file.txt"
    assert getOrThrow(await env.readTextFile("nested/dir/file.txt", BACKGROUND_CONTEXT)) == "hello"


async def test_write_keeps_the_mutation_queue_locked_until_an_aborted_write_settles():
    env = BlockingWriteExecutionEnv({"cwd": createTempDir()})
    tool = createWriteTool()
    controller = AbortController()
    firstWrite = asyncio.ensure_future(
        run(tool, {"path": "file.txt", "content": "first\n"}, env, withAbortSignal(controller.signal, BACKGROUND_CONTEXT))
    )
    await env.firstWriteStarted.promise
    controller.abort()
    secondWrite = asyncio.ensure_future(run(tool, {"path": "file.txt", "content": "second\n"}, env))
    await delay(20)
    assert env.secondWriteStarted is False
    env.finishFirstWrite.resolve()
    with pytest.raises(Exception):  # noqa: B017
        await firstWrite
    await secondWrite
    assert getOrThrow(await env.readTextFile("file.txt", BACKGROUND_CONTEXT)) == "second\n"


async def test_edit_applies_disjoint_edits_and_returns_both_diff_formats():
    env = createEnv()
    original = "alpha\nbeta\ngamma\ndelta\n"
    getOrThrow(await env.writeFile("edit.txt", original, BACKGROUND_CONTEXT))
    result = await run(
        createEditTool(),
        {
            "path": "edit.txt",
            "edits": [
                {"oldText": "alpha\n", "newText": "ALPHA\n"},
                {"oldText": "gamma\n", "newText": "GAMMA\n"},
            ],
        },
        env,
    )
    details = result["details"]
    assert textOutput(result) == "Successfully replaced 2 block(s) in edit.txt."
    assert "ALPHA" in details["diff"]
    assert "GAMMA" in details["diff"]
    assert applyPatch(original, details["patch"]) == "ALPHA\nbeta\nGAMMA\ndelta\n"
    assert getOrThrow(await env.readTextFile("edit.txt", BACKGROUND_CONTEXT)) == "ALPHA\nbeta\nGAMMA\ndelta\n"


def test_edit_repairs_edits_sent_as_a_json_string_a_single_object_or_top_level_old_text_new_text_without_mutating_them():
    prepare = createEditTool()["prepareArguments"]
    edit = {"oldText": "a", "newText": "b"}
    asString = {"path": "f", "edits": jsonStringify([edit])}
    assert prepare(asString) == {"path": "f", "edits": [edit]}
    assert asString["edits"] == jsonStringify([edit])
    assert prepare({"path": "f", "edits": jsonStringify(edit)}) == {"path": "f", "edits": [edit]}
    assert prepare({"path": "f", "edits": edit}) == {"path": "f", "edits": [edit]}
    assert prepare({"path": "f", "edits": [edit], "oldText": "c", "newText": "d"}) == {
        "path": "f",
        "edits": [edit, {"oldText": "c", "newText": "d"}],
    }
    assert prepare({"path": "f", "edits": "not json"}) == {"path": "f", "edits": "not json"}


async def test_edit_matches_all_edits_against_the_original_and_rejects_overlaps():
    env = createEnv()
    getOrThrow(await env.writeFile("edit.txt", "one\ntwo\nthree\n", BACKGROUND_CONTEXT))
    with pytest.raises(Exception, match="overlap"):
        await run(
            createEditTool(),
            {
                "path": "edit.txt",
                "edits": [
                    {"oldText": "one\ntwo\n", "newText": "ONE\nTWO\n"},
                    {"oldText": "two\nthree\n", "newText": "TWO\nTHREE\n"},
                ],
            },
            env,
        )
    assert getOrThrow(await env.readTextFile("edit.txt", BACKGROUND_CONTEXT)) == "one\ntwo\nthree\n"


async def test_edit_rejects_missing_and_duplicate_target_text():
    env = createEnv()
    getOrThrow(await env.writeFile("edit.txt", "foo foo foo", BACKGROUND_CONTEXT))
    tool = createEditTool()
    with pytest.raises(Exception, match="Could not find the exact text"):
        await run(tool, {"path": "edit.txt", "edits": [{"oldText": "bar", "newText": "baz"}]}, env)
    with pytest.raises(Exception, match="Found 3 occurrences"):
        await run(tool, {"path": "edit.txt", "edits": [{"oldText": "foo", "newText": "bar"}]}, env)


async def test_edit_keeps_the_mutation_queue_locked_until_an_aborted_edit_write_settles():
    env = BlockingEditExecutionEnv({"cwd": createTempDir()})
    getOrThrow(await env.writeFile("file.txt", "alpha\nbeta\n", BACKGROUND_CONTEXT))
    tool = createEditTool()
    controller = AbortController()
    firstEdit = asyncio.ensure_future(
        run(
            tool,
            {"path": "file.txt", "edits": [{"oldText": "alpha", "newText": "ALPHA"}]},
            env,
            withAbortSignal(controller.signal, BACKGROUND_CONTEXT),
        )
    )
    await env.firstEditWriteStarted.promise
    controller.abort()
    secondEdit = asyncio.ensure_future(
        run(tool, {"path": "file.txt", "edits": [{"oldText": "beta", "newText": "BETA"}]}, env)
    )
    await delay(20)
    assert env.secondEditWriteStarted is False
    env.finishFirstEditWrite.resolve()
    with pytest.raises(Exception, match="Operation aborted"):
        await firstEdit
    await secondEdit
    assert env.firstEditWriteSettled is True
    assert getOrThrow(await env.readTextFile("file.txt", BACKGROUND_CONTEXT)) == "ALPHA\nBETA\n"


async def test_edit_serializes_concurrent_edits_through_canonical_and_symlink_paths():
    env = SlowReadExecutionEnv({"cwd": createTempDir()})
    getOrThrow(await env.writeFile("target.txt", "alpha\nbeta\ngamma\n", BACKGROUND_CONTEXT))
    symlink("target.txt", f"{env.cwd}/link.txt")
    tool = createEditTool()
    await asyncio.gather(
        run(tool, {"path": "target.txt", "edits": [{"oldText": "alpha", "newText": "ALPHA"}]}, env),
        run(tool, {"path": "link.txt", "edits": [{"oldText": "beta", "newText": "BETA"}]}, env),
    )
    assert getOrThrow(await env.readTextFile("target.txt", BACKGROUND_CONTEXT)) == "ALPHA\nBETA\ngamma\n"


async def test_edit_serializes_edits_of_one_file_across_environment_objects_of_one_file_system():
    directory = createTempDir()
    first = SlowReadExecutionEnv({"cwd": directory})
    second = SlowReadExecutionEnv({"cwd": directory})
    getOrThrow(await first.writeFile("file.txt", "alpha\nbeta\n", BACKGROUND_CONTEXT))
    tool = createEditTool()
    await asyncio.gather(
        run(tool, {"path": "file.txt", "edits": [{"oldText": "alpha", "newText": "ALPHA"}]}, first),
        run(tool, {"path": "file.txt", "edits": [{"oldText": "beta", "newText": "BETA"}]}, second),
    )
    assert getOrThrow(await first.readTextFile("file.txt", BACKGROUND_CONTEXT)) == "ALPHA\nBETA\n"


async def test_edit_serializes_a_new_file_created_through_a_symlinked_directory_with_its_canonical_path():
    env = BlockingWriteExecutionEnv({"cwd": createTempDir()})
    os.mkdir(f"{env.cwd}/real")
    symlink(f"{env.cwd}/real", f"{env.cwd}/link")
    tool = createWriteTool()
    first = asyncio.ensure_future(run(tool, {"path": "link/new.txt", "content": "first\n"}, env))
    await env.firstWriteStarted.promise
    second = asyncio.ensure_future(run(tool, {"path": "real/new.txt", "content": "second\n"}, env))
    await delay(20)
    assert env.secondWriteStarted is False
    env.finishFirstWrite.resolve()
    await asyncio.gather(first, second)
    assert getOrThrow(await env.readTextFile("real/new.txt", BACKGROUND_CONTEXT)) == "second\n"


@pytest.mark.skipif(sys.platform == "win32", reason="a backslash separates path segments on Windows")
async def test_edit_keys_a_missing_file_whose_name_contains_a_backslash_like_the_created_file():
    env = createEnv()
    created = deferred()
    release = deferred()

    async def create() -> None:
        getOrThrow(await env.writeFile("a\\b.txt", "first\n", BACKGROUND_CONTEXT))
        created.resolve()
        await release.promise

    first = asyncio.ensure_future(withFileMutationQueue(env, "a\\b.txt", create, BACKGROUND_CONTEXT))
    await created.promise
    entered = False

    async def enter() -> None:
        nonlocal entered
        entered = True

    second = asyncio.ensure_future(withFileMutationQueue(env, "a\\b.txt", enter, BACKGROUND_CONTEXT))
    await delay(20)
    assert entered is False
    release.resolve()
    await asyncio.gather(first, second)
    assert entered is True


async def test_edit_does_not_serialize_the_same_path_on_different_file_systems():
    class OtherFileSystem(BlockingWriteExecutionEnv):
        def __init__(self, options: dict[str, Any]) -> None:
            super().__init__(options)
            self.id = "other"

    directory = createTempDir()
    local = BlockingWriteExecutionEnv({"cwd": directory})
    other = OtherFileSystem({"cwd": directory})
    tool = createWriteTool()
    blocked = asyncio.ensure_future(run(tool, {"path": "file.txt", "content": "first\n"}, local))
    await local.firstWriteStarted.promise
    await run(tool, {"path": "file.txt", "content": "second\n"}, other)
    assert other.secondWriteStarted is True
    local.finishFirstWrite.resolve()
    await blocked


async def test_edit_edits_regular_files_through_symlinks():
    env = createEnv()
    getOrThrow(await env.writeFile("target.txt", "before\n", BACKGROUND_CONTEXT))
    symlink("target.txt", f"{env.cwd}/link.txt")
    await run(createEditTool(), {"path": "link.txt", "edits": [{"oldText": "before", "newText": "after"}]}, env)
    assert getOrThrow(await env.readTextFile("target.txt", BACKGROUND_CONTEXT)) == "after\n"


async def test_edit_preserves_bom_and_crlf_line_endings():
    env = createEnv()
    getOrThrow(await env.writeFile("edit.txt", "﻿one\r\ntwo\r\n", BACKGROUND_CONTEXT))
    await run(createEditTool(), {"path": "edit.txt", "edits": [{"oldText": "two", "newText": "TWO"}]}, env)
    assert getOrThrow(await env.readTextFile("edit.txt", BACKGROUND_CONTEXT)) == "﻿one\r\nTWO\r\n"


def programsEnv(installed: list[str], output: str, exitCode: int = 0) -> SimpleNamespace:
    """An environment where only the listed programs exist; it records each command and prints ``output``."""
    commands: list[str | list[str]] = []

    class ProgramsEnv(NodeExecutionEnv):
        async def exec(self, command: str | list[str], options: dict[str, Any] | None, context: Any) -> Any:
            commands.append(command)
            if isinstance(command, str) or command[0] not in installed:
                return err(ExecutionError("spawn_error", f"spawn {command[0]} ENOENT"))
            onOutput = (options or {}).get("onOutput")
            if onOutput is not None:
                onOutput(output, context, {"stream": "stdout"})
            return {"ok": True, "value": {"exitCode": exitCode}}

    return SimpleNamespace(env=ProgramsEnv({"cwd": createTempDir()}), commands=commands)


async def test_powershell_runs_the_command_with_pwsh_as_one_argument_forcing_utf_8_output():
    programs = programsEnv(["pwsh"], "héllo\n")
    result = await run(createPowerShellTool(), {"command": "Write-Output 'héllo'"}, programs.env)
    assert "".join(result["streamed"]) == "héllo\n"
    assert programs.commands == [
        [
            "pwsh",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {}\nWrite-Output 'héllo'",
        ]
    ]


async def test_powershell_falls_back_to_windows_powershell_and_reports_the_last_start_failure():
    windowsOnly = programsEnv(["powershell"], "ok")
    result = await run(createPowerShellTool({"commandPrefix": "$x = 1"}), {"command": "$x"}, windowsOnly.env)
    assert "".join(result["streamed"]) == "ok"
    assert [command[0] for command in windowsOnly.commands] == ["pwsh", "powershell"]
    assert re.search(r"\n\$x = 1\n\$x$", str(windowsOnly.commands[1][-1]))

    none = programsEnv([], "")
    failed = await runFailing(createPowerShellTool(), {"command": "1"}, none.env)
    assert str(failed.error) == "spawn powershell ENOENT"


async def test_powershell_answers_a_nonzero_exit_with_an_error_result_carrying_the_exit_code():
    programs = programsEnv(["pwsh"], "partial", 3)
    result = await run(createPowerShellTool(), {"command": "exit 3"}, programs.env)
    toMatchObject(
        result,
        {
            "isError": True,
            "structuredOutput": {"output": "partial", "truncated": False, "exitCode": 3},
            "diagnostics": [{"severity": "error", "code": "exit_code", "message": "Command exited with code 3"}],
        },
    )
    assert "".join(result["streamed"]) == "partial"


@pytest.mark.skipif(sys.platform != "win32", reason="real PowerShell runs on Windows")
async def test_powershell_runs_real_powershell_with_utf_8_output():
    result = await run(
        createPowerShellTool(), {"command": "Write-Output ('h' + [char]0xe9 + 'llo'); exit 0"}, createEnv()
    )
    assert "".join(result["streamed"]).strip() == "héllo"


async def test_bash_passes_the_retained_window_to_the_environment_and_forwards_what_it_skipped():
    window = {"maxBytes": 4, "maxLines": 1, "minIntervalMs": 100, "bytesPerSecond": 1024}
    skipped = {"bytes": 6, "newlines": 2, "endsWithNewline": True}
    received: dict[str, Any] = {}

    class SkippingEnv(NodeExecutionEnv):
        async def exec(self, _command: str | list[str], options: dict[str, Any] | None, context: Any) -> Any:
            received["window"] = (options or {}).get("window")
            (options or {})["onOutput"]("tail\n", context, {"stream": "stdout", "skipped": skipped})
            return {"ok": True, "value": {"exitCode": 0}}

    calls: list[tuple[str, Any]] = []
    api = SimpleNamespace(
        **{
            **vars(fakeApi(SkippingEnv({"cwd": createTempDir()})).api),
            "outputWindow": window,
            "output": lambda chunk, skip=None: calls.append((str(chunk), skip)),
        }
    )
    await createBashTool()["execute"]({"command": "anything"}, api, BACKGROUND_CONTEXT)
    assert received["window"] == window
    assert calls == [("tail\n", skipped)]


async def test_bash_streams_combined_stdout_and_stderr_returns_no_output_of_its_own_and_repeats_it_with_the_exit_code():
    result = await run(createBashTool(), {"command": "printf out; printf err >&2"}, createEnv())
    assert "out" in "".join(result["streamed"])
    assert "err" in "".join(result["streamed"])
    assert "output" not in result
    assert result["structuredOutput"] == {"output": "".join(result["streamed"]), "truncated": False, "exitCode": 0}


async def test_bash_answers_a_nonzero_exit_with_an_error_result_and_throws_on_a_timeout_after_streaming_the_output():
    env = createEnv()
    tool = createBashTool()
    failed = await run(tool, {"command": "printf failed; exit 7"}, env)
    toMatchObject(
        failed,
        {
            "isError": True,
            "structuredOutput": {"output": "failed", "truncated": False, "exitCode": 7},
            "diagnostics": [{"code": "exit_code", "message": "Command exited with code 7"}],
        },
    )
    assert "".join(failed["streamed"]) == "failed"
    slow = await runFailing(tool, {"command": "sleep 2", "timeout": 0.01}, env)
    assert str(slow.error) == "Command timed out after 0.01 seconds"


async def test_bash_reports_the_spill_of_a_command_that_times_out():
    env = TimeoutOutputExecutionEnv({"cwd": createTempDir()})
    failed = await runFailing(createBashTool(), {"command": "emit-output-then-time-out", "timeout": 0.05}, env)
    assert str(failed.error) == "Command timed out after 0.05 seconds"
    found = re.match(r"^Full output: (.+)$", failed.reported[0]["message"])
    assert found is not None
    fullOutput = getOrThrow(await env.readTextFile(found.group(1), BACKGROUND_CONTEXT))
    assert "line-1\nline-2" in fullOutput
    assert f"line-{DEFAULT_MAX_LINES}\nline-{TRUNCATED_OUTPUT_LINES}" in fullOutput


async def test_bash_prepares_command_cwd_and_an_explicit_environment_with_the_calls_api():
    env = NodeExecutionEnv({"cwd": createTempDir(), "shellEnv": {"PI_BASH_PREPARE_INHERITED": "inherited"}})
    getOrThrow(await env.createDir("workspace", None, BACKGROUND_CONTEXT))
    workspace = f"{env.cwd}/workspace"
    controller = AbortController()
    received: dict[str, Any] = {}

    async def prepare(execution: dict[str, Any], api: Any, callContext: Any) -> None:
        received["env"] = api.env
        received["signal"] = callContext.abortSignal
        execution["cwd"] = workspace
        execution["env"] = {"PI_BASH_PREPARE_EXPLICIT": "explicit"}
        execution["inheritEnv"] = False
        execution["command"] += (
            "\n: > prepared-cwd\nprintf '%s:%s:%s' \"$prefix\" \"${PI_BASH_PREPARE_INHERITED-}\" \"$PI_BASH_PREPARE_EXPLICIT\""
        )
        # Git Bash on Windows reports $PWD as an MSYS path, so only POSIX compares it.
        if sys.platform != "win32":
            execution["command"] += "\nprintf ':%s' \"$PWD\""

    tool = createBashTool({"commandPrefix": "prefix=ready", "prepare": prepare})
    result = await run(tool, {"command": ":"}, env, withAbortSignal(controller.signal, BACKGROUND_CONTEXT))
    assert received["env"] is env
    assert received["signal"] is controller.signal
    pwd = "" if sys.platform == "win32" else f":{getOrThrow(await env.canonicalPath(workspace, BACKGROUND_CONTEXT))}"
    assert "".join(result["streamed"]) == f"ready::explicit{pwd}"
    assert getOrThrow(await env.exists(f"{workspace}/prepared-cwd", BACKGROUND_CONTEXT)) is True


async def test_bash_supports_command_prefixes():
    result = await run(createBashTool({"commandPrefix": "value=hello"}), {"command": "printf $value"}, createEnv())
    assert "".join(result["streamed"]) == "hello"


async def test_bash_streams_every_byte_and_spills_complete_output_beyond_the_default_limits():
    env = createEnv()
    result = await run(
        createBashTool(), {"command": "i=1; while [ $i -le 3000 ]; do echo line-$i; i=$((i + 1)); done"}, env
    )
    expected = "".join(f"line-{index + 1}\n" for index in range(3000))
    assert "".join(result["streamed"]) == expected
    found = re.match(r"^Full output: (.+)$", result["reported"][0]["message"])
    assert found is not None
    assert getOrThrow(await env.readTextFile(found.group(1), BACKGROUND_CONTEXT)) == expected


async def test_bash_does_not_spill_output_within_the_limits():
    result = await run(createBashTool(), {"command": "printf small"}, createEnv())
    assert result["reported"] == []


async def test_read_counts_a_negative_limit_from_the_end_like_array_slice():
    env = createEnv()
    getOrThrow(await env.writeFile("five.txt", "l1\nl2\nl3\nl4\nl5", BACKGROUND_CONTEXT))
    result = await run(createReadTool(), {"path": "five.txt", "limit": -1}, env)
    assert textOutput(result) == "l1\nl2\nl3\nl4"
    result = await run(createReadTool(), {"path": "five.txt", "offset": 3, "limit": -3}, env)
    assert textOutput(result) == "l3\nl4"


async def test_read_keeps_fractional_offsets_and_limits_until_slice_truncates_them():
    env = createEnv()
    getOrThrow(await env.writeFile("five.txt", "l1\nl2\nl3\nl4\nl5", BACKGROUND_CONTEXT))
    result = await run(createReadTool(), {"path": "five.txt", "offset": 1.5, "limit": 1.5}, env)
    assert textOutput(result) == "l1\nl2"
    assert diagnosticText(result) == "3 more lines in file. Use offset=3 to continue."
    result = await run(createReadTool(), {"path": "five.txt", "offset": 2, "limit": 2.5}, env)
    assert textOutput(result) == "l2\nl3"
    assert diagnosticText(result) == "1.5 more lines in file. Use offset=4.5 to continue."


async def test_edit_counts_occurrences_of_whitespace_only_text():
    env = createEnv()
    getOrThrow(await env.writeFile("indent.txt", "    \n", BACKGROUND_CONTEXT))
    result = await run(createEditTool(), {"path": "indent.txt", "edits": [{"oldText": "    ", "newText": "x"}]}, env)
    assert not result.get("isError")
    assert getOrThrow(await env.readTextFile("indent.txt", BACKGROUND_CONTEXT)) == "x\n"
    getOrThrow(await env.writeFile("tabbed.txt", "def f():\n\treturn 1\n", BACKGROUND_CONTEXT))
    with pytest.raises(Exception, match="Found 18 occurrences of the text in tabbed.txt"):
        await run(createEditTool(), {"path": "tabbed.txt", "edits": [{"oldText": "\t", "newText": "  "}]}, env)
