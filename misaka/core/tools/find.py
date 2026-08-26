"""Glob-based file discovery tool."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools._common import _is_aborted, _maybe_await, _value, abort_race
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
from misaka.utils.tools_manager import ensure_tool

T = TypeVar("T")


def _to_posix_path(value: str) -> str:
    return value.replace(os.sep, "/")


def _relativize_find_result(path_value: str, search_path: str) -> str:
    """Make a result path relative to search_path (pi #7569/523b5a491).

    Absolute paths go through relpath: prefix slicing loses the first segment at the
    root ("/etc" under "/" becomes "tc") and mistakes shared-prefix siblings
    (/foo-bar vs /foo) for children. Relative paths pass through unchanged: the custom
    glob already yields relative results, and relpath would wrongly resolve them
    against cwd.
    """
    if os.path.isabs(path_value):
        return _to_posix_path(os.path.relpath(path_value, search_path))
    return _to_posix_path(path_value)


class FindToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pattern: str = Field(
        description="Glob pattern to match files, e.g. '*.ts', '**/*.json', or 'src/**/*.spec.ts'"
    )
    path: str | None = Field(default=None, description="Directory to search in (default: current directory)")
    limit: int | None = Field(default=None, description="Maximum number of results (default: 1000)")


DEFAULT_LIMIT = 1000


@dataclass(slots=True)
class FindToolDetails:
    truncation: TruncationResult | None = None
    resultLimitReached: int | None = None


class FindOperations(Protocol):
    exists: Callable[[str], Awaitable[bool] | bool]
    glob: Callable[[str, str, dict[str, Any]], Awaitable[list[str]] | list[str]]


@dataclass(slots=True)
class FindToolOptions:
    operations: FindOperations | None = None


def _coerce_options(options: FindToolOptions | Mapping[str, Any] | None) -> FindToolOptions:
    if options is None:
        return FindToolOptions()
    if isinstance(options, FindToolOptions):
        return options
    return FindToolOptions(operations=options.get("operations"))


def _format_find_call(args: Mapping[str, Any] | None, theme_obj: Any) -> str:
    pattern = str_value(_value(args, "pattern"))
    raw_path = str_value(_value(args, "path"))
    path_value = shorten_path(raw_path or ".") if raw_path is not None else None
    limit = _value(args, "limit")
    invalid_arg = invalid_arg_text(theme_obj)

    text = (
        theme_obj.fg("toolTitle", theme_obj.bold("find"))
        + " "
        + (invalid_arg if pattern is None else theme_obj.fg("accent", pattern or ""))
        + theme_obj.fg("toolOutput", f" in {invalid_arg if path_value is None else path_value}")
    )
    if limit is not None:
        text += theme_obj.fg("toolOutput", f" (limit {limit})")
    return text


def _format_find_result(result: Any, options: Any, theme_obj: Any, show_images: bool) -> str:
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
            text += (
                f"{more_lines_text} {key_hint('app.tools.expand', 'to expand')})"
            )

    details = _value(result, "details")
    result_limit = _value(details, "resultLimitReached")
    truncation = _value(details, "truncation")
    if result_limit or bool(_value(truncation, "truncated")):
        warnings: list[str] = []
        if result_limit:
            warnings.append(f"{result_limit} results limit")
        if bool(_value(truncation, "truncated")):
            warnings.append(f"{format_size(_value(truncation, 'maxBytes') or DEFAULT_MAX_BYTES)} limit")
        warning_text = f"[Truncated: {', '.join(warnings)}]"
        text += "\n" + theme_obj.fg("warning", warning_text)
    return text


def _details_or_none(details: FindToolDetails) -> FindToolDetails | None:
    if any(getattr(details, field.name) is not None for field in fields(details)):
        return details
    return None


async def _run_fd_search(fd_path: str, args: list[str], signal: Any | None) -> tuple[bytes, bytes, int | None]:
    try:
        process = await asyncio.create_subprocess_exec(
            fd_path,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as error:  # noqa: BLE001 - any spawn failure is reported as 'failed to run fd'
        raise RuntimeError(f"Failed to run fd: {error}") from None

    communicate_task = asyncio.create_task(process.communicate())
    async with abort_race(signal) as abort_task:
        if abort_task is not None:
            done, _pending = await asyncio.wait({communicate_task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
            if abort_task in done and not communicate_task.done():
                process.kill()
                await communicate_task
                raise RuntimeError("Operation aborted")

        stdout, stderr = await communicate_task

    if _is_aborted(signal):
        raise RuntimeError("Operation aborted")

    return stdout, stderr, process.returncode


def create_find_tool_definition(
    cwd: str,
    options: FindToolOptions | Mapping[str, Any] | None = None,
) -> ToolDefinition[FindToolInput | dict[str, Any], FindToolDetails | None]:
    custom_ops = _coerce_options(options).operations

    async def execute(
        _tool_call_id: str,
        params: FindToolInput | dict[str, Any],
        signal: Any | None = None,
        _on_update: Callable[[AgentToolResult], None] | None = None,
        _ctx: Any = None,
    ) -> AgentToolResult:
        if _is_aborted(signal):
            raise RuntimeError("Operation aborted")

        parsed = FindToolInput.model_validate(params)
        search_path = resolve_to_cwd(parsed.path or ".", cwd)
        effective_limit = parsed.limit if parsed.limit is not None else DEFAULT_LIMIT

        if custom_ops is not None and callable(getattr(custom_ops, "glob", None)):
            if not await _maybe_await(custom_ops.exists(search_path)):
                raise RuntimeError(f"Path not found: {search_path}")
            if _is_aborted(signal):
                raise RuntimeError("Operation aborted")

            results = await _maybe_await(
                custom_ops.glob(
                    parsed.pattern,
                    search_path,
                    {"ignore": ["**/node_modules/**", "**/.git/**"], "limit": effective_limit},
                )
            )
            if _is_aborted(signal):
                raise RuntimeError("Operation aborted")
            if not results:
                return AgentToolResult(content=[TextContent(text="No files found matching pattern")], details=None)

            relativized = [_relativize_find_result(path_value, search_path)
                           for path_value in results]
            result_limit_reached = len(relativized) >= effective_limit
            raw_output = "\n".join(relativized)
            truncation = truncate_head(raw_output, TruncationOptions(maxLines=2**31 - 1))
            result_output = truncation.content
            details = FindToolDetails()
            notices: list[str] = []
            if result_limit_reached:
                notices.append(f"{effective_limit} results limit reached")
                details.resultLimitReached = effective_limit
            if truncation.truncated:
                notices.append(f"{format_size(DEFAULT_MAX_BYTES)} limit reached")
                details.truncation = truncation
            if notices:
                result_output += f"\n\n[{'. '.join(notices)}]"
            return AgentToolResult(
                content=[TextContent(text=result_output)],
                details=_details_or_none(details),
            )

        fd_path = await ensure_tool("fd", silent=True)
        if _is_aborted(signal):
            raise RuntimeError("Operation aborted")
        if not fd_path:
            raise RuntimeError("fd is not available and could not be downloaded")

        args: list[str] = [
            "--glob",
            "--color=never",
            "--hidden",
            "--no-require-git",
            "--max-results",
            str(effective_limit),
        ]

        effective_pattern = parsed.pattern
        if "/" in parsed.pattern:
            args.append("--full-path")
            if (
                not parsed.pattern.startswith("/")
                and not parsed.pattern.startswith("**/")
                and parsed.pattern != "**"
            ):
                effective_pattern = f"**/{parsed.pattern}"
        args.extend(["--", effective_pattern, search_path])

        stdout, stderr, return_code = await _run_fd_search(fd_path, args, signal)
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        output = "\n".join(lines)

        if return_code != 0 and not output:
            raise RuntimeError(stderr_text or f"fd exited with code {return_code}")
        if not output:
            return AgentToolResult(content=[TextContent(text="No files found matching pattern")], details=None)

        relativized: list[str] = []
        for raw_line in lines:
            line = raw_line.rstrip("\r").strip()
            if not line:
                continue
            had_trailing_slash = line.endswith(("/", "\\"))
            posix_value = _relativize_find_result(line, search_path)
            if had_trailing_slash and not posix_value.endswith("/"):
                posix_value += "/"
            relativized.append(posix_value)

        result_limit_reached = len(relativized) >= effective_limit
        raw_output = "\n".join(relativized)
        truncation = truncate_head(raw_output, TruncationOptions(maxLines=2**31 - 1))
        result_output = truncation.content
        details = FindToolDetails()
        notices: list[str] = []
        if result_limit_reached:
            notices.append(
                f"{effective_limit} results limit reached. Use limit={effective_limit * 2} for more, or refine pattern"
            )
            details.resultLimitReached = effective_limit
        if truncation.truncated:
            notices.append(f"{format_size(DEFAULT_MAX_BYTES)} limit reached")
            details.truncation = truncation
        if notices:
            result_output += f"\n\n[{'. '.join(notices)}]"

        return AgentToolResult(
            content=[TextContent(text=result_output)],
            details=_details_or_none(details),
        )

    def render_call(args: Mapping[str, Any] | None, theme_obj: Any, context: Any) -> Text:
        text = context.lastComponent if isinstance(context.lastComponent, Text) else Text("", 0, 0)
        text.setText(_format_find_call(args, theme_obj))
        return text

    def render_result(result: Any, options_obj: Any, theme_obj: Any, context: Any) -> Text:
        text = context.lastComponent if isinstance(context.lastComponent, Text) else Text("", 0, 0)
        text.setText(_format_find_result(result, options_obj, theme_obj, bool(context.showImages)))
        return text

    return ToolDefinition(
        name="find",
        label="find",
        description=(
            "Search for files by glob pattern. Returns matching file paths relative to the search directory. "
            "Respects .gitignore. Output is truncated to 1000 results or 50KB (whichever is hit first)."
        ),
        promptSnippet="Find files by glob pattern (respects .gitignore)",
        parameters=FindToolInput,
        execute=execute,
        renderCall=render_call,
        renderResult=render_result,
    )


def create_find_tool(cwd: str, options: FindToolOptions | Mapping[str, Any] | None = None) -> AgentTool:
    return wrap_tool_definition(create_find_tool_definition(cwd, options))


createFindTool = create_find_tool
createFindToolDefinition = create_find_tool_definition

__all__ = [
    "FindOperations",
    "FindToolDetails",
    "FindToolInput",
    "FindToolOptions",
    "createFindTool",
    "createFindToolDefinition",
]
