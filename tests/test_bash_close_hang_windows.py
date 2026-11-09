"""Translated from pi's ``packages/coding-agent/test/bash-close-hang-windows.test.ts`` (v1.1.0).

MISAKA fork: the command that leaves a detached grandchild holding the shell's stdio is a Python one-liner (pi's is a
``node -e`` one); the grandchild inherits the three standard handles the same way ``stdio: "inherit"`` does.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from misaka.core.bash_executor import executeBashWithOperations
from misaka.core.tools.bash import createBashTool, createLocalBashOperations
from misaka.utils.js import AbortController
from misaka.utils.values import read_field

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows child-process close handling")


def toBashSingleQuotedArg(value: str) -> str:
    return "'" + value.replace("\\", "/").replace("'", "'\"'\"'") + "'"


def createInheritedStdioCommand(pidFile: str) -> str:
    code = (
        "import subprocess,sys;"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
        "stdin=sys.stdin,stdout=sys.stdout,stderr=sys.stderr,close_fds=False,creationflags=0x8|0x200);"
        "open(sys.argv[1],'w').write(str(child.pid));"
        "print('child-exiting',flush=True)"
    )
    return f'{toBashSingleQuotedArg(sys.executable)} -c "{code}" {toBashSingleQuotedArg(pidFile)}'


def cleanupDetachedChild(pidFile: str) -> None:
    if not os.path.exists(pidFile):
        return

    try:
        pid = int(Path(pidFile).read_text(encoding="utf-8").strip())
    except ValueError:
        return
    if pid > 0:
        # Process may have already exited.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
        )


async def withTimeout(awaitable: Any, ms: int, onTimeout: Any) -> Any:
    try:
        return await asyncio.wait_for(awaitable, ms / 1000)
    except TimeoutError:
        onTimeout()
        raise RuntimeError(f"Timed out after {ms}ms") from None


def getTextOutput(result: Any) -> str:
    return "\n".join(
        read_field(block, "text") or ""
        for block in read_field(result, "content") or []
        if read_field(block, "type") == "text"
    )


async def test_execute_bash_resolves_after_the_shell_exits_even_if_inherited_stdio_handles_stay_open(tmp_path: Path):
    pidFile = str(tmp_path / "executor-grandchild.pid")
    command = createInheritedStdioCommand(pidFile)
    controller = AbortController()

    try:
        result = await withTimeout(
            executeBashWithOperations(command, os.getcwd(), createLocalBashOperations(), {"signal": controller.signal}),
            3000,
            controller.abort,
        )

        assert "child-exiting" in result.output
        assert result.exitCode == 0
        assert result.cancelled is False
    finally:
        controller.abort()
        cleanupDetachedChild(pidFile)


async def test_bash_tool_resolves_after_the_shell_exits_even_if_inherited_stdio_handles_stay_open(tmp_path: Path):
    pidFile = str(tmp_path / "tool-grandchild.pid")
    command = createInheritedStdioCommand(pidFile)
    controller = AbortController()
    bashTool = createBashTool(str(tmp_path))

    try:
        result = await withTimeout(
            bashTool.execute("test-call", {"command": command}, controller.signal), 3000, controller.abort
        )

        assert "child-exiting" in getTextOutput(result)
    finally:
        controller.abort()
        cleanupDetachedChild(pidFile)
