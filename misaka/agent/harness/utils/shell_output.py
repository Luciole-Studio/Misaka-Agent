"""Shell execution helpers that capture and truncate output safely."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from misaka.agent.harness.types import ExecutionError, err, ok
from misaka.agent.harness.utils.truncate import DEFAULT_MAX_BYTES, truncate_tail


@dataclass(slots=True)
class ShellCaptureOptions:
    cwd: str | None = None
    env: dict[str, str] | None = None
    timeout: int | float | None = None
    abortSignal: Any | None = None
    onChunk: Any | None = None


@dataclass(slots=True)
class ShellCaptureResult:
    output: str
    exitCode: int | None
    cancelled: bool
    truncated: bool
    fullOutputPath: str | None = None


def sanitize_binary_output(value: str) -> str:
    filtered: list[str] = []
    for char in value:
        code = ord(char)
        if code in {0x09, 0x0A, 0x0D}:
            filtered.append(char)
            continue
        if code <= 0x1F:
            continue
        if 0xFFF9 <= code <= 0xFFFB:
            continue
        filtered.append(char)
    return "".join(filtered)


async def execute_shell_with_capture(
    env: Any,
    command: str,
    options: ShellCaptureOptions | dict[str, Any] | None = None,
):
    normalized = _normalize_options(options)
    output_chunks: deque[str] = deque()
    output_chars = 0
    max_output_chars = DEFAULT_MAX_BYTES * 2
    total_bytes = 0
    full_output_chunks: list[str] = []
    capture_error: ExecutionError | None = None

    def on_chunk(chunk: str) -> None:
        nonlocal output_chars, total_bytes, capture_error
        try:
            total_bytes += len(chunk.encode("utf-8", errors="replace"))
            text = sanitize_binary_output(chunk).replace("\r", "")
            full_output_chunks.append(text)
            output_chunks.append(text)
            output_chars += len(text)
            while output_chars > max_output_chars and len(output_chunks) > 1:
                removed = output_chunks.popleft()
                output_chars -= len(removed)
            if normalized.onChunk is not None:
                normalized.onChunk(text)
        except Exception as error:
            capture_error = _to_execution_error(error)

    exec_options = {
        key: value
        for key, value in {
            "cwd": normalized.cwd,
            "env": normalized.env,
            "timeout": normalized.timeout,
            "abortSignal": normalized.abortSignal,
            "onStdout": on_chunk,
            "onStderr": on_chunk,
        }.items()
        if value is not None
    }

    try:
        result = await env.exec(command, exec_options)
    except Exception as error:
        return err(_to_execution_error(error))

    tail_output = "".join(output_chunks)
    truncation_result = truncate_tail(tail_output)
    full_output_path: str | None = None
    if total_bytes > DEFAULT_MAX_BYTES or truncation_result.truncated:
        full_output_path = await _write_full_output(env, "".join(full_output_chunks), normalized.abortSignal)
        if isinstance(full_output_path, ExecutionError):
            return err(full_output_path)
    if capture_error is not None:
        return err(capture_error)

    if not result.ok:
        if result.error.code == "aborted" or _signal_aborted(normalized.abortSignal):
            return ok(
                ShellCaptureResult(
                    output=truncation_result.content if truncation_result.truncated else tail_output,
                    exitCode=None,
                    cancelled=True,
                    truncated=truncation_result.truncated,
                    fullOutputPath=full_output_path,
                )
            )
        return err(result.error)

    cancelled = _signal_aborted(normalized.abortSignal)
    return ok(
        ShellCaptureResult(
            output=truncation_result.content if truncation_result.truncated else tail_output,
            exitCode=None if cancelled else result.value["exitCode"],
            cancelled=cancelled,
            truncated=truncation_result.truncated,
            fullOutputPath=full_output_path,
        )
    )


async def _write_full_output(env: Any, content: str, abort_signal: Any | None) -> str | ExecutionError:
    temp_file = await env.createTempFile({"prefix": "bash-", "suffix": ".log", "abortSignal": abort_signal})
    if not temp_file.ok:
        return _to_execution_error(temp_file.error)
    append_result = await env.appendFile(temp_file.value, content, abort_signal)
    if not append_result.ok:
        return _to_execution_error(append_result.error)
    return temp_file.value


def _to_execution_error(error: Any) -> ExecutionError:
    if isinstance(error, ExecutionError):
        return error
    message = getattr(error, "message", None) or str(error)
    return ExecutionError("unknown", message, error if isinstance(error, Exception) else None)


def _normalize_options(options: ShellCaptureOptions | dict[str, Any] | None) -> ShellCaptureOptions:
    if isinstance(options, ShellCaptureOptions):
        return options
    if isinstance(options, dict):
        return ShellCaptureOptions(
            cwd=options.get("cwd"),
            env=options.get("env"),
            timeout=options.get("timeout"),
            abortSignal=options.get("abortSignal"),
            onChunk=options.get("onChunk"),
        )
    return ShellCaptureOptions()


def _signal_aborted(signal: Any | None) -> bool:
    return bool(getattr(signal, "aborted", False))


executeShellWithCapture = execute_shell_with_capture
sanitizeBinaryOutput = sanitize_binary_output

__all__ = [
    "ShellCaptureOptions",
    "ShellCaptureResult",
    "executeShellWithCapture",
    "execute_shell_with_capture",
    "sanitizeBinaryOutput",
    "sanitize_binary_output",
]
