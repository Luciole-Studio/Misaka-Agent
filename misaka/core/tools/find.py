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
from misaka.core.tools._common import _ignore_background_task_result, abort_race
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
from misaka.utils.tools_manager import find_tool, missing_tool_message
from misaka.utils.values import maybe_await, read_field, signal_aborted

T = TypeVar("T")


def _relativize_find_result(
    path_value: str,
    search_path: str,
    path_module: Any | None = None,
) -> str:
    """Make a result path relative to search_path (pi #7569/523b5a491).

    Absolute paths go through relpath: prefix slicing loses the first segment at the
    root ("/etc" under "/" becomes "tc") and mistakes shared-prefix siblings
    (/foo-bar vs /foo) for children. Relative paths pass through unchanged: the custom
    glob already yields relative results, and relpath would wrongly resolve them
    against cwd.
    """
    paths = os.path if path_module is None else path_module
    had_trailing_separator = path_value.endswith(paths.sep) or (
        paths.sep == "\\" and path_value.endswith("/")
    )
    is_absolute = paths.isabs(path_value) or (
        paths.sep == "\\" and path_value.startswith(("\\", "/"))
    )
    if is_absolute:
        try:
            relative_path = paths.relpath(path_value, search_path)
        except ValueError:
            # Match win32.relative() across drives/UNC roots and root-relative paths.
            def node_normalize(value: str) -> str:
                normalized = paths.normpath(value)
                if paths.sep != "\\":
                    return normalized
                drive, tail = paths.splitdrive(normalized)
                if tail or not drive.startswith("\\\\"):
                    return normalized
                drive_parts = [
                    part for part in drive[2:].split(paths.sep) if part
                ]
                if not drive_parts or drive_parts[0] in {"?", "."}:
                    return normalized
                if len(drive_parts) == 2:
                    return normalized + paths.sep
                return normalized

            relative_path = node_normalize(path_value)
            source_path = node_normalize(search_path)
            target_parts = [
                part for part in relative_path.lstrip(paths.sep).split(paths.sep) if part
            ]
            source_parts = [
                part for part in source_path.lstrip(paths.sep).split(paths.sep) if part
            ]
            common = 0
            for source_part, target_part in zip(source_parts, target_parts):
                if source_part.lower() != target_part.lower():
                    break
                common += 1
            if not target_parts:
                relative_path = paths.sep.join([".."] * len(source_parts))
            elif common:
                relative_path = paths.sep.join(
                    [".."] * (len(source_parts) - common) + target_parts[common:]
                )
        if relative_path == paths.curdir:
            # Node's path.relative(path, path) returns "", while Python returns ".".
            relative_path = ""
    else:
        relative_path = path_value

    posix_path = relative_path.replace(paths.sep, "/")
    if had_trailing_separator and not posix_path.endswith("/"):
        return f"{posix_path}/"
    return posix_path


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
    pattern = str_value(read_field(args, "pattern"))
    raw_path = str_value(read_field(args, "path"))
    path_value = shorten_path(raw_path or ".") if raw_path is not None else None
    limit = read_field(args, "limit")
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
        max_lines = len(lines) if bool(read_field(options, "expanded")) else 20
        display_lines = lines[:max_lines]
        remaining = len(lines) - max_lines
        text += "\n" + "\n".join(theme_obj.fg("toolOutput", line) for line in display_lines)
        if remaining > 0:
            more_lines_text = theme_obj.fg("muted", f"\n... ({remaining} more lines,")
            text += (
                f"{more_lines_text} {key_hint('app.tools.expand', 'to expand')})"
            )

    details = read_field(result, "details")
    result_limit = read_field(details, "resultLimitReached")
    truncation = read_field(details, "truncation")
    if result_limit or bool(read_field(truncation, "truncated")):
        warnings: list[str] = []
        if result_limit:
            warnings.append(f"{result_limit} results limit")
        if bool(read_field(truncation, "truncated")):
            warnings.append(f"{format_size(read_field(truncation, 'maxBytes') or DEFAULT_MAX_BYTES)} limit")
        warning_text = f"[Truncated: {', '.join(warnings)}]"
        text += "\n" + theme_obj.fg("warning", warning_text)
    return text


def _details_or_none(details: FindToolDetails) -> FindToolDetails | None:
    if any(getattr(details, field.name) is not None for field in fields(details)):
        return details
    return None


def _is_inside_git_repo(search_path: str) -> bool:
    current = search_path
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


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

    if signal_aborted(signal):
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
        if signal_aborted(signal):
            raise RuntimeError("Operation aborted")

        parsed = FindToolInput.model_validate(params)
        search_path = resolve_to_cwd(parsed.path or ".", cwd)
        effective_limit = parsed.limit if parsed.limit is not None else DEFAULT_LIMIT

        if custom_ops is not None and callable(getattr(custom_ops, "glob", None)):

            async def run_custom_search() -> AgentToolResult:
                if not await maybe_await(custom_ops.exists(search_path)):
                    raise RuntimeError(f"Path not found: {search_path}")
                if signal_aborted(signal):
                    raise RuntimeError("Operation aborted")

                results = await maybe_await(
                    custom_ops.glob(
                        parsed.pattern,
                        search_path,
                        {
                            "ignore": ["**/node_modules/**", "**/.git/**"],
                            "limit": effective_limit,
                        },
                    )
                )
                if signal_aborted(signal):
                    raise RuntimeError("Operation aborted")
                if not results:
                    return AgentToolResult(
                        content=[TextContent(text="No files found matching pattern")],
                        details=None,
                    )

                relativized = [
                    _relativize_find_result(path_value, search_path)
                    for path_value in results
                ]
                result_limit_reached = len(relativized) >= effective_limit
                raw_output = "\n".join(relativized)
                truncation = truncate_head(
                    raw_output, TruncationOptions(maxLines=2**31 - 1)
                )
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

            worker_task = asyncio.create_task(run_custom_search())
            try:
                async with abort_race(signal) as abort_task:
                    if abort_task is None:
                        return await worker_task

                    done, _pending = await asyncio.wait(
                        {worker_task, abort_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if abort_task in done or signal_aborted(signal):
                        if worker_task in done:
                            await asyncio.gather(worker_task, return_exceptions=True)
                        else:
                            _ignore_background_task_result(worker_task)
                        raise RuntimeError("Operation aborted")

                    result = await worker_task
                    if signal_aborted(signal):
                        raise RuntimeError("Operation aborted")
                    return result
            except asyncio.CancelledError:
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
                raise

        fd_path = find_tool("fd")
        if signal_aborted(signal):
            raise RuntimeError("Operation aborted")
        if not fd_path:
            raise RuntimeError(missing_tool_message("fd"))

        args: list[str] = ["--glob", "--color=never", "--hidden"]
        repo_probe = asyncio.create_task(
            asyncio.to_thread(_is_inside_git_repo, search_path)
        )
        try:
            async with abort_race(signal) as abort_task:
                if abort_task is not None:
                    done, _pending = await asyncio.wait(
                        {repo_probe, abort_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if abort_task in done and repo_probe not in done:
                        raise RuntimeError("Operation aborted")
                inside_git_repo = await repo_probe
        finally:
            if not repo_probe.done():
                repo_probe.cancel()
            await asyncio.gather(repo_probe, return_exceptions=True)
        if signal_aborted(signal):
            raise RuntimeError("Operation aborted")
        if not inside_git_repo:
            args.append("--no-require-git")
        args.extend(["--max-results", str(effective_limit)])

        effective_pattern = parsed.pattern
        if "/" in parsed.pattern:
            args.append("--full-path")
            if (
                not parsed.pattern.startswith("/")
                and not parsed.pattern.startswith("**/")
                and parsed.pattern != "**"
            ):
                effective_pattern = f"**/{parsed.pattern}"
            if os.name == "nt":
                effective_pattern = effective_pattern.replace("/", r"[/\\]")
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
            relativized.append(_relativize_find_result(line, search_path))

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
