"""A prompt handed to a child process as a file, never on its command line.

pi's subagent extension (``coding-agent/examples/extensions/subagent/index.ts``,
``writePromptToTempFile``) writes a child's system prompt to a private temporary directory, passes
the path, and removes both once the child has exited. A command line is bounded -- 32,767
characters on Windows, where ``CreateProcess`` fails with WinError 206, and one argument of 128 KiB
on Linux -- while a card's continuation message carries a whole review brief, so the message goes
the same way (2026-10-29, issue #13).
"""

from __future__ import annotations

import os
import re
import tempfile

from misaka.utils.text import stripBom


def writePromptToTempFile(name: str, prompt: str) -> tuple[str, str]:
    """Write ``prompt`` to a file of its own; returns ``(directory, path)`` for
    ``removePromptFile``. Unpaired UTF-16 surrogates become U+FFFD, as Node's UTF-8 writer does."""
    directory = tempfile.mkdtemp(prefix="misaka-card-")
    safe_name = re.sub(r"[^\w.-]+", "_", name, flags=re.ASCII)
    path = os.path.join(directory, f"prompt-{safe_name}.md")
    text = prompt.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with open(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    except BaseException:
        removePromptFile((directory, path))
        raise
    return directory, path


def readPromptFile(path: str) -> str:
    """The prompt as written: UTF-8, line endings untouched, a leading BOM dropped (pi reads a
    prompt file with ``stripBom(readFileSync(path, "utf-8"))``)."""
    with open(path, encoding="utf-8", newline="") as handle:
        return stripBom(handle.read())


def removePromptFile(written: tuple[str, str] | None) -> None:
    """Remove the file and its directory, ignoring what is already gone (pi's ``finally``)."""
    if written is None:
        return
    directory, path = written
    try:
        os.unlink(path)
    except OSError:
        pass
    try:
        os.rmdir(directory)
    except OSError:
        pass


__all__ = ["readPromptFile", "removePromptFile", "writePromptToTempFile"]
