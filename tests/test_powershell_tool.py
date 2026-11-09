"""Translated from pi's ``packages/coding-agent/test/powershell-tool.test.ts`` (v1.1.0)."""

from __future__ import annotations

import os
import sys
from typing import Any

import pytest

from misaka.core.tools.powershell import createPowerShellTool
from misaka.utils.shell import POWERSHELL_ARGS, getPowerShellConfig
from misaka.utils.values import read_field


def getTextOutput(result: Any) -> str:
    return "\n".join(
        read_field(content, "text") or ""
        for content in read_field(result, "content")
        if read_field(content, "type") == "text"
    )


def test_uses_process_local_execution_policy_bypass():
    assert POWERSHELL_ARGS == ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell runs on Windows")
async def test_executes_powershell_commands_with_utf8_output():
    config = getPowerShellConfig()
    assert config.args == POWERSHELL_ARGS

    tool = createPowerShellTool(os.getcwd())
    result = await tool.execute(
        "powershell-test", {"command": "Write-Output 'héllo €'; Get-ExecutionPolicy -Scope Process"}
    )
    output = getTextOutput(result)

    assert "héllo €" in output
    assert "Bypass" in output
