"""pi durable's ``test/env-node.test.ts``.

Node-specific mechanics (mocked ``FileHandle`` methods, a mocked ``process.platform``, asynchronous ``taskkill`` spawn
errors) are checked through the Python equivalents where there is one and left out where there is none.
"""

from __future__ import annotations

import asyncio
import errno
import os
import shlex
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from misaka.chord.context import BACKGROUND_CONTEXT, withAbortSignal
from misaka.durable.env import FileError, getOrThrow
from misaka.durable.env import node as envNode
from misaka.durable.env.node import NodeExecutionEnv
from misaka.utils.js import AbortController
from tests.chord_helpers import matches

win32 = sys.platform == "win32"


def removeTempDir(directory: str) -> None:
    """Remove a test directory. On Windows, ``taskkill /T`` runs asynchronously and can miss descendants such as Git
    Bash's ``sleep``, so a killed command's processes can hold the directory for a while after ``exec`` settles; retry
    until they exit."""
    import shutil
    import time

    attempt = 0
    while True:
        try:
            shutil.rmtree(directory)
            return
        except FileNotFoundError:
            return
        except OSError as error:
            code = error.errno
            if attempt >= 80 or code not in (errno.EBUSY, errno.EPERM, errno.EACCES, errno.ENOTEMPTY):
                raise
            attempt += 1
            time.sleep(0.1)


@pytest.fixture
def root() -> Iterator[str]:
    directory = tempfile.mkdtemp(prefix="pi-durable-env-")
    yield directory
    removeTempDir(directory)


def abortedContext():
    controller = AbortController()
    controller.abort()
    return withAbortSignal(controller.signal, BACKGROUND_CONTEXT)


def norm(*parts: str) -> str:
    return os.path.normpath(os.path.join(*parts))


async def collectShellOutput(env, command, options, context):
    output: list[str] = []
    result = await env.exec(command, {**(options or {}), "onOutput": lambda text, _context, _info: output.append(text)}, context)
    return result, "".join(output)


def pythonCommand(script: str) -> str:
    return f"{shlex.quote(Path(sys.executable).as_posix())} -c {shlex.quote(script)}"


class FailingSpillExecutionEnv(NodeExecutionEnv):
    async def createTempFile(self, options, context):
        if options is not None and options.get("prefix") == "pi-output-":
            return {"ok": True, "value": os.path.join(self.cwd, "missing", "spill.log")}
        return await super().createTempFile(options, context)


class TestNodeExecutionEnvFilesystem:
    async def test_reads_writes_lists_and_removes_files_and_directories(self, root):
        env = NodeExecutionEnv({"cwd": root})
        assert getOrThrow(await env.absolutePath("nested/child", BACKGROUND_CONTEXT)) == norm(root, "nested/child")
        assert getOrThrow(await env.joinPath([root, "nested", "child"], BACKGROUND_CONTEXT)) == norm(root, "nested", "child")
        getOrThrow(await env.createDir("nested/child", None, BACKGROUND_CONTEXT))
        getOrThrow(await env.writeFile("nested/child/file.txt", "hel", BACKGROUND_CONTEXT))
        getOrThrow(await env.appendFile("nested/child/file.txt", "lo", BACKGROUND_CONTEXT))
        assert getOrThrow(await env.readTextFile("nested/child/file.txt", BACKGROUND_CONTEXT)) == "hello"
        assert getOrThrow(await env.readTextLines("nested/child/file.txt", {"maxLines": 1}, BACKGROUND_CONTEXT)) == ["hello"]
        assert getOrThrow(await env.readBinaryFile("nested/child/file.txt", BACKGROUND_CONTEXT)).decode("utf-8") == "hello"

        entries = getOrThrow(await env.listDir("nested/child", BACKGROUND_CONTEXT))
        assert len(entries) == 1
        assert matches(
            entries[0],
            {"name": "file.txt", "path": norm(root, "nested/child/file.txt"), "kind": "file", "size": 5},
        )
        assert isinstance(entries[0]["mtimeMs"], int | float)

        assert getOrThrow(await env.exists("nested/child/file.txt", BACKGROUND_CONTEXT)) is True
        getOrThrow(await env.remove("nested/child/file.txt", None, BACKGROUND_CONTEXT))
        assert getOrThrow(await env.exists("nested/child/file.txt", BACKGROUND_CONTEXT)) is False

    async def test_expands_home_relative_paths_and_file_urls(self, root):
        env = NodeExecutionEnv({"cwd": root})
        assert getOrThrow(await env.absolutePath("~/pi-node-env-test", BACKGROUND_CONTEXT)) == norm(
            str(Path.home()), "pi-node-env-test"
        )
        filePath = os.path.join(root, "file with spaces.txt")
        assert getOrThrow(await env.absolutePath(Path(filePath).as_uri(), BACKGROUND_CONTEXT)) == filePath

    async def test_returns_file_info_for_files_directories_and_symlinks_without_following_symlinks(self, root):
        env = NodeExecutionEnv({"cwd": root})
        getOrThrow(await env.createDir("dir", {"recursive": True}, BACKGROUND_CONTEXT))
        getOrThrow(await env.writeFile("dir/file.txt", "hello", BACKGROUND_CONTEXT))
        try:
            os.symlink(os.path.join(root, "dir", "file.txt"), os.path.join(root, "file-link"))
            os.symlink(os.path.join(root, "dir"), os.path.join(root, "dir-link"), target_is_directory=True)
        except OSError:
            pytest.skip("symbolic links are not available")

        assert matches(
            getOrThrow(await env.fileInfo("dir", BACKGROUND_CONTEXT)),
            {"name": "dir", "path": norm(root, "dir"), "kind": "directory"},
        )
        assert matches(
            getOrThrow(await env.fileInfo("dir/file.txt", BACKGROUND_CONTEXT)),
            {"name": "file.txt", "path": norm(root, "dir/file.txt"), "kind": "file", "size": 5},
        )
        assert matches(
            getOrThrow(await env.fileInfo("file-link", BACKGROUND_CONTEXT)),
            {"name": "file-link", "path": norm(root, "file-link"), "kind": "symlink"},
        )
        assert matches(
            getOrThrow(await env.fileInfo("dir-link", BACKGROUND_CONTEXT)),
            {"name": "dir-link", "path": norm(root, "dir-link"), "kind": "symlink"},
        )
        assert getOrThrow(await env.canonicalPath("file-link", BACKGROUND_CONTEXT)) == os.path.realpath(
            os.path.join(root, "dir", "file.txt")
        )

    async def test_lists_symlinks_as_symlinks(self, root):
        env = NodeExecutionEnv({"cwd": root})
        getOrThrow(await env.writeFile("target.txt", "hello", BACKGROUND_CONTEXT))
        try:
            os.symlink(os.path.join(root, "target.txt"), os.path.join(root, "link.txt"))
        except OSError:
            pytest.skip("symbolic links are not available")

        entries = getOrThrow(await env.listDir(".", BACKGROUND_CONTEXT))
        assert sorted(({"name": entry["name"], "kind": entry["kind"]} for entry in entries), key=lambda e: e["name"]) == [
            {"name": "link.txt", "kind": "symlink"},
            {"name": "target.txt", "kind": "file"},
        ]

    async def test_stops_reading_text_lines_at_the_requested_limit(self, root):
        env = NodeExecutionEnv({"cwd": root})
        getOrThrow(await env.writeFile("file.txt", "one\ntwo\nthree", BACKGROUND_CONTEXT))
        assert getOrThrow(await env.readTextLines("file.txt", {"maxLines": 1}, BACKGROUND_CONTEXT)) == ["one"]

    async def test_returns_file_error_for_missing_paths_and_keeps_exists_false_for_missing_paths(self, root):
        env = NodeExecutionEnv({"cwd": root})
        info = await env.fileInfo("missing.txt", BACKGROUND_CONTEXT)
        assert info["ok"] is False
        assert isinstance(info["error"], FileError)
        assert info["error"].name == "FileError"
        assert info["error"].code == "not_found"
        assert info["error"].path == norm(root, "missing.txt")
        assert getOrThrow(await env.exists("missing.txt", BACKGROUND_CONTEXT)) is False

    async def test_returns_file_error_for_listing_non_directories(self, root):
        env = NodeExecutionEnv({"cwd": root})
        getOrThrow(await env.writeFile("file.txt", "hello", BACKGROUND_CONTEXT))
        result = await env.listDir("file.txt", BACKGROUND_CONTEXT)
        assert result["ok"] is False
        assert isinstance(result["error"], FileError)
        assert result["error"].code == "not_directory"

    async def test_appends_to_new_files_and_creates_parent_directories(self, root):
        env = NodeExecutionEnv({"cwd": root})
        getOrThrow(await env.appendFile("new/nested/file.txt", "a", BACKGROUND_CONTEXT))
        getOrThrow(await env.appendFile("new/nested/file.txt", "b", BACKGROUND_CONTEXT))
        assert getOrThrow(await env.readTextFile("new/nested/file.txt", BACKGROUND_CONTEXT)) == "ab"

    async def test_atomically_renames_a_file_and_replaces_the_destination(self, root):
        env = NodeExecutionEnv({"cwd": root})
        getOrThrow(await env.writeFile("source.txt", "new", BACKGROUND_CONTEXT))
        getOrThrow(await env.writeFile("destination.txt", "old", BACKGROUND_CONTEXT))

        getOrThrow(await env.renameFile("source.txt", "destination.txt", BACKGROUND_CONTEXT))

        assert getOrThrow(await env.exists("source.txt", BACKGROUND_CONTEXT)) is False
        assert getOrThrow(await env.readTextFile("destination.txt", BACKGROUND_CONTEXT)) == "new"

    async def test_reports_the_source_path_when_rename_fails_because_the_source_is_missing(self, root):
        env = NodeExecutionEnv({"cwd": root})
        getOrThrow(await env.writeFile("destination.txt", "unchanged", BACKGROUND_CONTEXT))

        result = await env.renameFile("missing-source.txt", "destination.txt", BACKGROUND_CONTEXT)

        assert result["ok"] is False
        assert result["error"].code == "not_found"
        assert result["error"].path == norm(root, "missing-source.txt")
        assert getOrThrow(await env.readTextFile("destination.txt", BACKGROUND_CONTEXT)) == "unchanged"

    async def test_creates_temporary_directories_and_files(self, root):
        env = NodeExecutionEnv({"cwd": root})
        tempDir = getOrThrow(await env.createTempDir("node-env-test-", BACKGROUND_CONTEXT))
        try:
            assert os.path.isdir(tempDir)
            tempFile = getOrThrow(await env.createTempFile({"prefix": "prefix-", "suffix": ".txt"}, BACKGROUND_CONTEXT))
            try:
                assert os.path.exists(tempFile)
                assert tempFile.endswith(".txt")
                assert getOrThrow(await env.readTextFile(tempFile, BACKGROUND_CONTEXT)) == ""
            finally:
                import shutil

                shutil.rmtree(os.path.dirname(tempFile), ignore_errors=True)
        finally:
            import shutil

            shutil.rmtree(tempDir, ignore_errors=True)

    async def test_honors_create_dir_recursive_false_and_remove_recursive_force_options(self, root):
        env = NodeExecutionEnv({"cwd": root})
        createResult = await env.createDir("missing/child", {"recursive": False}, BACKGROUND_CONTEXT)
        assert createResult["ok"] is False
        assert createResult["error"].code == "not_found"

        getOrThrow(await env.writeFile("dir/child/file.txt", "hello", BACKGROUND_CONTEXT))
        removeDirectory = await env.remove("dir", {"recursive": False}, BACKGROUND_CONTEXT)
        assert removeDirectory["ok"] is False
        getOrThrow(await env.remove("dir", {"recursive": True}, BACKGROUND_CONTEXT))
        assert getOrThrow(await env.exists("dir", BACKGROUND_CONTEXT)) is False

        removeMissing = await env.remove("missing", {"force": False}, BACKGROUND_CONTEXT)
        assert removeMissing["ok"] is False
        getOrThrow(await env.remove("missing", {"force": True}, BACKGROUND_CONTEXT))

    async def test_returns_aborted_results_without_side_effects_for_pre_aborted_file_operations(self, root):
        env = NodeExecutionEnv({"cwd": root})
        getOrThrow(await env.writeFile("file.txt", "hello", BACKGROUND_CONTEXT))
        context = abortedContext()

        results = await asyncio.gather(
            env.readTextFile("file.txt", context),
            env.readTextLines("file.txt", None, context),
            env.readBinaryFile("file.txt", context),
            env.openTextLineReader("file.txt", context),
            env.writeFile("other.txt", "hello", context),
            env.appendFile("file.txt", " world", context),
            env.truncateFile("file.txt", 1, context),
            env.flushFile("file.txt", context),
            env.renameFile("file.txt", "renamed.txt", context),
            env.fileInfo("file.txt", context),
            env.listDir(".", context),
            env.canonicalPath("file.txt", context),
            env.exists("file.txt", context),
            env.createDir("dir", None, context),
            env.remove("file.txt", None, context),
            env.createTempDir(None, context),
            env.createTempFile(None, context),
        )
        for result in results:
            assert result["ok"] is False
            assert result["error"].code == "aborted"
        assert getOrThrow(await env.readTextFile("file.txt", BACKGROUND_CONTEXT)) == "hello"
        assert [entry["name"] for entry in getOrThrow(await env.listDir(".", BACKGROUND_CONTEXT))] == ["file.txt"]

    async def test_truncates_and_extends_files_to_exact_byte_sizes(self, root):
        env = NodeExecutionEnv({"cwd": root})
        content = bytes([0x61, 0xC3, 0xA9, 0x0A, 0x62, 0x0A])
        getOrThrow(await env.writeFile("file.bin", content, BACKGROUND_CONTEXT))

        # Truncation is byte-exact even when the boundary splits a UTF-8 sequence.
        getOrThrow(await env.truncateFile("file.bin", 2, BACKGROUND_CONTEXT))
        assert list(getOrThrow(await env.readBinaryFile("file.bin", BACKGROUND_CONTEXT))) == [0x61, 0xC3]

        getOrThrow(await env.truncateFile("file.bin", 4, BACKGROUND_CONTEXT))
        assert list(getOrThrow(await env.readBinaryFile("file.bin", BACKGROUND_CONTEXT))) == [0x61, 0xC3, 0, 0]

        getOrThrow(await env.truncateFile("file.bin", 0, BACKGROUND_CONTEXT))
        assert getOrThrow(await env.fileInfo("file.bin", BACKGROUND_CONTEXT))["size"] == 0

    async def test_rejects_invalid_truncation_sizes_and_never_creates_missing_files(self, root):
        env = NodeExecutionEnv({"cwd": root})
        getOrThrow(await env.writeFile("file.txt", "hello", BACKGROUND_CONTEXT))
        for size in [-1, 1.5, float("nan"), float("inf"), 2**53]:
            result = await env.truncateFile("file.txt", size, BACKGROUND_CONTEXT)
            assert result["ok"] is False
            assert result["error"].code == "invalid"
            assert result["error"].path == norm(root, "file.txt")
        assert getOrThrow(await env.readTextFile("file.txt", BACKGROUND_CONTEXT)) == "hello"

        result = await env.truncateFile("missing.txt", 0, BACKGROUND_CONTEXT)
        assert result["ok"] is False
        assert result["error"].code == "not_found"
        assert result["error"].path == norm(root, "missing.txt")
        assert getOrThrow(await env.exists("missing.txt", BACKGROUND_CONTEXT)) is False

    async def test_flushes_existing_files_without_changing_content_and_reports_missing_paths(self, root):
        env = NodeExecutionEnv({"cwd": root})
        getOrThrow(await env.writeFile("file.txt", "durable", BACKGROUND_CONTEXT))
        getOrThrow(await env.flushFile("file.txt", BACKGROUND_CONTEXT))
        assert getOrThrow(await env.readTextFile("file.txt", BACKGROUND_CONTEXT)) == "durable"

        result = await env.flushFile("missing.txt", BACKGROUND_CONTEXT)
        assert result["ok"] is False
        assert result["error"].code == "not_found"
        assert result["error"].path == norm(root, "missing.txt")
        assert getOrThrow(await env.exists("missing.txt", BACKGROUND_CONTEXT)) is False

        getOrThrow(await env.createDir("dir", None, BACKGROUND_CONTEXT))
        directory = await env.flushFile("dir", BACKGROUND_CONTEXT)
        assert directory["ok"] is False
        assert directory["error"].code == "is_directory"

    async def test_syncs_through_an_opened_handle_returns_sync_failures_and_always_closes_the_handle(
        self, root, monkeypatch
    ):
        env = NodeExecutionEnv({"cwd": root})
        getOrThrow(await env.writeFile("file.txt", "durable", BACKGROUND_CONTEXT))
        synced: list[int] = []
        original = os.fsync
        failures: list[Exception] = []

        def spy(fd: int) -> None:
            synced.append(fd)
            if failures:
                raise failures.pop()
            original(fd)

        monkeypatch.setattr(os, "fsync", spy)
        getOrThrow(await env.flushFile("file.txt", BACKGROUND_CONTEXT))
        assert len(synced) == 1
        with pytest.raises(OSError):
            os.fstat(synced[0])

        failures.append(OSError("EIO: i/o error, fsync"))
        result = await env.flushFile("file.txt", BACKGROUND_CONTEXT)
        assert result["ok"] is False
        assert result["error"].code == "unknown"
        assert result["error"].path == norm(root, "file.txt")
        assert result["error"].message == "EIO: i/o error, fsync"
        assert len(synced) == 2
        with pytest.raises(OSError):
            os.fstat(synced[1])

    async def test_cleanup_is_best_effort(self, root):
        env = NodeExecutionEnv({"cwd": root})
        assert await env.cleanup(BACKGROUND_CONTEXT) is None


class TestNodeExecutionEnvTextLineReader:
    async def test_reports_whether_each_line_was_newline_terminated_and_preserves_carriage_returns(self, root):
        env = NodeExecutionEnv({"cwd": root})
        getOrThrow(await env.writeFile("lines.txt", "one\r\n\ntwo\npartial", BACKGROUND_CONTEXT))
        reader = getOrThrow(await env.openTextLineReader("lines.txt", BACKGROUND_CONTEXT))
        try:
            lines = []
            line = getOrThrow(await reader.readLine(BACKGROUND_CONTEXT))
            while line is not None:
                lines.append(line)
                line = getOrThrow(await reader.readLine(BACKGROUND_CONTEXT))
            assert lines == [
                {"text": "one\r", "terminated": True},
                {"text": "", "terminated": True},
                {"text": "two", "terminated": True},
                {"text": "partial", "terminated": False},
            ]
            assert getOrThrow(await reader.readLine(BACKGROUND_CONTEXT)) is None
        finally:
            await reader.close(BACKGROUND_CONTEXT)

    async def test_returns_no_lines_for_an_empty_file_and_one_terminated_empty_line_for_a_lone_newline(self, root):
        env = NodeExecutionEnv({"cwd": root})
        getOrThrow(await env.writeFile("empty.txt", "", BACKGROUND_CONTEXT))
        getOrThrow(await env.writeFile("newline.txt", "\n", BACKGROUND_CONTEXT))

        empty = getOrThrow(await env.openTextLineReader("empty.txt", BACKGROUND_CONTEXT))
        assert getOrThrow(await empty.readLine(BACKGROUND_CONTEXT)) is None
        await empty.close(BACKGROUND_CONTEXT)

        newline = getOrThrow(await env.openTextLineReader("newline.txt", BACKGROUND_CONTEXT))
        assert getOrThrow(await newline.readLine(BACKGROUND_CONTEXT)) == {"text": "", "terminated": True}
        assert getOrThrow(await newline.readLine(BACKGROUND_CONTEXT)) is None
        await newline.close(BACKGROUND_CONTEXT)

    async def test_decodes_multi_byte_characters_split_across_read_chunks_and_lines_longer_than_one_chunk(self, root):
        env = NodeExecutionEnv({"cwd": root})
        # The reader uses 64 KiB chunks; place a four-byte character across the first boundary.
        first = "a" * (64 * 1024 - 2) + "\U0001f600tail"
        second = "é" * 100_000
        getOrThrow(await env.writeFile("large.txt", f"{first}\n{second}", BACKGROUND_CONTEXT))
        reader = getOrThrow(await env.openTextLineReader("large.txt", BACKGROUND_CONTEXT))
        try:
            assert getOrThrow(await reader.readLine(BACKGROUND_CONTEXT)) == {"text": first, "terminated": True}
            assert getOrThrow(await reader.readLine(BACKGROUND_CONTEXT)) == {"text": second, "terminated": False}
            assert getOrThrow(await reader.readLine(BACKGROUND_CONTEXT)) is None
        finally:
            await reader.close(BACKGROUND_CONTEXT)

    async def test_rejects_a_read_cancelled_while_pending_without_consuming_its_bytes(self, root, monkeypatch):
        env = NodeExecutionEnv({"cwd": root})
        getOrThrow(await env.writeFile("lines.txt", "one\ntwo\n", BACKGROUND_CONTEXT))
        original = envNode.NodeTextLineReader._readAt
        started = threading.Event()
        release = threading.Event()
        calls = [0]

        def held(self, offset):
            calls[0] += 1
            if calls[0] == 1:
                started.set()
                release.wait(5)
            return original(self, offset)

        monkeypatch.setattr(envNode.NodeTextLineReader, "_readAt", held)
        reader = getOrThrow(await env.openTextLineReader("lines.txt", BACKGROUND_CONTEXT))
        try:
            controller = AbortController()
            pending = asyncio.ensure_future(reader.readLine(withAbortSignal(controller.signal, BACKGROUND_CONTEXT)))
            await asyncio.to_thread(started.wait, 5)
            controller.abort()
            release.set()
            result = await pending
            assert result["ok"] is False
            assert result["error"].code == "aborted"
            assert getOrThrow(await reader.readLine(BACKGROUND_CONTEXT)) == {"text": "one", "terminated": True}
            assert getOrThrow(await reader.readLine(BACKGROUND_CONTEXT)) == {"text": "two", "terminated": True}
            assert getOrThrow(await reader.readLine(BACKGROUND_CONTEXT)) is None
        finally:
            await reader.close(BACKGROUND_CONTEXT)

    async def test_rejects_reads_after_close_and_closes_idempotently(self, root):
        env = NodeExecutionEnv({"cwd": root})
        getOrThrow(await env.writeFile("lines.txt", "one\n", BACKGROUND_CONTEXT))
        reader = getOrThrow(await env.openTextLineReader("lines.txt", BACKGROUND_CONTEXT))
        await reader.close(BACKGROUND_CONTEXT)
        assert await reader.close(BACKGROUND_CONTEXT) is None
        result = await reader.readLine(BACKGROUND_CONTEXT)
        assert result["ok"] is False
        assert result["error"].code == "invalid"
        assert result["error"].path == norm(root, "lines.txt")

    async def test_reports_missing_files_when_opening_a_reader(self, root):
        env = NodeExecutionEnv({"cwd": root})
        result = await env.openTextLineReader("missing.txt", BACKGROUND_CONTEXT)
        assert result["ok"] is False
        assert result["error"].code == "not_found"
        assert result["error"].path == norm(root, "missing.txt")


class TestNodeExecutionEnvShell:
    async def test_executes_commands_in_cwd_with_env_overrides(self, root):
        env = NodeExecutionEnv({"cwd": root})
        result, output = await collectShellOutput(
            env,
            "printf '%s' \"$NODE_ENV_TEST\" > cwd-marker.txt; printf '%s:%s' \"$PWD\" \"$NODE_ENV_TEST\"",
            {"env": {"NODE_ENV_TEST": "ok"}},
            BACKGROUND_CONTEXT,
        )
        value = getOrThrow(result)
        assert value["exitCode"] == 0
        assert Path(root, "cwd-marker.txt").read_text(encoding="utf-8") == "ok"
        # Git Bash on Windows reports $PWD as an MSYS path (/tmp/...), not the Windows path.
        if not win32:
            assert output == f"{os.path.realpath(root)}:ok"

    @pytest.mark.parametrize(
        ("overrides", "expectedSessionFile"),
        [
            (None, "x:/stale/parent.jsonl"),
            ({"PI_SESSION_FILE": ""}, "x:"),
            ({"PI_SESSION_FILE": "/sessions/current.jsonl"}, "x:/sessions/current.jsonl"),
        ],
    )
    async def test_applies_string_shell_environment_overrides(self, root, overrides, expectedSessionFile):
        env = NodeExecutionEnv(
            {
                "cwd": root,
                "shellEnv": {
                    "PI_SESSION_FILE": "/stale/parent.jsonl",
                    "PI_CODING_AGENT": "true",
                    "PI_NODE_ENV_PRESERVED_TEST": "preserved",
                },
            }
        )
        result, output = await collectShellOutput(
            env,
            "printf '%s:%s|%s|%s' \"${PI_SESSION_FILE+x}\" \"${PI_SESSION_FILE-}\" \"$PI_CODING_AGENT\" \"$PI_NODE_ENV_PRESERVED_TEST\"",
            {"env": overrides} if overrides is not None else None,
            BACKGROUND_CONTEXT,
        )
        getOrThrow(result)
        assert output == f"{expectedSessionFile}|true|preserved"

    async def test_can_replace_rather_than_inherit_the_default_shell_environment(self, root, monkeypatch):
        monkeypatch.setenv("PI_NODE_ENV_INHERITED_TEST", "host")
        env = NodeExecutionEnv({"cwd": root, "shellEnv": {"PI_NODE_ENV_CONFIGURED_TEST": "configured"}})
        result, output = await collectShellOutput(
            env,
            "printf '%s:%s:%s' \"${PI_NODE_ENV_INHERITED_TEST-}\" \"${PI_NODE_ENV_CONFIGURED_TEST-}\" \"${PI_NODE_ENV_EXPLICIT_TEST-}\"",
            {"inheritEnv": False, "env": {"PI_NODE_ENV_EXPLICIT_TEST": "explicit"}},
            BACKGROUND_CONTEXT,
        )
        getOrThrow(result)
        assert output == "::explicit"

    @pytest.mark.skipif(win32, reason="needs a POSIX file system")
    async def test_uses_stdin_command_transport_for_legacy_wsl_bash_paths(self, root, monkeypatch):
        shellPath = "C:\\Windows\\System32\\bash.exe"
        env = NodeExecutionEnv({"cwd": root})
        getOrThrow(
            await env.writeFile(
                shellPath, "#!/bin/sh\nprintf 'args:%s\\n' \"$*\" >&2\nexec /bin/bash \"$@\"\n", BACKGROUND_CONTEXT
            )
        )
        os.chmod(os.path.join(root, shellPath), 0o755)

        monkeypatch.chdir(root)
        monkeypatch.setenv("PATH", f"{root}{os.pathsep}{os.environ.get('PATH', '')}")

        wslEnv = NodeExecutionEnv({"cwd": root, "shellPath": shellPath})
        nameExpansion = "${name}"
        result, output = await collectShellOutput(
            wslEnv, f"name='World'; echo \"Hello, {nameExpansion}!\"", None, BACKGROUND_CONTEXT
        )
        value = getOrThrow(result)
        assert "Hello, World!" in output
        assert "args:-s" in output
        assert value["exitCode"] == 0

    @pytest.mark.skipif(not win32, reason="Windows keeps stdio of detached descendants open")
    async def test_settles_after_the_shell_exits_when_a_detached_descendant_retains_inherited_stdio(self, root):
        pidFile = os.path.join(root, "grandchild.pid")
        script = (
            "import subprocess, sys\n"
            # ``stdio: "inherit"``: the descendant holds the shell's stdout pipe. Without ``close_fds=False`` Windows passes it no
            # handles at all, and the pipe closes when the shell exits.
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
            "stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, close_fds=False, "
            "creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)\n"
            "open(sys.argv[1], 'w').write(str(child.pid))\n"
            "print('child-exiting')\n"
        )
        env = NodeExecutionEnv({"cwd": root})
        controller = AbortController()
        try:
            result, output = await asyncio.wait_for(
                collectShellOutput(
                    env,
                    f"{pythonCommand(script)} {shlex.quote(Path(pidFile).as_posix())}",
                    None,
                    withAbortSignal(controller.signal, BACKGROUND_CONTEXT),
                ),
                3,
            )
            getOrThrow(result)
            assert "child-exiting" in output
        finally:
            controller.abort()
            if os.path.exists(pidFile):
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/F", "/T", "/PID", Path(pidFile).read_text().strip()],
                    check=False,
                    capture_output=True,
                )

    async def test_cleanup_terminates_active_shell_processes(self, root):
        env = NodeExecutionEnv({"cwd": root})
        execution = asyncio.ensure_future(env.exec("touch started; sleep 60", None, BACKGROUND_CONTEXT))
        try:
            deadline = asyncio.get_running_loop().time() + 10
            while not getOrThrow(await env.exists("started", BACKGROUND_CONTEXT)):
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)
        finally:
            await env.cleanup(BACKGROUND_CONTEXT)
        result = await asyncio.wait_for(execution, 3)
        assert result["ok"] is True

    async def test_streams_combined_stdout_and_stderr(self, root):
        env = NodeExecutionEnv({"cwd": root})
        result, output = await collectShellOutput(env, "printf out; printf err >&2", None, BACKGROUND_CONTEXT)
        assert getOrThrow(result) == {"exitCode": 0}
        assert "out" in output
        assert "err" in output

    async def test_decodes_utf8_split_across_raw_process_chunks(self, root):
        env = NodeExecutionEnv({"cwd": root})
        script = (
            "import sys, time\n"
            "b = bytes([0xf0, 0x9f, 0x98, 0x80])\n"
            "sys.stdout.buffer.write(b[:2]); sys.stdout.buffer.flush(); time.sleep(0.05)\n"
            "sys.stdout.buffer.write(b[2:]); sys.stdout.buffer.flush()\n"
        )
        result, output = await collectShellOutput(env, pythonCommand(script), None, BACKGROUND_CONTEXT)
        getOrThrow(result)
        assert output == "\U0001f600"

    async def test_reports_a_missing_working_directory_before_spawning(self, root):
        env = NodeExecutionEnv({"cwd": os.path.join(root, "missing")})
        result = await env.exec("printf ok", None, BACKGROUND_CONTEXT)
        assert result["ok"] is False
        assert result["error"].code == "spawn_error"
        assert "Working directory does not exist" in result["error"].message

    async def test_returns_non_zero_command_exit_codes_as_successful_execution_results(self, root):
        env = NodeExecutionEnv({"cwd": root})
        assert getOrThrow(await env.exec("exit 7", None, BACKGROUND_CONTEXT)) == {"exitCode": 7}

    @pytest.mark.skipif(win32, reason="signals")
    async def test_maps_signal_killed_processes_to_a_non_zero_exit_code(self, root):
        env = NodeExecutionEnv({"cwd": root})
        result = getOrThrow(await env.exec("kill -9 $$", None, BACKGROUND_CONTEXT))
        assert result["exitCode"] == 128 + 9

    async def test_returns_timeout_errors_for_commands_exceeding_the_timeout(self, root):
        env = NodeExecutionEnv({"cwd": root})
        result = await env.exec("sleep 5", {"timeout": 0.01}, BACKGROUND_CONTEXT)
        assert result["ok"] is False
        assert result["error"].code == "timeout"

    async def test_rejects_invalid_timeouts_before_spawning(self, root):
        env = NodeExecutionEnv({"cwd": root})
        for timeout in [0, -1, float("nan"), float("inf"), 2_147_484]:
            result = await env.exec("touch spawned", {"timeout": timeout}, BACKGROUND_CONTEXT)
            assert result["ok"] is False
            assert result["error"].code == "timeout"
            assert "Invalid timeout" in result["error"].message
        assert getOrThrow(await env.exists("spawned", BACKGROUND_CONTEXT)) is False

    async def test_returns_callback_errors_from_exec_stream_handlers(self, root):
        env = NodeExecutionEnv({"cwd": root})

        def failing(text, context, info):
            raise RuntimeError("callback failed")

        result = await env.exec("printf out", {"onOutput": failing}, BACKGROUND_CONTEXT)
        assert result["ok"] is False
        assert result["error"].code == "callback_error"
        assert result["error"].message == "callback failed"

    async def test_returns_shell_unavailable_and_spawn_errors(self, root):
        missingShellEnv = NodeExecutionEnv({"cwd": root, "shellPath": os.path.join(root, "missing-shell")})
        missingShell = await missingShellEnv.exec("printf ok", None, BACKGROUND_CONTEXT)
        assert missingShell["ok"] is False
        assert missingShell["error"].code == "shell_unavailable"

        shellPath = os.path.join(root, "not-executable-shell")
        env = NodeExecutionEnv({"cwd": root})
        getOrThrow(await env.writeFile(shellPath, "not executable", BACKGROUND_CONTEXT))
        spawnErrorEnv = NodeExecutionEnv({"cwd": root, "shellPath": shellPath})
        spawnError = await spawnErrorEnv.exec("printf ok", None, BACKGROUND_CONTEXT)
        assert spawnError["ok"] is False
        assert spawnError["error"].code == "spawn_error"

    async def test_returns_an_aborted_result_for_pre_aborted_and_aborted_commands(self, root):
        env = NodeExecutionEnv({"cwd": root})
        result = await env.exec("touch spawned", None, abortedContext())
        assert result["ok"] is False
        assert result["error"].code == "aborted"
        assert getOrThrow(await env.exists("spawned", BACKGROUND_CONTEXT)) is False

        controller = AbortController()
        promise = asyncio.ensure_future(
            env.exec("sleep 5", None, withAbortSignal(controller.signal, BACKGROUND_CONTEXT))
        )
        await asyncio.sleep(0.2)
        controller.abort()
        aborted = await promise
        assert aborted["ok"] is False
        assert aborted["error"].code == "aborted"

    async def test_does_not_create_a_spill_before_output_crosses_its_thresholds(self, root):
        env = NodeExecutionEnv({"cwd": root})
        result = getOrThrow(
            await env.exec("printf short", {"spill": {"afterBytes": 100, "afterLines": 10}}, BACKGROUND_CONTEXT)
        )
        assert "spillPath" not in result

    async def test_preserves_exact_raw_bytes_in_the_spill_while_streaming_decoded_text(self, root):
        env = NodeExecutionEnv({"cwd": root})
        expected = [0x66, 0x80, 0x00, 0x6F]
        result = getOrThrow(
            await env.exec(
                pythonCommand(f"import sys; sys.stdout.buffer.write(bytes({expected}))"),
                {"spill": {"afterBytes": 1, "afterLines": 10}},
                BACKGROUND_CONTEXT,
            )
        )
        assert "spillPath" in result
        try:
            assert list(getOrThrow(await env.readBinaryFile(result["spillPath"], BACKGROUND_CONTEXT))) == expected
        finally:
            import shutil

            shutil.rmtree(os.path.dirname(result["spillPath"]), ignore_errors=True)

    async def test_reports_the_spill_of_a_command_that_times_out(self, root):
        env = NodeExecutionEnv({"cwd": root})
        result = await env.exec(
            "printf 12345678901234567890; sleep 5",
            {"timeout": 0.3, "spill": {"afterBytes": 10, "afterLines": 10}},
            BACKGROUND_CONTEXT,
        )
        assert result["ok"] is False
        assert result["error"].code == "timeout"
        spillPath = result["error"].spillPath
        assert isinstance(spillPath, str)
        try:
            assert getOrThrow(await env.readTextFile(spillPath, BACKGROUND_CONTEXT)) == "12345678901234567890"
        finally:
            import shutil

            shutil.rmtree(os.path.dirname(spillPath), ignore_errors=True)

    async def test_fails_rather_than_silently_losing_a_requested_spill(self, root):
        env = FailingSpillExecutionEnv({"cwd": root})
        result = await env.exec(
            "printf 12345678901234567890", {"spill": {"afterBytes": 10, "afterLines": 10}}, BACKGROUND_CONTEXT
        )
        assert result["ok"] is False
        assert result["error"].code == "unknown"
        assert "Failed to preserve complete shell output" in result["error"].message

    async def test_preserves_complete_large_output_in_the_spill(self, root):
        env = NodeExecutionEnv({"cwd": root})
        size = 500_000
        result = getOrThrow(
            await env.exec(
                pythonCommand(f"import sys; sys.stdout.write('x' * {size})"),
                {"spill": {"afterBytes": 10, "afterLines": 10}},
                BACKGROUND_CONTEXT,
            )
        )
        assert "spillPath" in result
        try:
            assert len(getOrThrow(await env.readTextFile(result["spillPath"], BACKGROUND_CONTEXT))) == size
        finally:
            import shutil

            shutil.rmtree(os.path.dirname(result["spillPath"]), ignore_errors=True)

    async def test_streams_every_line_and_spills_them_all_once_output_crosses_its_line_threshold(self, root):
        env = NodeExecutionEnv({"cwd": root})
        result, output = await collectShellOutput(
            env,
            "i=1; while [ $i -le 15000 ]; do echo line-$i; i=$((i+1)); done",
            {"spill": {"afterBytes": 1024 * 1024, "afterLines": 100}},
            BACKGROUND_CONTEXT,
        )
        value = getOrThrow(result)
        assert output.replace("\r\n", "\n") == "".join(f"line-{index + 1}\n" for index in range(15000))
        assert "spillPath" in value
        try:
            spilled = getOrThrow(await env.readTextLines(value["spillPath"], None, BACKGROUND_CONTEXT))
            assert len(spilled) == 15000
            assert spilled[-1].rstrip("\r") == "line-15000"
        finally:
            import shutil

            shutil.rmtree(os.path.dirname(value["spillPath"]), ignore_errors=True)


@pytest.mark.skipif(win32, reason="needs POSIX shell semantics")
class TestNodeExecutionEnvSpillBackpressure:
    async def test_keeps_inherited_stdio_open_past_the_exit_grace_period_while_a_spill_write_is_pending(
        self, root, monkeypatch
    ):
        # Make the spill stream deterministically slow and immediately backpressured.
        rejected = [0]
        monkeypatch.setattr(envNode, "SPILL_HIGH_WATER_MARK", 1)
        originalWrite = envNode._writeSpillChunk

        def slowWrite(file: Any, chunk: bytes) -> None:
            import time

            time.sleep(0.6)
            originalWrite(file, chunk)

        monkeypatch.setattr(envNode, "_writeSpillChunk", slowWrite)
        originalStreamWrite = envNode._SpillStream.write

        def countingWrite(self, chunk):
            accepted = originalStreamWrite(self, chunk)
            if not accepted:
                rejected[0] += 1
            return accepted

        monkeypatch.setattr(envNode._SpillStream, "write", countingWrite)

        env = NodeExecutionEnv({"cwd": root})
        # The shell exits after the first chunk crosses the spill threshold and backpressures the spill. A background
        # descendant retains stdout and writes after the post-exit grace period. Without the pending-spill guard,
        # settlement destroys stdout before that descendant output is read.
        command = "printf '%020d' 0 | tr 0 a; (sleep 0.2; printf '%01000d' 0 | tr 0 b) &"
        output: list[str] = []
        result = getOrThrow(
            await env.exec(
                command,
                {
                    "spill": {"afterBytes": 10, "afterLines": 10},
                    "onOutput": lambda text, _context, _info: output.append(text),
                },
                BACKGROUND_CONTEXT,
            )
        )

        assert rejected[0] > 0
        assert len("".join(output)) == 1020
        assert "spillPath" in result
        try:
            assert getOrThrow(await env.readTextFile(result["spillPath"], BACKGROUND_CONTEXT)) == "a" * 20 + "b" * 1000
        finally:
            import shutil

            shutil.rmtree(os.path.dirname(result["spillPath"]), ignore_errors=True)
