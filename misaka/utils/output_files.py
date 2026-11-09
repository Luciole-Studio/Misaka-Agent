"""Output files: files misaka writes so the model can use output it was not shown in full, such as the
full text of truncated tool output, binary MCP resources, and images shown by codemode scripts.
Every output file is created here, so where they are stored can change in one place. Today they
go to the OS temp directory.

Translated from pi's ``packages/coding-agent/src/utils/output-files.ts`` (v1.1.0). MISAKA fork: the stream is a
binary file object (callers that write text encode it as UTF-8), and ``writeOutputFile`` writes on a worker thread.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import tempfile
from typing import BinaryIO

# Output can carry private data, so only the user may read the files.
OUTPUT_FILE_MODE = 0o600

# `wx`: create the file, and fail rather than follow a link someone else placed at the path.
_EXCLUSIVE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)


def _createOutputFilePath(prefix: str, extension: str) -> str:
    """A new, unused path: `<dir>/<prefix>-<random hex><extension>`. `extension` includes the dot."""
    return os.path.join(tempfile.gettempdir(), f"{prefix}-{secrets.token_hex(8)}{extension}")


# Node writes a lone surrogate of a string as U+FFFD, where `str.encode` would raise.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _writeFile(path: str, data: str | bytes) -> None:
    descriptor = os.open(path, _EXCLUSIVE_FLAGS, OUTPUT_FILE_MODE)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(_LONE_SURROGATE.sub("\ufffd", data).encode("utf-8") if isinstance(data, str) else data)


async def writeOutputFile(prefix: str, extension: str, data: str | bytes) -> str:
    """Write `data` to a new output file and return its path."""
    path = _createOutputFilePath(prefix, extension)
    await asyncio.to_thread(_writeFile, path, data)
    return path


def createOutputFileStream(prefix: str, extension: str) -> tuple[str, BinaryIO]:
    """Open a new output file for streamed output. Returns the path and the stream (upstream's `{ path, stream }`)."""
    path = _createOutputFilePath(prefix, extension)
    descriptor = os.open(path, _EXCLUSIVE_FLAGS, OUTPUT_FILE_MODE)
    return path, os.fdopen(descriptor, "wb")


__all__ = ["OUTPUT_FILE_MODE", "createOutputFileStream", "writeOutputFile"]
