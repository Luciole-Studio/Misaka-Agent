"""Translated from pi's ``coding-agent/test/external-editor.test.ts`` (v1.1.0).

The fake editor is ``tests/fixtures/fake_external_editor.py``, run with this Python.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from typing import Any

import pytest

from misaka.modes.interactive.external_editor import editInExternalEditor

editorFixturePath = os.path.join(os.path.dirname(__file__), "fixtures", "fake_external_editor.py")


async def runExternalEditor(fixtureFlag: str | None = None) -> tuple[Any, dict[str, Any]]:
    testDirectory = tempfile.mkdtemp(prefix="pi-external-editor-test-")
    capturePath = os.path.join(testDirectory, "capture.json")
    try:
        result = await editInExternalEditor(
            {
                "command": f"{sys.executable} {editorFixturePath} {capturePath}{f' {fixtureFlag}' if fixtureFlag else ''}",
                "content": "original",
            }
        )
        with open(capturePath, encoding="utf-8") as file:
            capture = json.load(file)
        return result, capture
    finally:
        shutil.rmtree(testDirectory, ignore_errors=True)


async def test_edits_a_prompt_inside_a_private_temporary_directory():
    result, capture = await runExternalEditor()
    directory = os.path.dirname(capture["filePath"])

    assert result == {"status": "complete", "content": "edited"}
    assert os.path.normcase(os.path.dirname(directory)) == os.path.normcase(tempfile.gettempdir())
    assert re.fullmatch(r"pi-editor-.+", os.path.basename(directory))
    assert os.path.basename(capture["filePath"]) == "prompt.md"
    assert capture["entries"] == ["prompt.md"]
    assert capture["content"] == "original"
    if sys.platform != "win32":
        assert capture["directoryMode"] & 0o077 == 0
    assert not os.path.exists(directory)


async def test_keeps_the_original_content_when_the_editor_exits_unsuccessfully():
    result, capture = await runExternalEditor("--fail")

    assert result == {"status": "failed"}
    assert not os.path.exists(os.path.dirname(capture["filePath"]))


async def test_returns_empty_content_when_the_editor_clears_the_prompt():
    result, _capture = await runExternalEditor("--empty")

    assert result == {"status": "complete", "content": ""}


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe reads the command's quotes")
async def test_a_quoted_editor_path_with_a_space_runs_on_windows(tmp_path):
    folder = tmp_path / "editor dir"
    folder.mkdir()
    editor = shutil.copy(editorFixturePath, folder / "fake editor.py")
    capturePath = tmp_path / "capture.json"

    result = await editInExternalEditor({"command": f'"{sys.executable}" "{editor}" "{capturePath}"', "content": "original"})

    assert result == {"status": "complete", "content": "edited"}
    assert json.loads(capturePath.read_text(encoding="utf-8"))["content"] == "original"
