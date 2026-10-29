"""Translated from pi's ``packages/coding-agent/src/core/tools/bash.ts`` (v1.1.0).

MISAKA forks:
- Managed background commands (CCB's ``run_in_background``): ``BackgroundBashToolInput``, the ``startBackground`` and
  ``registerForeground`` options and their branches in ``execute``, and the description sentence about them.
- A skill's runtime (its venv, a remote host) shapes the command's environment and is made ready before it runs.
- Output files are named after misaka.
"""

from __future__ import annotations

import asyncio
import math
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools._common import _drain_worker, abort_race
from misaka.core.tools.output_accumulator import (
    OutputAccumulator,
    OutputAccumulatorOptions,
    OutputSnapshot,
)
from misaka.core.tools.renderers.bash import (
    BASH_UPDATE_THROTTLE_MS,
    createShellRenderers,
)
from misaka.core.tools.tool_definition_wrapper import wrapToolDefinition
from misaka.core.tools.truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    TruncationResult,
    formatSize,
)
from misaka.utils.child_process import spawn_child_process, waitForChildProcess
from misaka.utils.js import formatNumber, jsRound
from misaka.utils.shell import (
    ShellConfig,
    getShellConfig,
    getShellEnv,
    killProcessTree,
    normalize_command_for_stdin,
    trackDetachedChildPid,
    untrackDetachedChildPid,
)
from misaka.utils.values import read_field, semantic_boolean, signal_aborted

MAX_TIMEOUT_MS = 2_147_483_647
# Output limit of `structuredContent.output`, which programmatic callers such as codemode scripts receive.
STRUCTURED_OUTPUT_MAX_BYTES = 1024 * 1024
MAX_TIMEOUT_SECONDS = MAX_TIMEOUT_MS / 1000


def resolveTimeoutMs(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    if not math.isfinite(timeout) or timeout <= 0:
        raise RuntimeError("Invalid timeout: must be a finite number of seconds")

    timeoutMs = timeout * 1000
    if timeoutMs > MAX_TIMEOUT_MS:
        raise RuntimeError(f"Invalid timeout: maximum is {formatNumber(MAX_TIMEOUT_SECONDS)} seconds")
    return timeoutMs


class BashToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    command: str = Field(description="Shell command to execute")
    timeout: float | None = Field(default=None, description="Timeout in seconds (optional, no default timeout)")


bashToolSystemPromptContribution = {
    "snippet": "Execute bash commands (ls, grep, find, etc.)",
    "guidelines": ["You can inspect PI_* environment variables for current model and session details."],
}


# MISAKA fork: the background-capable input misaka's managed bash uses (CCB's run_in_background/description);
# pi's schema is BashToolInput alone.
class BackgroundBashToolInput(BashToolInput):
    # CCB semanticBoolean accepts exactly the two boolean strings, not 0/1/yes. A model's tool call is converted first,
    # as every tool's is upstream (pi's `Value.Convert`, misaka.ai.utils.typebox_convert), which also reads "0"/"1"
    # and 0/1 as booleans; the strings are left to this validator only when the model is validated directly.
    timeout: float | None = Field(default=None, description="Foreground wait in seconds; managed commands default to BASH_DEFAULT_TIMEOUT_MS / 1000 (120 seconds if unset). Eligible commands continue in the background at timeout.")
    run_in_background: bool = Field(default=False, strict=True, description="Run in the background; use TaskOutput or TaskStop with the returned task ID")
    description: str | None = Field(default=None, description="Short description of this command")

    _semantic_boolean = field_validator("run_in_background", mode="before")(semantic_boolean)


# Result for programmatic callers such as codemode scripts. A non-zero exit code is an error result for the model, but
# scripts still resolve to this value.
# `output` is not limited like the model-facing output: callers decide how much of it reaches the model.
bashOutputSchema: dict[str, Any] = {
    "type": "object",
    "properties": {
        "output": {"type": "string", "description": "Combined stdout and stderr, possibly truncated"},
        "truncated": {"type": "boolean"},
        "full_output_path": {"type": "string", "description": "Full output, when truncated"},
        "exit_code": {"type": "number"},
        "wall_time_seconds": {"type": "number"},
    },
    "required": ["output", "truncated", "exit_code", "wall_time_seconds"],
}


class BashToolOutput(TypedDict, total=False):
    output: str
    truncated: bool
    full_output_path: str
    exit_code: int
    wall_time_seconds: float


@dataclass(slots=True)
class BashToolDetails:
    truncation: TruncationResult | None = None
    fullOutputPath: str | None = None


class BashExecOptions(TypedDict, total=False):
    onData: Callable[[bytes], None]
    signal: Any
    timeout: float
    env: dict[str, str]


class BashOperations(Protocol):
    """Pluggable operations for the bash tool.
    Override these to delegate command execution to remote systems (for example SSH).
    """

    async def exec(self, command: str, cwd: str, options: BashExecOptions) -> dict[str, int | None]:
        """Execute a command and stream output.

        Returns the exit code. Report signal terminations as 128 + signal number; a null exit code is treated as a
        failed command.
        """
        ...


def createLocalShellOperations(shellName: str, resolveShellConfig: Callable[[], ShellConfig]) -> BashOperations:
    """Shared process execution used by the built-in shell tools."""

    async def exec(command: str, cwd: str, options: BashExecOptions) -> dict[str, int | None]:
        onData = options["onData"]
        signal = options.get("signal")
        timeout = options.get("timeout")
        env = options.get("env")
        timeoutMs = resolveTimeoutMs(timeout)
        if signal_aborted(signal):
            raise RuntimeError("aborted")
        shellConfig = resolveShellConfig()
        if not os.path.exists(cwd):
            raise RuntimeError(f"Working directory does not exist: {cwd}\nCannot execute {shellName} commands.")

        commandFromStdin = shellConfig.commandTransport == "stdin"
        child = await spawn_child_process(
            shellConfig.shell,
            *(shellConfig.args if commandFromStdin else [*shellConfig.args, command]),
            cwd=cwd,
            start_new_session=os.name != "nt",
            env=dict(env if env is not None else getShellEnv()),
            stdin=subprocess.PIPE if commandFromStdin else subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            on_data=lambda _fd, data: onData(data),
        )
        stdinError: BaseException | None = None
        if commandFromStdin:
            try:
                # MISAKA fork: lone UTF-16 surrogates are replaced as Node's stream encoder does.
                child.write_stdin(normalize_command_for_stdin(command).encode("utf-8"))
            except OSError:
                pass
            except BaseException as error:  # noqa: BLE001 - preserve the local-operations contract
                stdinError = error
            finally:
                try:
                    child.close_stdin()
                except OSError:
                    pass
                except BaseException as error:  # noqa: BLE001 - report writer failures after cleanup
                    stdinError = error
        if child.pid is not None:
            trackDetachedChildPid(child.pid)
        timedOut = False

        # Set timeout if provided.
        timeoutHandle = asyncio.create_task(asyncio.sleep(timeoutMs / 1000)) if timeoutMs is not None else None
        # Handle shell spawn errors and wait for the process to terminate without hanging
        # on inherited stdio handles held by detached descendants.
        waitTask = asyncio.create_task(waitForChildProcess(child))
        # Handle abort signal by killing the entire process tree.
        async with abort_race(signal) as abortTask:
            primaryError: BaseException | None = None
            try:
                pending: set[asyncio.Task[Any]] = {waitTask}
                if abortTask is not None:
                    pending.add(abortTask)
                if timeoutHandle is not None:
                    pending.add(timeoutHandle)
                done, _pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

                timedOut = timeoutHandle is not None and timeoutHandle in done
                abortRequested = (abortTask is not None and abortTask in done) or signal_aborted(signal)
                if (timedOut or abortRequested) and child.pid is not None:
                    killProcessTree(child.pid)
                exitCode = await waitTask

                if signal_aborted(signal):
                    raise RuntimeError("aborted")
                if timedOut:
                    raise RuntimeError(f"timeout:{formatNumber(timeout)}")
                # A signal-killed shell has no exit code. Use the standard shell convention so
                # callers do not mistake the termination for a successful command.
                # MISAKA fork: asyncio reports a signal death as the negative signal number.
                if exitCode is not None and exitCode < 0:
                    return {"exitCode": 128 + (-exitCode)}
                return {"exitCode": exitCode if exitCode is not None else 1}
            except BaseException as error:
                primaryError = error
                if isinstance(error, asyncio.CancelledError):
                    try:
                        if child.pid is not None:
                            killProcessTree(child.pid)
                    finally:
                        child.close()
                raise
            finally:
                async def cleanup() -> None:
                    tasks: list[asyncio.Task[Any]] = [waitTask]
                    for task in (abortTask, timeoutHandle):
                        if task is None:
                            continue
                        if not task.done():
                            task.cancel()
                        tasks.append(task)
                    await asyncio.gather(*tasks, return_exceptions=True)

                cancelledDuringCleanup = await _drain_worker(asyncio.create_task(cleanup()))
                if child.pid is not None:
                    untrackDetachedChildPid(child.pid)
                if primaryError is None:
                    if cancelledDuringCleanup:
                        raise asyncio.CancelledError
                    if stdinError is not None:
                        raise stdinError

    return SimpleNamespace(exec=exec)  # type: ignore[return-value]


def createLocalBashOperations(options: Mapping[str, Any] | None = None) -> BashOperations:
    """Create bash operations using pi's built-in local shell execution backend.

    This is useful for extensions that intercept user_bash and still want pi's
    standard local shell behavior while wrapping or rewriting commands.
    """
    return createLocalShellOperations("bash", lambda: getShellConfig(options.get("shellPath") if options else None))


@dataclass(slots=True)
class BashSpawnContext:
    command: str
    cwd: str
    env: dict[str, str]


type BashSpawnHook = Callable[[BashSpawnContext], BashSpawnContext]


def resolveSpawnContext(
    command: str,
    cwd: str,
    spawnHook: BashSpawnHook | None,
    exposeSessionEnvironment: bool,
    ctx: Any,
) -> BashSpawnContext:
    env = {**getShellEnv()}
    env.pop("PI_SESSION_ID", None)
    env.pop("PI_SESSION_FILE", None)
    env.pop("PI_PROVIDER", None)
    env.pop("PI_MODEL", None)
    env.pop("PI_REASONING_LEVEL", None)
    if exposeSessionEnvironment and ctx:
        model = read_field(ctx, "model")
        sessionManager = read_field(ctx, "sessionManager")
        env["PI_SESSION_ID"] = read_field(sessionManager, "getSessionId")()
        sessionFile = read_field(sessionManager, "getSessionFile")()
        if sessionFile:
            env["PI_SESSION_FILE"] = sessionFile
        if model:
            env["PI_PROVIDER"] = read_field(model, "provider")
            env["PI_MODEL"] = read_field(model, "id")
        if read_field(ctx, "thinkingLevel"):
            env["PI_REASONING_LEVEL"] = read_field(ctx, "thinkingLevel")
    # MISAKA fork: a skill's runtime (its venv, a remote host) shapes the environment the command runs in.
    runtime = getattr(read_field(ctx, "sessionManager"), "_skill_runtime", None) if ctx is not None else None
    if runtime is not None:
        env = runtime.execution_env(env)
    baseContext = BashSpawnContext(command=command, cwd=cwd, env=env)
    return spawnHook(baseContext) if spawnHook else baseContext


@dataclass(slots=True)
class BashToolOptions:
    # Custom operations for command execution. Default: local shell
    operations: BashOperations | None = None
    # Command prefix prepended to every command (for example shell setup commands)
    commandPrefix: str | None = None
    # Optional explicit shell path from settings
    shellPath: str | None = None
    # Expose current Pi session metadata as PI_* environment variables. Default: true
    exposeSessionEnvironment: bool | None = None
    # Hook to adjust command, cwd, or env before execution
    spawnHook: BashSpawnHook | None = None
    # MISAKA fork: managed background commands (see the module docstring).
    startBackground: Callable[..., AgentToolResult] | None = None
    registerForeground: Callable[..., Any] | None = None


def _coerce_options(options: BashToolOptions | Mapping[str, Any] | None) -> BashToolOptions | None:
    """An option bag passed as a mapping, as JavaScript callers pass an object literal."""
    if options is None or isinstance(options, BashToolOptions):
        return options
    return BashToolOptions(**{name: options.get(name) for name in BashToolOptions.__dataclass_fields__})


@dataclass(slots=True)
class BashRenderState:
    startedAt: float | None = None
    endedAt: float | None = None
    interval: Any = None


@dataclass(slots=True)
class ShellToolConfig:
    name: str
    label: str
    shellName: str
    prompt: str
    promptSnippet: str
    promptGuidelines: tuple[str, ...] | None = None
    # MISAKA fork: a default, where upstream requires the field; output files are named after misaka.
    tempFilePrefix: str = "misaka-shell"


def createShellToolDefinition(
    cwd: str,
    config: ShellToolConfig,
    options: BashToolOptions | Mapping[str, Any] | None = None,
) -> ToolDefinition[dict[str, Any], BashToolDetails | None]:
    options = _coerce_options(options)
    ops = (options.operations if options else None) or createLocalBashOperations(
        {"shellPath": options.shellPath if options else None}
    )
    commandPrefix = options.commandPrefix if options else None
    exposeSessionEnvironment = (
        options.exposeSessionEnvironment if options and options.exposeSessionEnvironment is not None else True
    )
    spawnHook = options.spawnHook if options else None
    # MISAKA fork: managed background commands.
    startBackground = options.startBackground if options else None
    registerForeground = options.registerForeground if options else None
    inputModel = BackgroundBashToolInput if startBackground is not None else BashToolInput

    async def execute(
        _toolCallId: str,
        params: dict[str, Any],
        signal: Any | None = None,
        onUpdate: Callable[[AgentToolResult], None] | None = None,
        ctx: Any = None,
    ) -> AgentToolResult:
        parsed = inputModel.model_validate(params)
        command, timeout = parsed.command, parsed.timeout
        resolvedCommand = f"{commandPrefix}\n{command}" if commandPrefix else command
        spawnContext = resolveSpawnContext(
            resolvedCommand,
            read_field(ctx, "cwd") or cwd,
            spawnHook,
            exposeSessionEnvironment,
            ctx,
        )
        # MISAKA fork: a skill whose runtime is remote gets its host ready before the command runs.
        runtime = getattr(read_field(ctx, "sessionManager"), "_skill_runtime", None) if ctx is not None else None
        if runtime is not None and runtime.remote is not None:
            from misaka.utils.async_lifecycle import run_in_thread
            await run_in_thread(runtime.prepare_remote)
        if isinstance(parsed, BackgroundBashToolInput) and parsed.run_in_background:
            if signal_aborted(signal):
                raise RuntimeError("Command aborted")
            # MISAKA fork: a backgrounded command has not finished, so its result carries a task
            # id rather than the declared `structuredContent`; the output arrives later.
            return startBackground(  # type: ignore[misc]
                command=command, description=parsed.description, context=ctx,
                spawn_context=spawnContext, operations=ops, timeout=_resolve_timeout_seconds(timeout),
            )
        output = OutputAccumulator(OutputAccumulatorOptions(tempFilePrefix=config.tempFilePrefix))
        acceptingOutput = True
        loop = asyncio.get_running_loop()
        updateTimer: asyncio.TimerHandle | None = None
        updateDirty = False
        lastUpdateAt = 0.0

        def emitOutputUpdate() -> None:
            nonlocal updateDirty, lastUpdateAt
            if onUpdate is None or not updateDirty:
                return
            updateDirty = False
            lastUpdateAt = loop.time()
            snapshot = output.snapshot(persistIfTruncated=True)
            onUpdate(
                AgentToolResult(
                    content=[TextContent(text=snapshot.content or "")],
                    details=BashToolDetails(
                        truncation=snapshot.truncation if snapshot.truncation.truncated else None,
                        fullOutputPath=snapshot.fullOutputPath,
                    ),
                )
            )

        def clearUpdateTimer() -> None:
            nonlocal updateTimer
            if updateTimer is not None:
                updateTimer.cancel()
                updateTimer = None

        def scheduleOutputUpdate() -> None:
            nonlocal updateDirty, updateTimer
            if onUpdate is None:
                return
            updateDirty = True
            delay = BASH_UPDATE_THROTTLE_MS / 1000 - (loop.time() - lastUpdateAt)
            if delay <= 0:
                clearUpdateTimer()
                emitOutputUpdate()
                return

            def fire() -> None:
                nonlocal updateTimer
                updateTimer = None
                emitOutputUpdate()

            if updateTimer is None:
                updateTimer = loop.call_later(delay, fire)

        if onUpdate is not None:
            onUpdate(AgentToolResult(content=[], details=None))

        def handleData(data: bytes) -> None:
            if not acceptingOutput:
                return
            output.append(data)
            scheduleOutputUpdate()

        async def finishOutput() -> OutputSnapshot:
            nonlocal acceptingOutput
            acceptingOutput = False
            output.finish()
            clearUpdateTimer()
            emitOutputUpdate()
            snapshot = output.snapshot(persistIfTruncated=True)
            await output.closeTempFile()
            return snapshot

        def formatOutput(snapshot: OutputSnapshot, emptyText: str = "(no output)") -> tuple[str, BashToolDetails | None]:
            truncation = snapshot.truncation
            text = snapshot.content or emptyText
            details: BashToolDetails | None = None
            if truncation.truncated:
                details = BashToolDetails(truncation=truncation, fullOutputPath=snapshot.fullOutputPath)
                startLine = truncation.totalLines - truncation.outputLines + 1
                endLine = truncation.totalLines
                if truncation.lastLinePartial:
                    lastLineSize = formatSize(output.getLastLineBytes())
                    text += f"\n\n[Showing last {formatSize(truncation.outputBytes)} of line {endLine} (line is {lastLineSize}). Full output: {snapshot.fullOutputPath}]"
                elif truncation.truncatedBy == "lines":
                    text += f"\n\n[Showing lines {startLine}-{endLine} of {truncation.totalLines}. Full output: {snapshot.fullOutputPath}]"
                else:
                    text += f"\n\n[Showing lines {startLine}-{endLine} of {truncation.totalLines} ({formatSize(DEFAULT_MAX_BYTES)} limit). Full output: {snapshot.fullOutputPath}]"
            return text, details

        def appendStatus(text: str, status: str) -> str:
            return f"{f'{text}\n\n' if text else ''}{status}"

        startedAt = time.perf_counter()

        # MISAKA fork: a managed foreground command registers so it can move to the background at its timeout.
        foreground = None
        if registerForeground is not None:
            if signal_aborted(signal):
                raise RuntimeError("Command aborted")
            foreground = registerForeground(
                command=command, description=read_field(parsed, "description"), context=ctx,
                spawn_context=spawnContext, operations=ops, output=output, on_data=handleData,
                timeout=timeout,
            )
        try:
            exitCode: int | None
            try:
                if foreground is not None:
                    result = await foreground.wait_foreground(signal, timeout, auto_background=True)
                    if result is None:
                        from misaka.core.subagent.shell import background_result
                        clearUpdateTimer()
                        # MISAKA fork: moved to the background, as above; no `structuredContent` yet.
                        return background_result(foreground, by_user=foreground.backgrounded_by_user)
                else:
                    result = await ops.exec(
                        spawnContext.command,
                        spawnContext.cwd,
                        {
                            "onData": handleData,
                            "signal": signal,
                            "timeout": timeout,
                            "env": spawnContext.env,
                        },
                    )
                exitCode = result.get("exitCode")
            except Exception as err:
                snapshot = await finishOutput()
                text, _details = formatOutput(snapshot, "")
                if str(err) == "aborted":
                    raise RuntimeError(appendStatus(text, "Command aborted")) from None
                if str(err).startswith("timeout:"):
                    timeoutSecs = str(err).split(":")[1]
                    raise RuntimeError(appendStatus(text, f"Command timed out after {timeoutSecs} seconds")) from None
                raise

            snapshot = await finishOutput()
            outputText, details = formatOutput(snapshot)
            if exitCode is None:
                raise RuntimeError(appendStatus(outputText, "Command terminated without an exit code"))
            wallTimeSeconds = jsRound((time.perf_counter() - startedAt) * 1000 / 100) / 10
            fullOutput = await output.readFullOutput(STRUCTURED_OUTPUT_MAX_BYTES)
            structuredContent: BashToolOutput = {
                "output": fullOutput.content,
                "truncated": fullOutput.truncated,
                **(
                    {"full_output_path": snapshot.fullOutputPath}  # type: ignore[typeddict-item]
                    if fullOutput.truncated and snapshot.fullOutputPath
                    else {}
                ),
                "exit_code": exitCode,
                "wall_time_seconds": wallTimeSeconds,
            }
            if exitCode != 0:
                return AgentToolResult(
                    content=[TextContent(text=appendStatus(outputText, f"Command exited with code {exitCode}"))],
                    details=details,
                    structuredContent=structuredContent,
                    isError=True,
                )
            return AgentToolResult(content=[TextContent(text=outputText)], details=details, structuredContent=structuredContent)
        finally:
            clearUpdateTimer()

    return ToolDefinition(
        name=config.name,
        label=config.label,
        description=(
            f"Execute a {config.shellName} command in the current working directory. Returns stdout and stderr. Output is truncated to last {DEFAULT_MAX_LINES} lines or {formatNumber(DEFAULT_MAX_BYTES / 1024)}KB (whichever is hit first). If truncated, full output is saved to a temp file. Optionally provide a timeout in seconds."
            # MISAKA fork: the background sentence appears only when the session runs managed background commands.
            + (" run_in_background=true returns a task ID immediately; completion arrives automatically. "
               "TaskOutput reads output and TaskStop stops the command. Eligible foreground commands also continue "
               "in the background when the foreground wait times out (default 120 seconds; BASH_DEFAULT_TIMEOUT_MS overrides)." if startBackground is not None else "")
        ),
        promptSnippet=config.promptSnippet,
        promptGuidelines=(
            [*config.promptGuidelines] if exposeSessionEnvironment and config.promptGuidelines else None
        ),
        parameters=inputModel,
        outputSchema=bashOutputSchema,
        constrainedSampling={"type": "json_schema", "strict": "prefer"},
        execute=execute,
        **createShellRenderers(config.prompt),
    )


bashToolConfig = ShellToolConfig(
    name="bash",
    label="bash",
    shellName="bash",
    prompt="$",
    promptSnippet=bashToolSystemPromptContribution["snippet"],
    promptGuidelines=tuple(bashToolSystemPromptContribution["guidelines"]),
    # MISAKA fork: output files are named after misaka (upstream: `pi-bash`).
    tempFilePrefix="misaka-bash",
)


def createBashToolDefinition(
    cwd: str,
    options: BashToolOptions | Mapping[str, Any] | None = None,
) -> ToolDefinition[dict[str, Any], BashToolDetails | None]:
    return createShellToolDefinition(cwd, bashToolConfig, options)


def createBashTool(cwd: str, options: BashToolOptions | Mapping[str, Any] | None = None) -> AgentTool:
    definition = createBashToolDefinition(cwd, options)
    tool = wrapToolDefinition(definition)
    object.__setattr__(tool, "promptSnippet", definition.promptSnippet)
    object.__setattr__(tool, "promptGuidelines", definition.promptGuidelines)
    return tool


# MISAKA fork: misaka/core/subagent imports these names and that package is not edited. Its background tasks count
# their timeout in seconds, so the check is resolveTimeoutMs's and the value stays in seconds.
def _resolve_timeout_seconds(timeout: float | None) -> float | None:
    resolveTimeoutMs(timeout)
    return timeout


_resolve_spawn_context = resolveSpawnContext
create_local_bash_operations = createLocalBashOperations


__all__ = [
    "BashOperations",
    "BashRenderState",
    "BashSpawnContext",
    "BashSpawnHook",
    "BashToolDetails",
    "BashToolInput",
    "BashToolOptions",
    "BashToolOutput",
    "ShellToolConfig",
    "bashToolSystemPromptContribution",
    "createBashTool",
    "createBashToolDefinition",
    "createLocalBashOperations",
    "createLocalShellOperations",
    "createShellToolDefinition",
]
