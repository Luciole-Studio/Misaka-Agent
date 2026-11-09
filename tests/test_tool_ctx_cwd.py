"""Ported from the "tool cwd resolution" cases of pi's ``packages/coding-agent/test/tools.test.ts`` (v1.1.0): every
built-in tool resolves paths against ``ctx.cwd`` when the call carries one.

MISAKA forks: grep and find need ripgrep and fd on the PATH (the managed copies live under the real home, which the
suite replaces), so those cases skip without them; on Windows ``pwd`` in Git Bash prints a POSIX path, so the bash
case looks for the directory's name there.
"""

from __future__ import annotations

import os
import pathlib
import sys
from types import SimpleNamespace

import pytest

from misaka.core.tools.bash import createBashToolDefinition
from misaka.core.tools.edit import createEditToolDefinition
from misaka.core.tools.find import createFindToolDefinition
from misaka.core.tools.grep import createGrepToolDefinition
from misaka.core.tools.ls import createLsToolDefinition
from misaka.core.tools.read import createReadToolDefinition
from misaka.core.tools.render_utils import getTextOutput
from misaka.core.tools.write import createWriteToolDefinition
from misaka.utils.tools_manager import find_tool

ROOT = os.path.abspath(os.sep)


def writeFileSync(path: str, content: str) -> None:
    pathlib.Path(path).write_text(content, encoding="utf-8")


def readFileSync(path: str) -> str:
    return pathlib.Path(path).read_text(encoding="utf-8")


def fakeCtx(cwd: str) -> SimpleNamespace:
    return SimpleNamespace(cwd=cwd)


@pytest.fixture
def testDir(tmp_path):
    return str(tmp_path)


async def test_read_uses_ctx_cwd_when_provided(testDir):
    writeFileSync(os.path.join(testDir, "ctx-cwd-read.txt"), "hello from ctx.cwd")
    tool = createReadToolDefinition(ROOT)
    result = await tool.execute("test-read-ctx-cwd", {"path": "ctx-cwd-read.txt"}, None, None, fakeCtx(testDir))
    output = getTextOutput(result, False)
    assert "hello from ctx.cwd" in output


async def test_write_uses_ctx_cwd_when_provided(testDir):
    tool = createWriteToolDefinition(ROOT)
    await tool.execute(
        "test-write-ctx-cwd",
        {"path": "ctx-cwd-write.txt", "content": "written via ctx.cwd"},
        None,
        None,
        fakeCtx(testDir),
    )
    assert readFileSync(os.path.join(testDir, "ctx-cwd-write.txt")) == "written via ctx.cwd"


async def test_edit_uses_ctx_cwd_when_provided(testDir):
    testFile = os.path.join(testDir, "ctx-cwd-edit.txt")
    writeFileSync(testFile, "old text")
    tool = createEditToolDefinition(ROOT)
    await tool.execute(
        "test-edit-ctx-cwd",
        {"path": "ctx-cwd-edit.txt", "edits": [{"oldText": "old text", "newText": "new text"}]},
        None,
        None,
        fakeCtx(testDir),
    )
    assert readFileSync(testFile) == "new text"


@pytest.mark.skipif(find_tool("rg") is None, reason="ripgrep is not on the PATH")
async def test_grep_uses_ctx_cwd_when_provided(testDir):
    writeFileSync(os.path.join(testDir, "ctx-cwd-grep.txt"), "match in ctx.cwd")
    tool = createGrepToolDefinition(ROOT)
    result = await tool.execute("test-grep-ctx-cwd", {"pattern": "match"}, None, None, fakeCtx(testDir))
    output = getTextOutput(result, False)
    assert "ctx-cwd-grep.txt" in output


@pytest.mark.skipif(find_tool("fd") is None, reason="fd is not on the PATH")
async def test_find_uses_ctx_cwd_when_provided(testDir):
    writeFileSync(os.path.join(testDir, "ctx-cwd-find.txt"), "find me")
    tool = createFindToolDefinition(ROOT)
    result = await tool.execute("test-find-ctx-cwd", {"pattern": "ctx-cwd-find.txt"}, None, None, fakeCtx(testDir))
    output = getTextOutput(result, False)
    assert "ctx-cwd-find.txt" in output


async def test_ls_uses_ctx_cwd_when_provided(testDir):
    writeFileSync(os.path.join(testDir, "ctx-cwd-ls.txt"), "list me")
    tool = createLsToolDefinition(ROOT)
    result = await tool.execute("test-ls-ctx-cwd", {}, None, None, fakeCtx(testDir))
    output = getTextOutput(result, False)
    assert "ctx-cwd-ls.txt" in output


async def test_bash_uses_ctx_cwd_when_provided(testDir):
    tool = createBashToolDefinition(ROOT, {"exposeSessionEnvironment": False})
    result = await tool.execute("test-bash-ctx-cwd", {"command": "pwd"}, None, None, fakeCtx(testDir))
    output = getTextOutput(result, False)
    assert (os.path.basename(testDir) if sys.platform == "win32" else testDir) in output
