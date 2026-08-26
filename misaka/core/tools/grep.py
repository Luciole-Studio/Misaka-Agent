"""Ripgrep-backed content search tool."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import TextContent
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
    GREP_MAX_LINE_LENGTH,
    TruncationOptions,
    TruncationResult,
    format_size,
    truncate_head,
    truncate_line,
)
from misaka.ui.tui import Text
from misaka.utils.tools_manager import ensure_tool

T = TypeVar("T")


class GrepToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pattern: str = Field(description="Search pattern (regex or literal string)")
    path: str | None = Field(default=None, description="Directory or file to search (default: current directory)")
    glob: str | None = Field(
        default=None,
        description="Filter files by glob pattern, e.g. '*.ts' or '**/*.spec.ts'",
    )
    ignoreCase: bool | None = Field(default=None, description="Case-insensitive search (default: false)")
    literal: bool | None = Field(
        default=None,
        description="Treat pattern as literal string instead of regex (default: false)",
    )
    context: int | None = Field(
        default=None,
        description="Number of lines to show before and after each match (default: 0)",
    )
    limit: int | None = Field(default=None, description="Maximum number of matches to return (default: 100)")


DEFAULT_LIMIT = 100


@dataclass(slots=True)
class GrepToolDetails:
    truncation: TruncationResult | None = None
    matchLimitReached: int | None = None
    linesTruncated: bool | None = None


class GrepOperations(Protocol):
    isDirectory: Callable[[str], Awaitable[bool] | bool]
    readFile: Callable[[str], Awaitable[str] | str]


@dataclass(slots=True)
class GrepToolOptions:
    operations: GrepOperations | None = None


@dataclass(slots=True)
class _DefaultGrepOperations:
    def isDirectory(self, absolute_path: str) -> bool:
        path = Path(absolute_path)
        path.stat()
        return path.is_dir()

    def readFile(self, absolute_path: str) -> str:
        return Path(absolute_path).read_text(encoding="utf-8")


def _coerce_options(options: GrepToolOptions | Mapping[str, Any] | None) -> GrepToolOptions:
    if options is None:
        return GrepToolOptions()
    if isinstance(options, GrepToolOptions):
        return options
    return GrepToolOptions(operations=options.get("operations"))


async def _maybe_await[T](value: Awaitable[T] | T) -> T:
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


def _format_grep_call(args: Mapping[str, Any] | None, theme_obj: Any) -> str:
    pattern = str_value(_value(args, "pattern"))
    raw_path = str_value(_value(args, "path"))
    path_value = shorten_path(raw_path or ".") if raw_path is not None else None
    glob_value = str_value(_value(args, "glob"))
    limit = _value(args, "limit")
    invalid_arg = invalid_arg_text(theme_obj)

    text = (
        theme_obj.fg("toolTitle", theme_obj.bold("grep"))
        + " "
        + (invalid_arg if pattern is None else theme_obj.fg("accent", f"/{pattern or ''}/"))
        + theme_obj.fg("toolOutput", f" in {invalid_arg if path_value is None else path_value}")
    )
    if glob_value:
        text += theme_obj.fg("toolOutput", f" ({glob_value})")
    if limit is not None:
        text += theme_obj.fg("toolOutput", f" limit {limit}")
    return text


def _format_grep_result(result: Any, options: Any, theme_obj: Any, show_images: bool) -> str:
    from misaka.ui.tui.interactive.components.keybinding_hints import key_hint

    output = get_text_output(result, show_images).strip()
    text = ""
    if output:
        lines = output.split("\n")
        max_lines = len(lines) if bool(_value(options, "expanded")) else 15
        display_lines = lines[:max_lines]
        remaining = len(lines) - max_lines
        text += "\n" + "\n".join(theme_obj.fg("toolOutput", line) for line in display_lines)
        if remaining > 0:
            more_lines_text = theme_obj.fg("muted", f"\n... ({remaining} more lines,")
            text += f"{more_lines_text} {key_hint('app.tools.expand', 'to expand')})"

    details = _value(result, "details")
    match_limit = _value(details, "matchLimitReached")
    truncation = _value(details, "truncation")
    lines_truncated = _value(details, "linesTruncated")
    if match_limit or bool(_value(truncation, "truncated")) or lines_truncated:
        warnings: list[str] = []
        if match_limit:
            warnings.append(f"{match_limit} matches limit")
        if bool(_value(truncation, "truncated")):
            warnings.append(f"{format_size(_value(truncation, 'maxBytes') or DEFAULT_MAX_BYTES)} limit")
        if lines_truncated:
            warnings.append("some lines truncated")
        warning_text = f"[Truncated: {', '.join(warnings)}]"
        text += "\n" + theme_obj.fg("warning", warning_text)
    return text


def _format_match_path(file_path: str, search_path: str, is_directory: bool) -> str:
    if is_directory:
        relative = os.path.relpath(file_path, search_path)
        if relative and not relative.startswith(".."):
            return relative.replace(os.sep, "/")
    return os.path.basename(file_path)


def _details_or_none(details: GrepToolDetails) -> GrepToolDetails | None:
    if any(getattr(details, field.name) is not None for field in fields(details)):
        return details
    return None


async def _read_stderr(stream: asyncio.StreamReader | None) -> str:
    if stream is None:
        return ""
    chunks: list[str] = []
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        chunks.append(chunk.decode("utf-8", errors="replace"))
    return "".join(chunks)


def create_grep_tool_definition(
    cwd: str,
    options: GrepToolOptions | Mapping[str, Any] | None = None,
) -> ToolDefinition[GrepToolInput | dict[str, Any], GrepToolDetails | None]:
    operations = _coerce_options(options).operations or _DefaultGrepOperations()

    async def execute(
        _tool_call_id: str,
        params: GrepToolInput | dict[str, Any],
        signal: Any | None = None,
        _on_update: Callable[[AgentToolResult], None] | None = None,
        _ctx: Any = None,
    ) -> AgentToolResult:
        if _signal_aborted(signal):
            raise RuntimeError("Operation aborted")

        parsed = GrepToolInput.model_validate(params)
        rg_path = await ensure_tool("rg", silent=True)
        if not rg_path:
            raise RuntimeError("ripgrep (rg) is not available and could not be downloaded")

        search_path = resolve_to_cwd(parsed.path or ".", cwd)
        try:
            is_directory = await _maybe_await(operations.isDirectory(search_path))
        except Exception:  # noqa: BLE001 - any lookup failure reads as 'path not found'
            raise RuntimeError(f"Path not found: {search_path}") from None

        context_value = parsed.context if parsed.context and parsed.context > 0 else 0
        limit_value = parsed.limit if parsed.limit is not None else DEFAULT_LIMIT
        effective_limit = max(1, limit_value)

        def format_path(file_path: str) -> str:
            return _format_match_path(file_path, search_path, bool(is_directory))

        file_cache: dict[str, list[str]] = {}

        async def get_file_lines(file_path: str) -> list[str]:
            cached = file_cache.get(file_path)
            if cached is not None:
                return cached
            try:
                content = await _maybe_await(operations.readFile(file_path))
                lines = str(content).replace("\r\n", "\n").replace("\r", "\n").split("\n")
            except Exception:  # noqa: BLE001 - an unreadable file searches as empty
                lines = []
            file_cache[file_path] = lines
            return lines

        args: list[str] = ["--json", "--line-number", "--color=never", "--hidden"]
        if parsed.ignoreCase:
            args.append("--ignore-case")
        if parsed.literal:
            args.append("--fixed-strings")
        if parsed.glob:
            args.extend(["--glob", parsed.glob])
        args.extend(["--", parsed.pattern, search_path])

        try:
            process = await asyncio.create_subprocess_exec(
                rg_path,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as error:  # noqa: BLE001 - any spawn failure is reported as 'failed to run ripgrep'
            raise RuntimeError(f"Failed to run ripgrep: {error}") from None

        stderr_task = asyncio.create_task(_read_stderr(process.stderr))
        abort_task, cleanup_abort = _create_abort_wait_task(signal)
        match_count = 0
        match_limit_reached = False
        lines_truncated = False
        aborted = False
        killed_due_to_limit = False
        matches: list[tuple[str, int, str | None]] = []

        def stop_process(due_to_limit: bool = False) -> None:
            nonlocal killed_due_to_limit
            if process.returncode is None:
                killed_due_to_limit = due_to_limit
                process.kill()

        try:
            assert process.stdout is not None
            while True:
                line_task = asyncio.create_task(process.stdout.readline())
                wait_tasks: set[asyncio.Task[Any]] = {line_task}
                if abort_task is not None:
                    wait_tasks.add(abort_task)

                done, _pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
                if abort_task is not None and abort_task in done:
                    aborted = True
                    stop_process()
                    line_task.cancel()
                    await asyncio.gather(line_task, return_exceptions=True)
                    break

                raw_line = await line_task
                if not raw_line:
                    break

                line = raw_line.decode("utf-8", errors="replace")
                if not line.strip() or match_count >= effective_limit:
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if event.get("type") != "match":
                    continue

                match_count += 1
                data = event.get("data") or {}
                path_data = data.get("path") or {}
                file_path = path_data.get("text")
                line_number = data.get("line_number")
                line_text = ((data.get("lines") or {}).get("text")) if isinstance(data.get("lines"), dict) else None
                if isinstance(file_path, str) and isinstance(line_number, int):
                    matches.append((file_path, line_number, line_text))

                if match_count >= effective_limit:
                    match_limit_reached = True
                    stop_process(True)
                    break
        finally:
            cleanup_abort()
            if abort_task is not None and not abort_task.done():
                abort_task.cancel()
            if abort_task is not None:
                await asyncio.gather(abort_task, return_exceptions=True)

        return_code = await process.wait()
        stderr_text = (await stderr_task).strip()

        if aborted or _signal_aborted(signal):
            raise RuntimeError("Operation aborted")

        if not killed_due_to_limit and return_code not in {0, 1}:
            raise RuntimeError(stderr_text or f"ripgrep exited with code {return_code}")

        if match_count == 0:
            return AgentToolResult(content=[TextContent(text="No matches found")], details=None)

        output_lines: list[str] = []
        for file_path, line_number, line_text in matches:
            if context_value == 0 and line_text is not None:
                relative_path = format_path(file_path)
                sanitized = line_text.replace("\r\n", "\n").replace("\r", "").removesuffix("\n")
                truncated = truncate_line(sanitized)
                lines_truncated = lines_truncated or bool(truncated["wasTruncated"])
                output_lines.append(f"{relative_path}:{line_number}: {truncated['text']}")
                continue

            relative_path = format_path(file_path)
            lines = await get_file_lines(file_path)
            if not lines:
                output_lines.append(f"{relative_path}:{line_number}: (unable to read file)")
                continue
            start = max(1, line_number - context_value)
            end = min(len(lines), line_number + context_value)
            for current in range(start, end + 1):
                line_text_value = lines[current - 1] if current - 1 < len(lines) else ""
                sanitized = line_text_value.replace("\r", "")
                truncated = truncate_line(sanitized)
                lines_truncated = lines_truncated or bool(truncated["wasTruncated"])
                if current == line_number:
                    output_lines.append(f"{relative_path}:{current}: {truncated['text']}")
                else:
                    output_lines.append(f"{relative_path}-{current}- {truncated['text']}")

        raw_output = "\n".join(output_lines)
        truncation = truncate_head(raw_output, TruncationOptions(maxLines=2**31 - 1))
        output = truncation.content
        details = GrepToolDetails()
        notices: list[str] = []
        if match_limit_reached:
            notices.append(
                f"{effective_limit} matches limit reached. Use limit={effective_limit * 2} for more, or refine pattern"
            )
            details.matchLimitReached = effective_limit
        if truncation.truncated:
            notices.append(f"{format_size(DEFAULT_MAX_BYTES)} limit reached")
            details.truncation = truncation
        if lines_truncated:
            notices.append(f"Some lines truncated to {GREP_MAX_LINE_LENGTH} chars. Use read tool to see full lines")
            details.linesTruncated = True
        if notices:
            output += f"\n\n[{'. '.join(notices)}]"

        return AgentToolResult(
            content=[TextContent(text=output)],
            details=_details_or_none(details),
        )

    def render_call(args: Mapping[str, Any] | None, theme_obj: Any, context: Any) -> Text:
        text = context.lastComponent if isinstance(context.lastComponent, Text) else Text("", 0, 0)
        text.setText(_format_grep_call(args, theme_obj))
        return text

    def render_result(result: Any, options_obj: Any, theme_obj: Any, context: Any) -> Text:
        text = context.lastComponent if isinstance(context.lastComponent, Text) else Text("", 0, 0)
        text.setText(_format_grep_result(result, options_obj, theme_obj, bool(context.showImages)))
        return text

    return ToolDefinition(
        name="grep",
        label="grep",
        description=(
            "Search file contents for a pattern. Returns matching lines with file paths and line numbers. "
            "Respects .gitignore. Output is truncated to 100 matches or 50KB (whichever is hit first). "
            f"Long lines are truncated to {GREP_MAX_LINE_LENGTH} chars."
        ),
        promptSnippet="Search file contents for patterns (respects .gitignore)",
        parameters=GrepToolInput,
        execute=execute,
        renderCall=render_call,
        renderResult=render_result,
    )


def create_grep_tool(cwd: str, options: GrepToolOptions | Mapping[str, Any] | None = None) -> AgentTool:
    return wrap_tool_definition(create_grep_tool_definition(cwd, options))


createGrepTool = create_grep_tool
createGrepToolDefinition = create_grep_tool_definition

__all__ = [
    "GrepOperations",
    "GrepToolDetails",
    "GrepToolInput",
    "GrepToolOptions",
    "createGrepTool",
    "createGrepToolDefinition",
]
