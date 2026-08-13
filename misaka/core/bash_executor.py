"""Bash execution helpers shared by interactive and non-interactive modes."""

from __future__ import annotations

import codecs
import os
import secrets
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TypedDict

from misaka.core.tools.bash import BashOperations
from misaka.core.tools.truncate import DEFAULT_MAX_BYTES, truncate_tail
from misaka.utils.ansi import strip_ansi
from misaka.utils.shell import sanitize_binary_output


class BashExecutorOptions(TypedDict, total=False):
    onChunk: Callable[[str], None]
    signal: Any


@dataclass(slots=True)
class BashResult:
    output: str
    exitCode: int | None
    cancelled: bool
    truncated: bool
    fullOutputPath: str | None = None


def _is_aborted(signal: Any | None) -> bool:
    return bool(getattr(signal, "aborted", False))


def _js_string_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


async def execute_bash_with_operations(
    command: str,
    cwd: str,
    operations: BashOperations,
    options: BashExecutorOptions | Mapping[str, Any] | None = None,
) -> BashResult:
    resolved_options = dict(options or {})
    output_chunks: list[str] = []
    output_bytes = 0
    max_output_bytes = DEFAULT_MAX_BYTES * 2
    total_bytes = 0
    temp_file_path: str | None = None
    temp_file_handle: Any | None = None
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def ensure_temp_file() -> None:
        nonlocal temp_file_path, temp_file_handle
        if temp_file_path is not None:
            return
        temp_file_path = os.path.join(tempfile.gettempdir(), f"harn-bash-{secrets.token_hex(8)}.log")
        temp_file_handle = open(temp_file_path, "w", encoding="utf-8")
        for chunk in output_chunks:
            temp_file_handle.write(chunk)

    def append_text(text: str) -> None:
        nonlocal output_bytes
        if temp_file_handle is not None:
            temp_file_handle.write(text)
        output_chunks.append(text)
        output_bytes += _js_string_length(text)
        while output_bytes > max_output_bytes and len(output_chunks) > 1:
            removed = output_chunks.pop(0)
            output_bytes -= _js_string_length(removed)
        on_chunk = resolved_options.get("onChunk")
        if callable(on_chunk):
            on_chunk(text)

    def on_data(data: bytes) -> None:
        nonlocal total_bytes
        total_bytes += len(data)
        text = sanitize_binary_output(strip_ansi(decoder.decode(data, final=False))).replace("\r", "")
        if total_bytes > DEFAULT_MAX_BYTES:
            ensure_temp_file()
        append_text(text)

    def close_temp_file() -> None:
        if temp_file_handle is None:
            return
        temp_file_handle.flush()
        temp_file_handle.close()

    try:
        try:
            result = await operations.exec(
                command,
                cwd,
                {
                    "onData": on_data,
                    "signal": resolved_options.get("signal"),
                },
            )
        except Exception:
            full_output = "".join(output_chunks)
            truncated = truncate_tail(full_output)
            if truncated.truncated:
                ensure_temp_file()
            close_temp_file()
            if _is_aborted(resolved_options.get("signal")):
                return BashResult(
                    output=truncated.content if truncated.truncated else full_output,
                    exitCode=None,
                    cancelled=True,
                    truncated=truncated.truncated,
                    fullOutputPath=temp_file_path,
                )
            raise

        full_output = "".join(output_chunks)
        truncated = truncate_tail(full_output)
        if truncated.truncated:
            ensure_temp_file()
        close_temp_file()
        cancelled = _is_aborted(resolved_options.get("signal"))
        return BashResult(
            output=truncated.content if truncated.truncated else full_output,
            exitCode=None if cancelled else result.get("exitCode"),
            cancelled=cancelled,
            truncated=truncated.truncated,
            fullOutputPath=temp_file_path,
        )
    finally:
        if temp_file_handle is not None and not temp_file_handle.closed:
            temp_file_handle.close()


executeBashWithOperations = execute_bash_with_operations

__all__ = [
    "BashExecutorOptions",
    "BashResult",
    "executeBashWithOperations",
]
