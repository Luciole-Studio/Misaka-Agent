"""Bash tool for command execution with streaming, truncation, and timeouts."""

from __future__ import annotations

import asyncio
import math
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.experimental import get_experimental_tool_sampling
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools._common import _drain_worker, _string_arg, abort_race
from misaka.core.tools.output_accumulator import (
    OutputAccumulator,
    OutputAccumulatorOptions,
    OutputSnapshot,
)
from misaka.core.tools.render_utils import get_text_output, invalid_arg_text
from misaka.core.tools.tool_definition_wrapper import wrap_tool_definition
from misaka.core.tools.truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    TruncationResult,
    format_size,
)
from misaka.ui.tui import Container, Text, truncateToWidth
from misaka.ui.tui.interactive.theme.theme import theme
from misaka.utils.child_process import spawn_child_process, wait_for_child_process
from misaka.utils.shell import (
    ShellConfig,
    get_shell_config,
    get_shell_env,
    kill_process_tree,
    normalize_command_for_stdin,
    track_detached_child_pid,
    untrack_detached_child_pid,
)
from misaka.utils.values import read_field, semantic_boolean, signal_aborted

_BASH_PREVIEW_LINES = 5
_BASH_UPDATE_THROTTLE_SECONDS = 0.1
_MAX_TIMEOUT_MS = 2_147_483_647
_MAX_TIMEOUT_SECONDS = _MAX_TIMEOUT_MS / 1000
_SESSION_ENV_KEYS = (
    "PI_SESSION_ID",
    "PI_SESSION_FILE",
    "PI_PROVIDER",
    "PI_MODEL",
    "PI_REASONING_LEVEL",
)
_SESSION_ENV_GUIDELINE = "You can inspect PI_* environment variables for current model and session details."


def _resolve_timeout_seconds(timeout: float | None) -> float | None:
    if timeout is None:
        return None
    if not math.isfinite(timeout) or timeout <= 0:
        raise RuntimeError("Invalid timeout: must be a finite number of seconds")
    if timeout > _MAX_TIMEOUT_SECONDS:
        raise RuntimeError(
            f"Invalid timeout: maximum is {_MAX_TIMEOUT_SECONDS} seconds"
        )
    return timeout


class BashToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    command: str = Field(description="Shell command to execute")
    timeout: float | None = Field(default=None, description="Timeout in seconds (optional, no default timeout)")


class BackgroundBashToolInput(BashToolInput):
    # CCB semanticBoolean accepts exactly the two boolean strings, not 0/1/yes.
    timeout: float | None = Field(default=None, description="Foreground wait in seconds; managed commands default to BASH_DEFAULT_TIMEOUT_MS / 1000 (120 seconds if unset). Eligible commands continue in the background at timeout.")
    run_in_background: bool = Field(default=False, strict=True, description="Run in the background; use TaskOutput or TaskStop with the returned task ID")
    description: str | None = Field(default=None, description="Short description of this command")

    _semantic_boolean = field_validator("run_in_background", mode="before")(semantic_boolean)


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
    async def exec(self, command: str, cwd: str, options: BashExecOptions) -> dict[str, int | None]: ...


@dataclass(slots=True)
class BashSpawnContext:
    command: str
    cwd: str
    env: dict[str, str]


type BashSpawnHook = Callable[[BashSpawnContext], BashSpawnContext]


@dataclass(slots=True)
class BashToolOptions:
    operations: BashOperations | None = None
    commandPrefix: str | None = None
    shellPath: str | None = None
    exposeSessionEnvironment: bool | None = True
    spawnHook: BashSpawnHook | None = None
    startBackground: Callable[..., AgentToolResult] | None = None
    registerForeground: Callable[..., Any] | None = None


@dataclass(slots=True)
class ShellToolConfig:
    name: str
    label: str
    shellName: str
    prompt: str
    promptSnippet: str
    promptGuidelines: tuple[str, ...] = ()
    tempFilePrefix: str = "misaka-shell"


@dataclass(slots=True)
class _BashRenderState:
    startedAt: float | None = None
    endedAt: float | None = None
    interval: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _BashResultRenderState:
    cachedWidth: int | None = None
    cachedLines: list[str] | None = None
    cachedSkipped: int | None = None


class _BashResultRenderComponent(Container):
    def __init__(self) -> None:
        super().__init__()
        self.state = _BashResultRenderState()


class _CollapsedBashPreview:
    def __init__(self, styled_output: str, state: _BashResultRenderState) -> None:
        self._styled_output = styled_output
        self._state = state

    def render(self, width: int) -> list[str]:
        from misaka.ui.tui.interactive.components.keybinding_hints import key_hint
        from misaka.ui.tui.interactive.components.visual_truncate import (
            truncate_to_visual_lines,
        )

        if self._state.cachedLines is None or self._state.cachedWidth != width:
            preview = truncate_to_visual_lines(self._styled_output, _BASH_PREVIEW_LINES, width)
            self._state.cachedLines = preview.visualLines
            self._state.cachedSkipped = preview.skippedCount
            self._state.cachedWidth = width

        if self._state.cachedSkipped and self._state.cachedSkipped > 0:
            hint = theme.fg("muted", f"... ({self._state.cachedSkipped} earlier lines,") + (
                f" {key_hint('app.tools.expand', 'to expand')})"
            )
            return ["", truncateToWidth(hint, width, "..."), *(self._state.cachedLines or [])]
        return ["", *(self._state.cachedLines or [])]

    def invalidate(self) -> None:
        self._state.cachedWidth = None
        self._state.cachedLines = None
        self._state.cachedSkipped = None


@dataclass(slots=True)
class _LocalShellOperations:
    shellName: str
    resolveShellConfig: Callable[[], ShellConfig]

    async def exec(self, command: str, cwd: str, options: BashExecOptions) -> dict[str, int | None]:
        timeout = _resolve_timeout_seconds(options.get("timeout"))
        signal = options.get("signal")
        if signal_aborted(signal):
            raise RuntimeError("aborted")
        if not os.path.exists(cwd):
            raise RuntimeError(
                f"Working directory does not exist: {cwd}\n"
                f"Cannot execute {self.shellName} commands."
            )

        shell_config = self.resolveShellConfig()
        env = options["env"] if "env" in options else get_shell_env()
        on_data = options["onData"]
        command_from_stdin = shell_config.commandTransport == "stdin"

        process = await spawn_child_process(
            shell_config.shell,
            *(
                shell_config.args
                if command_from_stdin
                else [*shell_config.args, command]
            ),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.PIPE if command_from_stdin else subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            on_data=lambda _fd, data: on_data(data),
        )
        if process.pid is not None:
            track_detached_child_pid(process.pid)

        stdin_error: BaseException | None = None
        if command_from_stdin:
            try:
                process.write_stdin(
                    normalize_command_for_stdin(command).encode("utf-8")
                )
            except OSError:
                pass
            except BaseException as error:  # noqa: BLE001 - preserve the local-operations contract
                stdin_error = error
            finally:
                try:
                    process.close_stdin()
                except OSError:
                    pass
                except BaseException as error:  # noqa: BLE001 - report writer failures after cleanup
                    stdin_error = error
        wait_task = asyncio.create_task(wait_for_child_process(process))
        timeout_task = (
            asyncio.create_task(asyncio.sleep(timeout)) if timeout is not None else None
        )
        timed_out = False

        async with abort_race(signal) as abort_task:
            primary_error: BaseException | None = None
            try:
                pending: set[asyncio.Task[Any]] = {wait_task}
                if abort_task is not None:
                    pending.add(abort_task)
                if timeout_task is not None:
                    pending.add(timeout_task)
                done, _pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

                timed_out = timeout_task is not None and timeout_task in done
                abort_won = abort_task is not None and abort_task in done
                abort_requested = abort_won or signal_aborted(signal)
                if (timed_out or abort_requested) and process.pid is not None:
                    kill_process_tree(process.pid)
                exit_code = await wait_task

                if signal_aborted(signal):
                    raise RuntimeError("aborted")
                if timed_out:
                    raise RuntimeError(f"timeout:{timeout}")
                return {"exitCode": None if exit_code is not None and exit_code < 0 else exit_code}
            except BaseException as error:
                primary_error = error
                if isinstance(error, asyncio.CancelledError):
                    try:
                        if process.pid is not None:
                            kill_process_tree(process.pid)
                    finally:
                        process.close()
                raise
            finally:
                async def cleanup() -> None:
                    tasks: list[asyncio.Task[Any]] = [wait_task]
                    for task in (abort_task, timeout_task):
                        if task is None:
                            continue
                        if not task.done():
                            task.cancel()
                        tasks.append(task)
                    await asyncio.gather(*tasks, return_exceptions=True)

                cancelled_during_cleanup = await _drain_worker(
                    asyncio.create_task(cleanup())
                )
                if process.pid is not None:
                    untrack_detached_child_pid(process.pid)
                if primary_error is None:
                    if cancelled_during_cleanup:
                        raise asyncio.CancelledError
                    if stdin_error is not None:
                        raise stdin_error


def create_local_shell_operations(
    shell_name: str,
    resolve_shell_config: Callable[[], ShellConfig],
) -> BashOperations:
    return _LocalShellOperations(
        shellName=shell_name,
        resolveShellConfig=resolve_shell_config,
    )


def create_local_bash_operations(options: Mapping[str, Any] | None = None) -> BashOperations:
    shell_path = options.get("shellPath") if options else None
    return create_local_shell_operations(
        "bash",
        lambda: get_shell_config(shell_path),
    )


def _coerce_options(options: BashToolOptions | Mapping[str, Any] | None) -> BashToolOptions:
    if options is None:
        return BashToolOptions()
    if isinstance(options, BashToolOptions):
        return options
    expose_session_environment = options.get("exposeSessionEnvironment")
    return BashToolOptions(
        operations=options.get("operations"),
        commandPrefix=options.get("commandPrefix"),
        shellPath=options.get("shellPath"),
        exposeSessionEnvironment=(
            True if expose_session_environment is None else expose_session_environment
        ),
        spawnHook=options.get("spawnHook"),
        startBackground=options.get("startBackground"),
        registerForeground=options.get("registerForeground"),
    )


def _resolve_spawn_context(
    command: str,
    cwd: str,
    spawn_hook: BashSpawnHook | None,
    expose_session_environment: bool,
    ctx: Any,
) -> BashSpawnContext:
    env = {**get_shell_env()}
    for key in _SESSION_ENV_KEYS:
        env.pop(key, None)
    if expose_session_environment and ctx is not None:
        session_manager = read_field(ctx, "sessionManager")
        env["PI_SESSION_ID"] = read_field(session_manager, "getSessionId")()
        session_file = read_field(session_manager, "getSessionFile")()
        if session_file:
            env["PI_SESSION_FILE"] = session_file
        model = read_field(ctx, "model")
        if model is not None:
            env["PI_PROVIDER"] = read_field(model, "provider")
            env["PI_MODEL"] = read_field(model, "id")
        thinking_level = read_field(ctx, "thinkingLevel")
        if thinking_level:
            env["PI_REASONING_LEVEL"] = thinking_level
    runtime = getattr(read_field(ctx, "sessionManager"), "_skill_runtime", None) if ctx is not None else None
    if runtime is not None:
        env = runtime.execution_env(env)
    base_context = BashSpawnContext(command=command, cwd=cwd, env=env)
    return spawn_hook(base_context) if spawn_hook is not None else base_context


def _make_text_result(text: str, details: BashToolDetails | None = None) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text=text)], details=details)


def _append_status(text: str, status: str) -> str:
    return f"{text}\n\n{status}" if text else status


def _now_ms() -> float:
    return time.time() * 1000


def _format_duration(ms: float) -> str:
    return f"{ms / 1000:.1f}s"


def _get_render_state(state: dict[str, Any]) -> _BashRenderState:
    render_state = state.get("bashRenderState")
    if isinstance(render_state, _BashRenderState):
        return render_state
    render_state = _BashRenderState()
    state["bashRenderState"] = render_state
    return render_state


def _create_render_interval(invalidate: Callable[[], None]) -> asyncio.Task[None] | None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None

    async def _tick() -> None:
        try:
            while True:
                await asyncio.sleep(1)
                invalidate()
        except asyncio.CancelledError:
            return

    return loop.create_task(_tick())


def _format_shell_call(args: dict[str, Any] | None, prompt: str) -> str:
    command = _string_arg(read_field(args, "command"))
    timeout = read_field(args, "timeout")
    timeout_suffix = theme.fg("muted", f" (timeout {timeout}s)") if timeout else ""
    if command is None:
        command_display = invalid_arg_text(theme)
    elif command:
        command_display = command
    else:
        command_display = theme.fg("toolOutput", "...")
    return theme.fg("toolTitle", theme.bold(f"{prompt} {command_display}")) + timeout_suffix


def _rebuild_bash_result_render_component(
    component: _BashResultRenderComponent,
    result: AgentToolResult | Mapping[str, Any],
    options: Mapping[str, Any],
    show_images: bool,
    started_at: float | None,
    ended_at: float | None,
) -> None:
    state = component.state
    component.clear()

    output = get_text_output(result, show_images).strip()
    details = read_field(result, "details")
    truncation = read_field(details, "truncation")
    full_output_path = read_field(details, "fullOutputPath")
    if (
        not bool(read_field(options, "isPartial"))
        and bool(read_field(truncation, "truncated"))
        and full_output_path
        and output.endswith("]")
    ):
        footer_start = output.rfind("\n\n[")
        if footer_start != -1 and str(full_output_path) in output[footer_start:]:
            output = output[:footer_start].rstrip()

    if output:
        styled_output = "\n".join(theme.fg("toolOutput", line) for line in output.split("\n"))
        if bool(read_field(options, "expanded")):
            component.addChild(Text(f"\n{styled_output}", 0, 0))
        else:
            component.addChild(_CollapsedBashPreview(styled_output, state))

    if bool(read_field(truncation, "truncated")) or full_output_path:
        warnings: list[str] = []
        if full_output_path:
            warnings.append(f"Full output: {full_output_path}")
        if bool(read_field(truncation, "truncated")):
            if read_field(truncation, "truncatedBy") == "lines":
                warnings.append(
                    f"Truncated: showing {read_field(truncation, 'outputLines')} of {read_field(truncation, 'totalLines')} lines"
                )
            else:
                warnings.append(
                    "Truncated: "
                    f"{read_field(truncation, 'outputLines')} lines shown "
                    f"({format_size(read_field(truncation, 'maxBytes') or DEFAULT_MAX_BYTES)} limit)"
                )
        warning_text = f"[{'. '.join(warnings)}]"
        component.addChild(Text(f"\n{theme.fg('warning', warning_text)}", 0, 0))

    if started_at is not None:
        label = "Elapsed" if bool(read_field(options, "isPartial")) else "Took"
        end_time = ended_at if ended_at is not None else _now_ms()
        component.addChild(Text(f"\n{theme.fg('muted', f'{label} {_format_duration(end_time - started_at)}')}", 0, 0))


def _render_call(args: dict[str, Any] | None, context: Any, prompt: str) -> Text:
    state = _get_render_state(context.state)
    if context.executionStarted and state.startedAt is None:
        state.startedAt = _now_ms()
        state.endedAt = None
    text = context.lastComponent if callable(getattr(context.lastComponent, "setText", None)) else Text("", 0, 0)
    text.setText(_format_shell_call(args, prompt))
    return text


def _render_result(
    result: AgentToolResult | Mapping[str, Any],
    options: Mapping[str, Any],
    context: Any,
) -> _BashResultRenderComponent:
    state = _get_render_state(context.state)
    if state.startedAt is not None and bool(read_field(options, "isPartial")) and state.interval is None:
        state.interval = _create_render_interval(context.invalidate)
    if not bool(read_field(options, "isPartial")) or context.isError:
        if state.endedAt is None:
            state.endedAt = _now_ms()
        if state.interval is not None:
            state.interval.cancel()
            state.interval = None

    component = (
        context.lastComponent
        if isinstance(context.lastComponent, _BashResultRenderComponent)
        else _BashResultRenderComponent()
    )
    _rebuild_bash_result_render_component(
        component,
        result,
        options,
        context.showImages,
        state.startedAt,
        state.endedAt,
    )
    component.invalidate()
    return component


def create_shell_tool_definition(
    cwd: str,
    config: ShellToolConfig,
    options: BashToolOptions | Mapping[str, Any] | None = None,
) -> ToolDefinition[dict[str, Any], BashToolDetails | None]:
    resolved_options = _coerce_options(options)
    expose_session_environment = (
        True
        if resolved_options.exposeSessionEnvironment is None
        else resolved_options.exposeSessionEnvironment
    )
    operations = resolved_options.operations
    if operations is None:
        operations = create_local_bash_operations(
            {"shellPath": resolved_options.shellPath}
        )

    input_model = BackgroundBashToolInput if resolved_options.startBackground is not None else BashToolInput

    async def execute(
        _tool_call_id: str,
        params: dict[str, Any],
        signal: Any | None = None,
        on_update: Callable[[AgentToolResult], None] | None = None,
        _ctx: Any = None,
    ) -> AgentToolResult:
        parsed = input_model.model_validate(params)
        resolved_command = (
            f"{resolved_options.commandPrefix}\n{parsed.command}" if resolved_options.commandPrefix else parsed.command
        )
        spawn_context = _resolve_spawn_context(
            resolved_command,
            cwd,
            resolved_options.spawnHook,
            expose_session_environment,
            _ctx,
        )
        runtime = getattr(read_field(_ctx, "sessionManager"), "_skill_runtime", None) if _ctx is not None else None
        if runtime is not None and runtime.remote is not None:
            from misaka.utils.async_lifecycle import run_in_thread
            await run_in_thread(runtime.prepare_remote)
        if isinstance(parsed, BackgroundBashToolInput) and parsed.run_in_background:
            if signal_aborted(signal):
                raise RuntimeError("Command aborted")
            timeout = _resolve_timeout_seconds(parsed.timeout)
            return resolved_options.startBackground(
                command=parsed.command, description=parsed.description, context=_ctx,
                spawn_context=spawn_context, operations=operations, timeout=timeout,
            )
        output = OutputAccumulator(
            OutputAccumulatorOptions(tempFilePrefix=config.tempFilePrefix)
        )
        accepting_output = True
        loop = asyncio.get_running_loop()
        update_handle: asyncio.TimerHandle | None = None
        update_dirty = False
        last_update_at = 0.0

        def emit_output_update() -> None:
            nonlocal update_dirty, last_update_at, update_handle
            if on_update is None or not update_dirty:
                return
            update_dirty = False
            last_update_at = loop.time()
            update_handle = None
            snapshot = output.snapshot(persistIfTruncated=True)
            on_update(
                AgentToolResult(
                    content=[TextContent(text=snapshot.content or "")],
                    details=BashToolDetails(
                        truncation=snapshot.truncation if snapshot.truncation.truncated else None,
                        fullOutputPath=snapshot.fullOutputPath,
                    ),
                )
            )

        def clear_update_handle() -> None:
            nonlocal update_handle
            if update_handle is not None:
                update_handle.cancel()
                update_handle = None

        def schedule_output_update() -> None:
            nonlocal update_dirty, update_handle
            if on_update is None:
                return
            update_dirty = True
            delay = _BASH_UPDATE_THROTTLE_SECONDS - (loop.time() - last_update_at)
            if delay <= 0:
                clear_update_handle()
                emit_output_update()
                return
            if update_handle is None:
                update_handle = loop.call_later(delay, emit_output_update)

        if on_update is not None:
            on_update(AgentToolResult(content=[], details=None))

        def handle_data(data: bytes) -> None:
            if not accepting_output:
                return
            output.append(data)
            schedule_output_update()

        async def finish_output() -> OutputSnapshot:
            nonlocal accepting_output
            accepting_output = False
            output.finish()
            clear_update_handle()
            emit_output_update()
            snapshot = output.snapshot(persistIfTruncated=True)
            await output.close_temp_file()
            return snapshot

        def format_output(snapshot: OutputSnapshot, empty_text: str = "(no output)") -> tuple[str, BashToolDetails | None]:
            truncation = snapshot.truncation
            text = snapshot.content or empty_text
            details: BashToolDetails | None = None
            if truncation.truncated:
                details = BashToolDetails(truncation=truncation, fullOutputPath=snapshot.fullOutputPath)
                start_line = truncation.totalLines - truncation.outputLines + 1
                end_line = truncation.totalLines
                if truncation.lastLinePartial:
                    last_line_size = format_size(output.get_last_line_bytes())
                    text += (
                        f"\n\n[Showing last {format_size(truncation.outputBytes)} of line {end_line} "
                        f"(line is {last_line_size}). Full output: {snapshot.fullOutputPath}]"
                    )
                elif truncation.truncatedBy == "lines":
                    text += (
                        f"\n\n[Showing lines {start_line}-{end_line} of {truncation.totalLines}. "
                        f"Full output: {snapshot.fullOutputPath}]"
                    )
                else:
                    text += (
                        f"\n\n[Showing lines {start_line}-{end_line} of {truncation.totalLines} "
                        f"({format_size(DEFAULT_MAX_BYTES)} limit). Full output: {snapshot.fullOutputPath}]"
                    )
            return text, details

        foreground = None
        timeout = parsed.timeout
        if resolved_options.registerForeground is not None:
            if signal_aborted(signal):
                raise RuntimeError("Command aborted")
            foreground = resolved_options.registerForeground(
                command=parsed.command, description=read_field(parsed, "description"), context=_ctx,
                spawn_context=spawn_context, operations=operations, output=output, on_data=handle_data,
                timeout=timeout,
            )
        try:
            try:
                if foreground is not None:
                    result = await foreground.wait_foreground(signal, timeout, auto_background=True)
                    if result is None:
                        from misaka.core.subagent.shell import background_result
                        clear_update_handle()
                        return background_result(foreground, by_user=foreground.backgrounded_by_user)
                else:
                    result = await operations.exec(
                        spawn_context.command,
                        spawn_context.cwd,
                        {
                            "onData": handle_data,
                            "signal": signal,
                            "timeout": parsed.timeout,
                            "env": spawn_context.env,
                        },
                    )
                exit_code = result.get("exitCode")
            except Exception as error:
                snapshot = await finish_output()
                output_text, _details = format_output(snapshot, "")
                message = str(error)
                if message == "aborted":
                    raise RuntimeError(_append_status(output_text, "Command aborted")) from None
                if message.startswith("timeout:"):
                    timeout_secs = message.split(":", 1)[1]
                    raise RuntimeError(_append_status(output_text, f"Command timed out after {timeout_secs} seconds")) from None
                raise

            snapshot = await finish_output()
            output_text, details = format_output(snapshot)
            if exit_code not in {0, None}:
                raise RuntimeError(_append_status(output_text, f"Command exited with code {exit_code}"))
            return _make_text_result(output_text, details)
        finally:
            clear_update_handle()

    return ToolDefinition(
        name=config.name,
        label=config.label,
        description=(
            f"Execute a {config.shellName} command in the current working directory. "
            "Returns stdout and stderr. "
            f"Output is truncated to last {DEFAULT_MAX_LINES} lines or {DEFAULT_MAX_BYTES // 1024}KB "
            "(whichever is hit first). If truncated, full output is saved to a temp file. "
            "Optionally provide a timeout in seconds."
            + (" run_in_background=true returns a task ID immediately; completion arrives automatically. "
               "TaskOutput reads output and TaskStop stops the command. Eligible foreground commands also continue "
               "in the background when the foreground wait times out (default 120 seconds; BASH_DEFAULT_TIMEOUT_MS overrides)." if resolved_options.startBackground is not None else "")
        ),
        promptSnippet=config.promptSnippet,
        promptGuidelines=(
            list(config.promptGuidelines) if expose_session_environment else []
        ),
        parameters=input_model,
        constrainedSampling=get_experimental_tool_sampling(),
        execute=execute,
        renderCall=lambda args, _theme, context: _render_call(
            args, context, config.prompt
        ),
        renderResult=lambda result, render_options, _theme, context: _render_result(result, render_options, context),
    )


_BASH_TOOL_CONFIG = ShellToolConfig(
    name="bash",
    label="bash",
    shellName="bash",
    prompt="$",
    promptSnippet="Execute bash commands (ls, grep, find, etc.)",
    promptGuidelines=(_SESSION_ENV_GUIDELINE,),
    tempFilePrefix="misaka-bash",
)


def create_bash_tool_definition(
    cwd: str,
    options: BashToolOptions | Mapping[str, Any] | None = None,
) -> ToolDefinition[dict[str, Any], BashToolDetails | None]:
    return create_shell_tool_definition(cwd, _BASH_TOOL_CONFIG, options)


def create_bash_tool(cwd: str, options: BashToolOptions | Mapping[str, Any] | None = None) -> AgentTool:
    definition = create_bash_tool_definition(cwd, options)
    tool = wrap_tool_definition(definition)
    # Pi assigns these definition-only fields dynamically so registerTool(createBashTool(...))
    # keeps its system-prompt contribution.
    object.__setattr__(tool, "promptSnippet", definition.promptSnippet)
    object.__setattr__(tool, "promptGuidelines", definition.promptGuidelines)
    return tool


createBashTool = create_bash_tool
createBashToolDefinition = create_bash_tool_definition
createLocalBashOperations = create_local_bash_operations

__all__ = [
    "BashOperations",
    "BashSpawnContext",
    "BashSpawnHook",
    "BashToolDetails",
    "BashToolInput",
    "BashToolOptions",
    "ShellToolConfig",
    "createBashTool",
    "createBashToolDefinition",
    "createLocalBashOperations",
]
