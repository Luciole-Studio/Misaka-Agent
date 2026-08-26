"""Directory listing tool."""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields
from typing import Any, Protocol, TypeVar

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import TextContent
from pydantic import BaseModel, ConfigDict, Field

from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools.path_utils import resolve_to_cwd
from misaka.core.tools.render_utils import (
    get_text_output,
    invalid_arg_text,
    shorten_path,
    str_value,
)
from misaka.core.tools.tool_definition_wrapper import wrap_tool_definition
from misaka.core.tools.truncate import (
    DEFAULT_MAX_BYTES,
    TruncationOptions,
    TruncationResult,
    format_size,
    truncate_head,
)
from misaka.ui.tui import Text

T = TypeVar("T")


DEFAULT_LIMIT = 500


class LsToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str | None = Field(default=None, description="Directory to list (default: current directory)")
    limit: int | None = Field(default=None, description="Maximum number of entries to return (default: 500)")


@dataclass(slots=True)
class LsToolDetails:
    truncation: TruncationResult | None = None
    entryLimitReached: int | None = None


class LsOperations(Protocol):
    exists: Callable[[str], Awaitable[bool] | bool]
    stat: Callable[[str], Awaitable[os.stat_result | Any] | os.stat_result | Any]
    readdir: Callable[[str], Awaitable[list[str]] | list[str]]


@dataclass(slots=True)
class LsToolOptions:
    operations: LsOperations | None = None


@dataclass(slots=True)
class _DefaultLsOperations:
    def exists(self, absolute_path: str) -> bool:
        return os.path.exists(absolute_path)

    def stat(self, absolute_path: str) -> os.stat_result:
        return os.stat(absolute_path)

    def readdir(self, absolute_path: str) -> list[str]:
        return os.listdir(absolute_path)


def _coerce_options(options: LsToolOptions | Mapping[str, Any] | None) -> LsToolOptions:
    if options is None:
        return LsToolOptions()
    if isinstance(options, LsToolOptions):
        return options
    return LsToolOptions(operations=options.get("operations"))


async def _maybe_await(value: Awaitable[T] | T) -> T:
    if asyncio.isfuture(value) or hasattr(value, "__await__"):
        return await value
    return value


def _signal_aborted(signal: Any | None) -> bool:
    return bool(getattr(signal, "aborted", False))


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


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

        return asyncio.ensure_future(future), _cleanup

    async def _poll_abort() -> None:
        while not _signal_aborted(signal):
            await asyncio.sleep(0.01)

    return asyncio.create_task(_poll_abort()), lambda: None


def _ignore_background_task_result(task: asyncio.Task[Any]) -> None:
    def _consume(done: asyncio.Task[Any]) -> None:
        try:
            done.result()
        except Exception:
            return

    task.add_done_callback(_consume)


def _format_ls_call(args: Mapping[str, Any] | None, theme_obj: Any) -> str:
    raw_path = str_value(_value(args, "path"))
    path_value = shorten_path(raw_path or ".") if raw_path is not None else None
    limit = _value(args, "limit")
    invalid_arg = invalid_arg_text(theme_obj)
    text = f"{theme_obj.fg('toolTitle', theme_obj.bold('ls'))} {invalid_arg if path_value is None else theme_obj.fg('accent', path_value)}"
    if limit is not None:
        text += theme_obj.fg("toolOutput", f" (limit {limit})")
    return text


def _format_ls_result(result: Any, options: Any, theme_obj: Any, show_images: bool) -> str:
    from misaka.ui.tui.interactive.components.keybinding_hints import key_hint

    output = get_text_output(result, show_images).strip()
    text = ""
    if output:
        lines = output.split("\n")
        max_lines = len(lines) if bool(_value(options, "expanded")) else 20
        display_lines = lines[:max_lines]
        remaining = len(lines) - max_lines
        text += "\n" + "\n".join(theme_obj.fg("toolOutput", line) for line in display_lines)
        if remaining > 0:
            more_lines_text = theme_obj.fg("muted", f"\n... ({remaining} more lines,")
            text += f"{more_lines_text} {key_hint('app.tools.expand', 'to expand')})"

    details = _value(result, "details")
    entry_limit = _value(details, "entryLimitReached")
    truncation = _value(details, "truncation")
    if entry_limit or bool(_value(truncation, "truncated")):
        warnings: list[str] = []
        if entry_limit:
            warnings.append(f"{entry_limit} entries limit")
        if bool(_value(truncation, "truncated")):
            warnings.append(f"{format_size(_value(truncation, 'maxBytes') or DEFAULT_MAX_BYTES)} limit")
        warning_text = f"[Truncated: {', '.join(warnings)}]"
        text += "\n" + theme_obj.fg("warning", warning_text)
    return text


def _is_directory(stat_result: Any) -> bool:
    if hasattr(stat_result, "is_dir"):
        return bool(stat_result.is_dir())
    if hasattr(stat_result, "isDirectory"):
        return bool(stat_result.isDirectory())
    mode = getattr(stat_result, "st_mode", None)
    if isinstance(mode, int):
        return stat.S_ISDIR(mode)
    raise TypeError("Unsupported stat result")


def _details_or_none(details: LsToolDetails) -> LsToolDetails | None:
    if any(getattr(details, field.name) is not None for field in fields(details)):
        return details
    return None


def create_ls_tool_definition(
    cwd: str,
    options: LsToolOptions | Mapping[str, Any] | None = None,
) -> ToolDefinition[LsToolInput | dict[str, Any], LsToolDetails | None]:
    operations = _coerce_options(options).operations or _DefaultLsOperations()

    async def execute(
        _tool_call_id: str,
        params: LsToolInput | dict[str, Any],
        signal: Any | None = None,
        _on_update: Callable[[AgentToolResult], None] | None = None,
        _ctx: Any = None,
    ) -> AgentToolResult:
        if _signal_aborted(signal):
            raise RuntimeError("Operation aborted")

        parsed = LsToolInput.model_validate(params)
        dir_path = resolve_to_cwd(parsed.path or ".", cwd)
        effective_limit = parsed.limit if parsed.limit is not None else DEFAULT_LIMIT

        async def worker() -> AgentToolResult:
            if not await _maybe_await(operations.exists(dir_path)):
                raise RuntimeError(f"Path not found: {dir_path}")

            stat_result = await _maybe_await(operations.stat(dir_path))
            if not _is_directory(stat_result):
                raise RuntimeError(f"Not a directory: {dir_path}")

            try:
                entries = await _maybe_await(operations.readdir(dir_path))
            except Exception as error:
                raise RuntimeError(f"Cannot read directory: {error}") from None

            entries.sort(key=str.lower)
            results: list[str] = []
            entry_limit_reached = False
            for entry in entries:
                if len(results) >= effective_limit:
                    entry_limit_reached = True
                    break
                full_path = os.path.join(dir_path, entry)
                try:
                    entry_stat = await _maybe_await(operations.stat(full_path))
                except Exception:
                    continue
                results.append(f"{entry}/" if _is_directory(entry_stat) else entry)

            if not results:
                return AgentToolResult(content=[TextContent(text="(empty directory)")], details=None)

            raw_output = "\n".join(results)
            truncation = truncate_head(raw_output, TruncationOptions(maxLines=2**31 - 1))
            output = truncation.content
            details = LsToolDetails()
            notices: list[str] = []
            if entry_limit_reached:
                notices.append(f"{effective_limit} entries limit reached. Use limit={effective_limit * 2} for more")
                details.entryLimitReached = effective_limit
            if truncation.truncated:
                notices.append(f"{format_size(DEFAULT_MAX_BYTES)} limit reached")
                details.truncation = truncation
            if notices:
                output += f"\n\n[{'. '.join(notices)}]"

            return AgentToolResult(
                content=[TextContent(text=output)],
                details=_details_or_none(details),
            )

        worker_task = asyncio.create_task(worker())
        abort_task, cleanup_abort = _create_abort_wait_task(signal)
        try:
            if abort_task is None:
                return await worker_task

            done, _pending = await asyncio.wait({worker_task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
            if abort_task in done and worker_task not in done:
                _ignore_background_task_result(worker_task)
                raise RuntimeError("Operation aborted")
            return await worker_task
        finally:
            cleanup_abort()
            if abort_task is not None and not abort_task.done():
                abort_task.cancel()
            if abort_task is not None:
                await asyncio.gather(abort_task, return_exceptions=True)

    def render_call(args: Mapping[str, Any] | None, theme_obj: Any, context: Any) -> Text:
        text = context.lastComponent if isinstance(context.lastComponent, Text) else Text("", 0, 0)
        text.setText(_format_ls_call(args, theme_obj))
        return text

    def render_result(result: Any, options_obj: Any, theme_obj: Any, context: Any) -> Text:
        text = context.lastComponent if isinstance(context.lastComponent, Text) else Text("", 0, 0)
        text.setText(_format_ls_result(result, options_obj, theme_obj, bool(context.showImages)))
        return text

    return ToolDefinition(
        name="ls",
        label="ls",
        description=(
            "List directory contents. Returns entries sorted alphabetically, with '/' suffix for directories. "
            "Includes dotfiles. Output is truncated to 500 entries or 50KB (whichever is hit first)."
        ),
        promptSnippet="List directory contents",
        parameters=LsToolInput,
        execute=execute,
        renderCall=render_call,
        renderResult=render_result,
    )


def create_ls_tool(cwd: str, options: LsToolOptions | Mapping[str, Any] | None = None) -> AgentTool:
    return wrap_tool_definition(create_ls_tool_definition(cwd, options))


createLsTool = create_ls_tool
createLsToolDefinition = create_ls_tool_definition

__all__ = [
    "LsOperations",
    "LsToolDetails",
    "LsToolInput",
    "LsToolOptions",
    "createLsTool",
    "createLsToolDefinition",
]
