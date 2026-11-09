"""Translated from pi's ``modes/interactive/external-editor.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Literal, TypedDict

from misaka.utils.text import stripBom


class ExternalEditorOptions(TypedDict):
    command: str
    content: str


class ExternalEditorComplete(TypedDict):
    status: Literal["complete"]
    content: str


class ExternalEditorFailed(TypedDict):
    status: Literal["failed"]


type ExternalEditorResult = ExternalEditorComplete | ExternalEditorFailed


async def editInExternalEditor(options: ExternalEditorOptions) -> ExternalEditorResult:
    directory = tempfile.mkdtemp(prefix="pi-editor-")
    filePath = os.path.join(directory, "prompt.md")
    try:
        with open(filePath, "w", encoding="utf-8", newline="") as file:  # noqa: ASYNC230 - upstream writeFileSync
            file.write(options["content"])
        editor, *editorArgs = options["command"].split(" ")
        # MISAKA fork: the product name in the hand-over line.
        sys.stdout.write(f"Launching external editor: {options['command']}\nMISAKA will resume when the editor exits.\n")
        sys.stdout.flush()

        # Do not use a blocking subprocess call here. On Windows, synchronous child process calls can keep the
        # console input read active after the parent pauses stdin, racing vim/nvim for the console input buffer.
        exitCode: int | None
        try:
            if sys.platform == "win32":
                # MISAKA fork: Node's `shell: true` joins the words with plain spaces, so cmd.exe reads the
                # command's own quotes (`"C:\\Program Files\\..." -w`) as written; the temp file is quoted, so
                # a user name with a space still reaches the editor as one argument.
                child = await asyncio.create_subprocess_shell(
                    " ".join([editor, *editorArgs, subprocess.list2cmdline([filePath])])
                )
            else:
                child = await asyncio.create_subprocess_exec(editor, *editorArgs, filePath)
            exitCode = await child.wait()
        except OSError:
            exitCode = None

        if exitCode != 0:
            return {"status": "failed"}

        with open(filePath, encoding="utf-8", newline="") as file:  # noqa: ASYNC230 - upstream readFileSync
            return {"status": "complete", "content": stripBom(file.read()).removesuffix("\n")}
    finally:
        # Cleanup is best effort.
        shutil.rmtree(directory, ignore_errors=True)


__all__ = [
    "ExternalEditorComplete",
    "ExternalEditorFailed",
    "ExternalEditorOptions",
    "ExternalEditorResult",
    "editInExternalEditor",
]
