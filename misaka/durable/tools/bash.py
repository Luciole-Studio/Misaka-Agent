"""The ``bash`` and ``powershell`` tools.

Translated from pi's ``packages/durable/src/tools/bash.ts`` (v1.3.0). ``createBashTool(options)`` takes an optional
``dict`` with ``commandPrefix`` and ``prepare(execution, api, context)``; an execution is ``{"command", "cwd", "env",
"inheritEnv"}`` and ``prepare`` edits it in place. ``createPowerShellTool(options)`` also takes ``programs``.
"""

from __future__ import annotations

import math
from typing import Any

from misaka.chord.context import Context
from misaka.utils.js import awaitValue, formatNumber

from ..harness.define import defineTool
from ..truncate import DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES
from .env import requireEnv

MAX_TIMEOUT_SECONDS = 2_147_483_647 / 1000

bashSchema: dict[str, Any] = {
    "type": "object",
    "required": ["command"],
    "properties": {
        "command": {"type": "string", "description": "Bash command to execute"},
        "timeout": {"type": "number", "description": "Timeout in seconds (optional, no default timeout)"},
    },
}

powershellSchema: dict[str, Any] = {
    "type": "object",
    "required": ["command"],
    "properties": {
        "command": {"type": "string", "description": "PowerShell command to execute"},
        "timeout": {"type": "number", "description": "Timeout in seconds (optional, no default timeout)"},
    },
}


shellStructuredOutputSchema: dict[str, Any] = {
    "type": "object",
    "required": ["output", "truncated", "exitCode"],
    "properties": {
        "output": {"type": "string", "description": "Combined stdout and stderr: the retained tail, as the model sees it"},
        "truncated": {"type": "boolean", "description": "Whether earlier output was dropped"},
        "fullOutputPath": {"type": "string", "description": "File with the complete output, when it was spilled"},
        "exitCode": {"type": "number"},
    },
}
"""What a program that calls ``bash`` or ``powershell`` receives (``ShellStructuredOutput``)."""


def validateTimeout(timeout: float | None) -> None:
    if timeout is None:
        return
    if not math.isfinite(timeout) or timeout <= 0:
        raise RuntimeError("Invalid timeout: must be a finite number of seconds")
    if timeout > MAX_TIMEOUT_SECONDS:
        raise RuntimeError(f"Invalid timeout: maximum is {formatNumber(MAX_TIMEOUT_SECONDS)} seconds")


async def prepareExecution(command: str, options: dict[str, Any] | None, api: Any, context: Context) -> dict[str, Any]:
    """The execution of one call: the command with its prefix, in the environment's working directory, then
    ``prepare``."""
    prefix = None if options is None else options.get("commandPrefix")
    execution: dict[str, Any] = {
        "command": f"{prefix}\n{command}" if prefix else command,
        "cwd": requireEnv(api).cwd,
        "env": {},
        "inheritEnv": True,
    }
    if options is not None and options.get("prepare") is not None:
        await awaitValue(options["prepare"](execution, api, context))
    return execution


async def runCommand(
    commands: list[str | list[str]], execution: dict[str, Any], timeout: float | None, api: Any, context: Context
) -> dict[str, Any]:
    """Run each command form in turn until one starts, streaming output to ``api.output()`` within the retained window,
    and turn the result into the tool's result: a spill diagnostic, an error result for a nonzero exit, and a thrown
    error for a failure such as a timeout. The structured output repeats the retained output with the exit code."""
    env = requireEnv(api)

    def onOutput(text: str, _context: Any, info: dict[str, Any]) -> None:
        api.output(text, info.get("skipped"))

    result: dict[str, Any] | None = None
    for command in commands:
        result = await env.exec(
            command,
            {
                "cwd": execution["cwd"],
                "env": execution["env"],
                "inheritEnv": execution["inheritEnv"],
                **({} if timeout is None else {"timeout": timeout}),
                "onOutput": onOutput,
                "spill": {"afterBytes": DEFAULT_MAX_BYTES, "afterLines": DEFAULT_MAX_LINES},
                # An environment may then omit output outside the retained tail and report the omission.
                **({} if api.outputWindow is None else {"window": api.outputWindow}),
            },
            context,
        )
        # A program that could not start produced no output; the next form may.
        if result["ok"] or result["error"].code != "spawn_error":
            break
    if result is None:
        raise RuntimeError("No command to run")
    spillPath = result["value"].get("spillPath") if result["ok"] else result["error"].spillPath
    if spillPath is not None:
        api.diagnostic({"severity": "info", "code": "full_output", "message": f"Full output: {spillPath}"})
    if not result["ok"]:
        error = result["error"]
        signal = context.abortSignal
        if error.code == "aborted" and signal is not None and signal.aborted:
            raise error
        if error.code == "timeout":
            raise RuntimeError(f"Command timed out after {formatNumber(timeout or 0)} seconds")
        if error.code == "aborted":
            raise RuntimeError("Command aborted")
        raise error
    exitCode = result["value"]["exitCode"]
    retained = api.retainedOutput()
    structuredOutput = {
        "output": retained["text"],
        "truncated": retained["truncated"],
        **({} if spillPath is None else {"fullOutputPath": spillPath}),
        "exitCode": exitCode,
    }
    if exitCode == 0:
        return {"structuredOutput": structuredOutput}
    exited = {"severity": "error", "code": "exit_code", "message": f"Command exited with code {exitCode}"}
    return {"structuredOutput": structuredOutput, "isError": True, "diagnostics": [exited]}


def createBashTool(options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Runs a command through the environment's shell. Its output streams to ``api.output()``, where the Harness keeps
    the tail within the default limits; the result content is that retained output. The retained window goes to the
    environment, which may omit output outside it and report how much it omitted, so dropped counts stay exact. Output
    beyond the limits is spilled to a file whose path is reported as a diagnostic. A nonzero exit is an error result
    with the exit code; a timeout throws, which makes an error result that still carries the output and diagnostics."""

    async def execute(args: dict[str, Any], api: Any, context: Context) -> dict[str, Any]:
        validateTimeout(args.get("timeout"))
        execution = await prepareExecution(args["command"], options, api, context)
        return await runCommand([execution["command"]], execution, args.get("timeout"), api, context)

    return defineTool(
        {
            "name": "bash",
            "description": f"Execute a bash command in the current working directory. Returns combined stdout and stderr. Output is truncated to last {DEFAULT_MAX_LINES} lines or {DEFAULT_MAX_BYTES // 1024}KB (whichever is hit first). If truncated, full output is saved to a temp file. Optionally provide a timeout in seconds.",
            "parameters": bashSchema,
            "structuredOutputSchema": shellStructuredOutputSchema,
            "outputLimits": {"retain": "tail"},
            "execute": execute,
        }
    )


UTF8_OUTPUT = "try { [Console]::OutputEncoding=[System.Text.Encoding]::UTF8 } catch {}"
"""Output in UTF-8 whatever the console's code page, as the coding agent's ``powershell`` tool does."""
POWERSHELL_ARGS = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]


def createPowerShellTool(options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Runs a PowerShell command, like ``bash`` but through PowerShell instead of the environment's shell: ``pwsh``
    (PowerShell 7), else Windows PowerShell, started directly with the command as an argument, so no other shell parses
    it. ``options["programs"]`` are the PowerShell programs to try in order (default ``pwsh``, then ``powershell``); a
    program that cannot start is skipped."""
    programs = ["pwsh", "powershell"] if options is None or options.get("programs") is None else options["programs"]

    async def execute(args: dict[str, Any], api: Any, context: Context) -> dict[str, Any]:
        validateTimeout(args.get("timeout"))
        execution = await prepareExecution(args["command"], options, api, context)
        script = f"{UTF8_OUTPUT}\n{execution['command']}"
        commands: list[str | list[str]] = [[program, *POWERSHELL_ARGS, script] for program in programs]
        return await runCommand(commands, execution, args.get("timeout"), api, context)

    return defineTool(
        {
            "name": "powershell",
            "description": f"Execute a PowerShell command in the current working directory. Returns combined stdout and stderr. Output is truncated to last {DEFAULT_MAX_LINES} lines or {DEFAULT_MAX_BYTES // 1024}KB (whichever is hit first). If truncated, full output is saved to a temp file. Optionally provide a timeout in seconds.",
            "parameters": powershellSchema,
            "structuredOutputSchema": shellStructuredOutputSchema,
            "outputLimits": {"retain": "tail"},
            "execute": execute,
        }
    )


__all__ = ["createBashTool", "createPowerShellTool"]
