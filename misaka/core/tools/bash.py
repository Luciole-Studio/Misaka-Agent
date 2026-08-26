"""Bash tool for command execution with streaming, truncation, and timeouts."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
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
from misaka.utils.child_process import wait_for_child_process
from misaka.utils.shell import (
    get_shell_config,
    get_shell_env,
    kill_process_tree,
    track_detached_child_pid,
    untrack_detached_child_pid,
)

_BASH_PREVIEW_LINES = 5
_BASH_UPDATE_THROTTLE_SECONDS = 0.1


class BashToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    command: str = Field(description="Bash command to execute")
    timeout: float | None = Field(default=None, description="Timeout in seconds (optional, no default timeout)")


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
    spawnHook: BashSpawnHook | None = None


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
class _LocalBashOperations:
    shellPath: str | None = None

    async def exec(self, command: str, cwd: str, options: BashExecOptions) -> dict[str, int | None]:
        if not os.path.exists(cwd):
            raise RuntimeError(f"Working directory does not exist: {cwd}\nCannot execute bash commands.")

        shell_config = get_shell_config(self.shellPath)
        env = options["env"] if "env" in options else get_shell_env()
        signal = options.get("signal")
        timeout = options.get("timeout")
        on_data = options["onData"]

        process = subprocess.Popen(  # noqa: ASYNC220 - the child is driven through pipes by reader threads; Popen is the intended API
            [shell_config.shell, *shell_config.args, command],
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if process.pid is not None:
            track_detached_child_pid(process.pid)

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        stream_count = 0

        def _pump_stream(stream: Any) -> None:
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        return
                    loop.call_soon_threadsafe(queue.put_nowait, bytes(chunk))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        async def _forward_output(expected_streams: int) -> None:
            completed_streams = 0
            while completed_streams < expected_streams:
                chunk = await queue.get()
                if chunk is None:
                    completed_streams += 1
                    continue
                on_data(chunk)

        reader_tasks: list[asyncio.Task[Any]] = []
        for stream in (process.stdout, process.stderr):
            if stream is None:
                continue
            stream_count += 1
            reader_tasks.append(asyncio.create_task(asyncio.to_thread(_pump_stream, stream)))

        forward_task = asyncio.create_task(_forward_output(stream_count))
        wait_task = asyncio.create_task(wait_for_child_process(process))
        abort_task, cleanup_abort = _create_abort_wait_task(signal)
        timeout_task = asyncio.create_task(asyncio.sleep(timeout)) if timeout is not None and timeout > 0 else None
        timed_out = False

        try:
            pending: set[asyncio.Task[Any]] = {wait_task}
            if abort_task is not None:
                pending.add(abort_task)
            if timeout_task is not None:
                pending.add(timeout_task)
            done, _pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

            if wait_task not in done:
                if timeout_task is not None and timeout_task in done:
                    timed_out = True
                if process.pid is not None:
                    kill_process_tree(process.pid)
                exit_code = await wait_task
            else:
                exit_code = await wait_task

            if _is_aborted(signal):
                raise RuntimeError("aborted")
            if timed_out:
                raise RuntimeError(f"timeout:{timeout}")
            return {"exitCode": None if exit_code is not None and exit_code < 0 else exit_code}
        finally:
            cleanup_abort()
            if abort_task is not None and not abort_task.done():
                abort_task.cancel()
            if timeout_task is not None and not timeout_task.done():
                timeout_task.cancel()
            await asyncio.gather(*reader_tasks, return_exceptions=True)
            await forward_task
            await asyncio.sleep(0)
            await asyncio.gather(wait_task, return_exceptions=True)
            if abort_task is not None:
                await asyncio.gather(abort_task, return_exceptions=True)
            if timeout_task is not None:
                await asyncio.gather(timeout_task, return_exceptions=True)
            if process.pid is not None:
                untrack_detached_child_pid(process.pid)


def create_local_bash_operations(options: Mapping[str, Any] | None = None) -> BashOperations:
    shell_path = options.get("shellPath") if options else None
    return _LocalBashOperations(shellPath=shell_path)


def _coerce_options(options: BashToolOptions | Mapping[str, Any] | None) -> BashToolOptions:
    if options is None:
        return BashToolOptions()
    if isinstance(options, BashToolOptions):
        return options
    return BashToolOptions(
        operations=options.get("operations"),
        commandPrefix=options.get("commandPrefix"),
        shellPath=options.get("shellPath"),
        spawnHook=options.get("spawnHook"),
    )


def _resolve_spawn_context(command: str, cwd: str, spawn_hook: BashSpawnHook | None = None) -> BashSpawnContext:
    base_context = BashSpawnContext(command=command, cwd=cwd, env={**get_shell_env()})
    return spawn_hook(base_context) if spawn_hook else base_context


def _is_aborted(signal: Any | None) -> bool:
    return bool(getattr(signal, "aborted", False))


async def _poll_abort(signal: Any) -> None:
    while not _is_aborted(signal):
        await asyncio.sleep(0.01)


def _create_abort_wait_task(signal: Any | None) -> tuple[asyncio.Task[None] | None, Callable[[], None]]:
    if signal is None:
        return None, lambda: None

    wait = getattr(signal, "wait", None)
    if callable(wait):
        wait_result = wait()
        if isinstance(wait_result, Awaitable):
            return asyncio.create_task(wait_result), lambda: None

    add_listener = getattr(signal, "addEventListener", None)
    remove_listener = getattr(signal, "removeEventListener", None)
    if callable(add_listener):
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()

        def _on_abort(*_args: Any, **_kwargs: Any) -> None:
            if not future.done():
                future.set_result(None)

        add_listener("abort", _on_abort, {"once": True})

        def _cleanup() -> None:
            if callable(remove_listener):
                remove_listener("abort", _on_abort)

        return asyncio.create_task(future), _cleanup

    return asyncio.create_task(_poll_abort(signal)), lambda: None


def _make_text_result(text: str, details: BashToolDetails | None = None) -> AgentToolResult:
    return AgentToolResult(content=[TextContent(text=text)], details=details)


def _append_status(text: str, status: str) -> str:
    return f"{text}\n\n{status}" if text else status


def _now_ms() -> float:
    return time.time() * 1000


def _format_duration(ms: float) -> str:
    return f"{ms / 1000:.1f}s"


def _string_arg(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return None


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


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


def _format_bash_call(args: dict[str, Any] | None) -> str:
    command = _string_arg(_value(args, "command"))
    timeout = _value(args, "timeout")
    timeout_suffix = theme.fg("muted", f" (timeout {timeout}s)") if timeout else ""
    if command is None:
        command_display = invalid_arg_text(theme)
    elif command:
        command_display = command
    else:
        command_display = theme.fg("toolOutput", "...")
    return theme.fg("toolTitle", theme.bold(f"$ {command_display}")) + timeout_suffix


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
    details = _value(result, "details")
    truncation = _value(details, "truncation")
    full_output_path = _value(details, "fullOutputPath")
    if (
        not bool(_value(options, "isPartial"))
        and bool(_value(truncation, "truncated"))
        and full_output_path
        and output.endswith("]")
    ):
        footer_start = output.rfind("\n\n[")
        if footer_start != -1 and str(full_output_path) in output[footer_start:]:
            output = output[:footer_start].rstrip()

    if output:
        styled_output = "\n".join(theme.fg("toolOutput", line) for line in output.split("\n"))
        if bool(_value(options, "expanded")):
            component.addChild(Text(f"\n{styled_output}", 0, 0))
        else:
            component.addChild(_CollapsedBashPreview(styled_output, state))

    if bool(_value(truncation, "truncated")) or full_output_path:
        warnings: list[str] = []
        if full_output_path:
            warnings.append(f"Full output: {full_output_path}")
        if bool(_value(truncation, "truncated")):
            if _value(truncation, "truncatedBy") == "lines":
                warnings.append(
                    f"Truncated: showing {_value(truncation, 'outputLines')} of {_value(truncation, 'totalLines')} lines"
                )
            else:
                warnings.append(
                    "Truncated: "
                    f"{_value(truncation, 'outputLines')} lines shown "
                    f"({format_size(_value(truncation, 'maxBytes') or DEFAULT_MAX_BYTES)} limit)"
                )
        warning_text = f"[{'. '.join(warnings)}]"
        component.addChild(Text(f"\n{theme.fg('warning', warning_text)}", 0, 0))

    if started_at is not None:
        label = "Elapsed" if bool(_value(options, "isPartial")) else "Took"
        end_time = ended_at if ended_at is not None else _now_ms()
        component.addChild(Text(f"\n{theme.fg('muted', f'{label} {_format_duration(end_time - started_at)}')}", 0, 0))


def _render_call(args: dict[str, Any] | None, context: Any) -> Text:
    state = _get_render_state(context.state)
    if context.executionStarted and state.startedAt is None:
        state.startedAt = _now_ms()
        state.endedAt = None
    text = context.lastComponent if callable(getattr(context.lastComponent, "setText", None)) else Text("", 0, 0)
    text.setText(_format_bash_call(args))
    return text


def _render_result(
    result: AgentToolResult | Mapping[str, Any],
    options: Mapping[str, Any],
    context: Any,
) -> _BashResultRenderComponent:
    state = _get_render_state(context.state)
    if state.startedAt is not None and bool(_value(options, "isPartial")) and state.interval is None:
        state.interval = _create_render_interval(context.invalidate)
    if not bool(_value(options, "isPartial")) or context.isError:
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


def create_bash_tool_definition(
    cwd: str,
    options: BashToolOptions | Mapping[str, Any] | None = None,
) -> ToolDefinition[dict[str, Any], BashToolDetails | None]:
    resolved_options = _coerce_options(options)
    operations = resolved_options.operations or create_local_bash_operations({"shellPath": resolved_options.shellPath})

    async def execute(
        _tool_call_id: str,
        params: dict[str, Any],
        signal: Any | None = None,
        on_update: Callable[[AgentToolResult], None] | None = None,
        _ctx: Any = None,
    ) -> AgentToolResult:
        parsed = BashToolInput.model_validate(params)
        resolved_command = (
            f"{resolved_options.commandPrefix}\n{parsed.command}" if resolved_options.commandPrefix else parsed.command
        )
        spawn_context = _resolve_spawn_context(resolved_command, cwd, resolved_options.spawnHook)
        output = OutputAccumulator(OutputAccumulatorOptions(tempFilePrefix="misaka-bash"))
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
            output.append(data)
            schedule_output_update()

        async def finish_output() -> OutputSnapshot:
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

        try:
            try:
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
        name="bash",
        label="bash",
        description=(
            "Execute a bash command in the current working directory. Returns stdout and stderr. "
            f"Output is truncated to last {DEFAULT_MAX_LINES} lines or {DEFAULT_MAX_BYTES // 1024}KB "
            "(whichever is hit first). If truncated, full output is saved to a temp file. "
            "Optionally provide a timeout in seconds."
        ),
        promptSnippet="Execute bash commands (ls, grep, find, etc.)",
        parameters=BashToolInput,
        execute=execute,
        renderCall=lambda args, _theme, context: _render_call(args, context),
        renderResult=lambda result, render_options, _theme, context: _render_result(result, render_options, context),
    )


def create_bash_tool(cwd: str, options: BashToolOptions | Mapping[str, Any] | None = None) -> AgentTool:
    return wrap_tool_definition(create_bash_tool_definition(cwd, options))


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
    "createBashTool",
    "createBashToolDefinition",
    "createLocalBashOperations",
]
