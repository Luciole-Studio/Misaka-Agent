"""Local filesystem and shell execution environment for the harness."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import aiofiles

from misaka.agent.harness.types import ExecutionError, FileError, FileInfo, err, ok, to_error


def _resolve_path(cwd: str, path: str) -> str:
    return path if os.path.isabs(path) else os.path.abspath(os.path.join(cwd, path))


def _file_kind_from_mode(mode: int) -> str | None:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return None


def _file_info_from_stat(path: str, stat_result: os.stat_result):
    kind = _file_kind_from_mode(stat_result.st_mode)
    if kind is None:
        return err(FileError("invalid", "Unsupported file type", path))
    return ok(
        FileInfo(
            name=os.path.basename(path.rstrip(os.sep)) or path,
            path=path,
            kind=kind,
            size=stat_result.st_size,
            mtimeMs=stat_result.st_mtime * 1000,
        )
    )


def _to_file_error(error: Any, path: str | None = None) -> FileError:
    if isinstance(error, FileError):
        return error
    cause = to_error(error)
    if isinstance(error, PermissionError):
        return FileError("permission_denied", cause.args[0], path, cause)
    if isinstance(error, FileNotFoundError):
        return FileError("not_found", cause.args[0], path, cause)
    if isinstance(error, NotADirectoryError):
        return FileError("not_directory", cause.args[0], path, cause)
    if isinstance(error, IsADirectoryError):
        return FileError("is_directory", cause.args[0], path, cause)
    if isinstance(error, ValueError):
        return FileError("invalid", cause.args[0], path, cause)
    return FileError("unknown", cause.args[0], path, cause)


def _abort_result(signal_obj: Any | None, path: str | None = None):
    if getattr(signal_obj, "aborted", False):
        return err(FileError("aborted", "aborted", path))
    return None


async def _path_exists(path: str) -> bool:
    return await asyncio.to_thread(os.path.exists, path)


async def _find_bash_on_path() -> str | None:
    return shutil.which("bash.exe" if os.name == "nt" else "bash")


async def _get_shell_config(custom_shell_path: str | None = None):
    if custom_shell_path:
        if await _path_exists(custom_shell_path):
            return ok({"shell": custom_shell_path, "args": ["-c"]})
        return err(ExecutionError("shell_unavailable", f"Custom shell path not found: {custom_shell_path}"))
    if os.name == "nt":
        candidates: list[str] = []
        program_files = os.environ.get("ProgramFiles")
        if program_files:
            candidates.append(os.path.join(program_files, "Git", "bin", "bash.exe"))
        program_files_x86 = os.environ.get("ProgramFiles(x86)")
        if program_files_x86:
            candidates.append(os.path.join(program_files_x86, "Git", "bin", "bash.exe"))
        for candidate in candidates:
            if await _path_exists(candidate):
                return ok({"shell": candidate, "args": ["-c"]})
        bash_on_path = await _find_bash_on_path()
        if bash_on_path:
            return ok({"shell": bash_on_path, "args": ["-c"]})
        return err(ExecutionError("shell_unavailable", "No bash shell found"))
    if await _path_exists("/bin/bash"):
        return ok({"shell": "/bin/bash", "args": ["-c"]})
    bash_on_path = await _find_bash_on_path()
    if bash_on_path:
        return ok({"shell": bash_on_path, "args": ["-c"]})
    return ok({"shell": "sh", "args": ["-c"]})


def _get_shell_env(base_env: dict[str, str] | None = None, extra_env: dict[str, str] | None = None) -> dict[str, str]:
    merged = dict(os.environ)
    if base_env:
        merged.update(base_env)
    if extra_env:
        merged.update(extra_env)
    return merged


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    try:
        if process.pid is None:
            return
        if os.name == "nt":
            subprocess.Popen(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


async def _wait_for_abort(signal_obj: Any | None) -> None:
    if signal_obj is None:
        await asyncio.Future()
    wait = getattr(signal_obj, "wait", None)
    if callable(wait):
        await wait()
        return
    while not getattr(signal_obj, "aborted", False) and not bool(getattr(signal_obj, "is_set", lambda: False)()):
        await asyncio.sleep(0.01)


class NodeExecutionEnv:
    def __init__(self, options: dict[str, Any]) -> None:
        self.cwd = options["cwd"]
        self.shellPath = options.get("shellPath")
        self.shellEnv = options.get("shellEnv")

    async def absolutePath(self, path: str, abortSignal: Any | None = None):
        aborted = _abort_result(abortSignal, _resolve_path(self.cwd, path))
        if aborted:
            return aborted
        return ok(_resolve_path(self.cwd, path))

    async def joinPath(self, parts: list[str], abortSignal: Any | None = None):
        aborted = _abort_result(abortSignal)
        if aborted:
            return aborted
        return ok(os.path.join(*parts))

    async def exec(self, command: str, options: dict[str, Any] | None = None):
        opts = dict(options or {})
        if getattr(opts.get("abortSignal"), "aborted", False):
            return err(ExecutionError("aborted", "aborted"))

        cwd = _resolve_path(self.cwd, opts["cwd"]) if opts.get("cwd") else self.cwd
        shell_config = await _get_shell_config(self.shellPath)
        if not shell_config.ok:
            return shell_config

        callback_error: ExecutionError | None = None
        timed_out = False
        process: asyncio.subprocess.Process | None = None
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        async def read_stream(stream: asyncio.StreamReader | None, callback: Any, sink: list[str]) -> None:
            nonlocal callback_error
            if stream is None:
                return
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    return
                text = chunk.decode("utf-8", errors="replace")
                sink.append(text)
                try:
                    if callback is not None:
                        callback(text)
                except Exception as error:
                    callback_error = ExecutionError("callback_error", str(error), error)
                    if process is not None:
                        _kill_process_tree(process)
                    return

        try:
            process = await asyncio.create_subprocess_exec(
                shell_config.value["shell"],
                *shell_config.value["args"],
                command,
                cwd=cwd,
                env=_get_shell_env(self.shellEnv, opts.get("env")),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name != "nt",
            )
        except Exception as error:
            return err(ExecutionError("spawn_error", str(error), error if isinstance(error, Exception) else None))

        stdout_task = asyncio.create_task(read_stream(process.stdout, opts.get("onStdout"), stdout_parts))
        stderr_task = asyncio.create_task(read_stream(process.stderr, opts.get("onStderr"), stderr_parts))
        wait_task = asyncio.create_task(process.wait())
        abort_task = None
        if opts.get("abortSignal") is not None:
            abort_task = asyncio.create_task(_wait_for_abort(opts["abortSignal"]))
        try:
            tasks = [wait_task]
            if abort_task is not None:
                tasks.append(abort_task)
            timeout = opts.get("timeout")
            done, _pending = await asyncio.wait(
                tasks,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if wait_task not in done:
                if abort_task is not None and abort_task in done:
                    _kill_process_tree(process)
                else:
                    timed_out = True
                    _kill_process_tree(process)
                await wait_task
        finally:
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            if abort_task is not None:
                abort_task.cancel()
                await asyncio.gather(abort_task, return_exceptions=True)

        if callback_error is not None:
            return err(callback_error)
        if timed_out:
            return err(ExecutionError("timeout", f"timeout:{opts.get('timeout')}"))
        if getattr(opts.get("abortSignal"), "aborted", False):
            return err(ExecutionError("aborted", "aborted"))
        return ok({"stdout": "".join(stdout_parts), "stderr": "".join(stderr_parts), "exitCode": process.returncode or 0})

    async def readTextFile(self, path: str, abortSignal: Any | None = None):
        resolved = _resolve_path(self.cwd, path)
        aborted = _abort_result(abortSignal, resolved)
        if aborted:
            return aborted
        try:
            async with aiofiles.open(resolved, encoding="utf-8") as handle:
                return ok(await handle.read())
        except Exception as error:
            return err(_to_file_error(error, resolved))

    async def readTextLines(self, path: str, options: dict[str, Any] | None = None):
        opts = dict(options or {})
        resolved = _resolve_path(self.cwd, path)
        aborted = _abort_result(opts.get("abortSignal"), resolved)
        if aborted:
            return aborted
        if opts.get("maxLines") is not None and opts["maxLines"] <= 0:
            return ok([])
        lines: list[str] = []
        try:
            async with aiofiles.open(resolved, encoding="utf-8") as handle:
                async for line in handle:
                    aborted = _abort_result(opts.get("abortSignal"), resolved)
                    if aborted:
                        return aborted
                    lines.append(line.rstrip("\n").rstrip("\r"))
                    if opts.get("maxLines") is not None and len(lines) >= opts["maxLines"]:
                        break
            return ok(lines)
        except Exception as error:
            return err(_to_file_error(error, resolved))

    async def readBinaryFile(self, path: str, abortSignal: Any | None = None):
        resolved = _resolve_path(self.cwd, path)
        aborted = _abort_result(abortSignal, resolved)
        if aborted:
            return aborted
        try:
            async with aiofiles.open(resolved, "rb") as handle:
                return ok(await handle.read())
        except Exception as error:
            return err(_to_file_error(error, resolved))

    async def writeFile(self, path: str, content: str | bytes, abortSignal: Any | None = None):
        resolved = _resolve_path(self.cwd, path)
        aborted = _abort_result(abortSignal, resolved)
        if aborted:
            return aborted
        try:
            await asyncio.to_thread(os.makedirs, os.path.dirname(resolved), exist_ok=True)
            aborted = _abort_result(abortSignal, resolved)
            if aborted:
                return aborted
            mode = "wb" if isinstance(content, bytes) else "w"
            kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8"}
            async with aiofiles.open(resolved, mode, **kwargs) as handle:
                await handle.write(content)
            return ok(None)
        except Exception as error:
            return err(_to_file_error(error, resolved))

    async def appendFile(self, path: str, content: str | bytes, abortSignal: Any | None = None):
        resolved = _resolve_path(self.cwd, path)
        aborted = _abort_result(abortSignal, resolved)
        if aborted:
            return aborted
        try:
            await asyncio.to_thread(os.makedirs, os.path.dirname(resolved), exist_ok=True)
            mode = "ab" if isinstance(content, bytes) else "a"
            kwargs = {} if isinstance(content, bytes) else {"encoding": "utf-8"}
            async with aiofiles.open(resolved, mode, **kwargs) as handle:
                await handle.write(content)
            return ok(None)
        except Exception as error:
            return err(_to_file_error(error, resolved))

    async def fileInfo(self, path: str, abortSignal: Any | None = None):
        resolved = _resolve_path(self.cwd, path)
        aborted = _abort_result(abortSignal, resolved)
        if aborted:
            return aborted
        try:
            stat_result = await asyncio.to_thread(os.lstat, resolved)
            return _file_info_from_stat(resolved, stat_result)
        except Exception as error:
            return err(_to_file_error(error, resolved))

    async def listDir(self, path: str, abortSignal: Any | None = None):
        resolved = _resolve_path(self.cwd, path)
        aborted = _abort_result(abortSignal, resolved)
        if aborted:
            return aborted
        try:
            entries = await asyncio.to_thread(lambda: list(os.scandir(resolved)))
            infos: list[FileInfo] = []
            for entry in entries:
                aborted = _abort_result(abortSignal, resolved)
                if aborted:
                    return aborted
                entry_path = os.path.join(resolved, entry.name)
                stat_result = await asyncio.to_thread(os.lstat, entry_path)
                info = _file_info_from_stat(entry_path, stat_result)
                if not info.ok:
                    return info
                infos.append(info.value)
            return ok(infos)
        except Exception as error:
            return err(_to_file_error(error, resolved))

    async def canonicalPath(self, path: str, abortSignal: Any | None = None):
        resolved = _resolve_path(self.cwd, path)
        aborted = _abort_result(abortSignal, resolved)
        if aborted:
            return aborted
        try:
            return ok(str(Path(resolved).resolve(strict=True)))
        except Exception as error:
            return err(_to_file_error(error, resolved))

    async def exists(self, path: str, abortSignal: Any | None = None):
        result = await self.fileInfo(path, abortSignal)
        if result.ok:
            return ok(True)
        if result.error.code == "not_found":
            return ok(False)
        return err(result.error)

    async def createDir(self, path: str, options: dict[str, Any] | None = None):
        opts = dict(options or {})
        resolved = _resolve_path(self.cwd, path)
        aborted = _abort_result(opts.get("abortSignal"), resolved)
        if aborted:
            return aborted
        try:
            if opts.get("recursive", True):
                await asyncio.to_thread(os.makedirs, resolved, exist_ok=True)
            else:
                await asyncio.to_thread(os.mkdir, resolved)
            return ok(None)
        except Exception as error:
            return err(_to_file_error(error, resolved))

    async def remove(self, path: str, options: dict[str, Any] | None = None):
        opts = dict(options or {})
        resolved = _resolve_path(self.cwd, path)
        aborted = _abort_result(opts.get("abortSignal"), resolved)
        if aborted:
            return aborted
        try:
            if os.path.isdir(resolved) and not os.path.islink(resolved):
                if opts.get("recursive", False):
                    await asyncio.to_thread(shutil.rmtree, resolved)
                else:
                    await asyncio.to_thread(os.rmdir, resolved)
            else:
                await asyncio.to_thread(os.unlink, resolved)
            return ok(None)
        except FileNotFoundError as error:
            if opts.get("force", False):
                return ok(None)
            return err(_to_file_error(error, resolved))
        except Exception as error:
            return err(_to_file_error(error, resolved))

    async def createTempDir(self, prefix: str = "tmp-", abortSignal: Any | None = None):
        aborted = _abort_result(abortSignal)
        if aborted:
            return aborted
        try:
            return ok(await asyncio.to_thread(tempfile.mkdtemp, prefix=prefix))
        except Exception as error:
            return err(_to_file_error(error))

    async def createTempFile(self, options: dict[str, Any] | None = None):
        opts = dict(options or {})
        temp_dir = await self.createTempDir("tmp-", opts.get("abortSignal"))
        if not temp_dir.ok:
            return temp_dir
        file_path = os.path.join(
            temp_dir.value,
            f"{opts.get('prefix', '')}{uuid.uuid4()}{opts.get('suffix', '')}",
        )
        try:
            async with aiofiles.open(file_path, "w", encoding="utf-8") as handle:
                await handle.write("")
            return ok(file_path)
        except Exception as error:
            return err(_to_file_error(error, file_path))

    async def cleanup(self) -> None:
        return None


LocalExecutionEnv = NodeExecutionEnv

__all__ = ["LocalExecutionEnv", "NodeExecutionEnv"]
