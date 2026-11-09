"""Translated from pi's ``packages/coding-agent/src/core/tools/ls.ts`` (v1.1.0)."""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields
from functools import cmp_to_key
from types import SimpleNamespace
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools._common import _ignore_background_task_result, abort_race
from misaka.core.tools.path_utils import pathExists, resolveToCwd
from misaka.core.tools.renderers.ls import lsRenderers
from misaka.core.tools.tool_definition_wrapper import wrapToolDefinition
from misaka.core.tools.truncate import (
    DEFAULT_MAX_BYTES,
    TruncationOptions,
    TruncationResult,
    formatSize,
    truncateHead,
)
from misaka.utils.collation import localeCompare
from misaka.utils.js import formatNumber
from misaka.utils.node_errors import withSyscall
from misaka.utils.values import maybe_await, read_field, signal_aborted

# `Type.Number`: the schema says number; the value is read as an integer.
_NUMBER = {"type": "number"}


lsToolSystemPromptContribution = {
    "snippet": "List directory contents",
    "guidelines": [],
}


class LsToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str | None = Field(default=None, description="Directory to list (default: current directory)")
    limit: int | None = Field(default=None, description="Maximum number of entries to return (default: 500)", json_schema_extra=_NUMBER)

DEFAULT_LIMIT = 500


@dataclass(slots=True)
class LsToolDetails:
    truncation: TruncationResult | None = None
    entryLimitReached: int | None = None


class LsOperations(Protocol):
    """Pluggable operations for the ls tool.
    Override these to delegate directory listing to remote systems (for example SSH).
    """

    # Check if path exists
    exists: Callable[[str], Awaitable[bool] | bool]
    # Get file or directory stats. Throws if not found.
    stat: Callable[[str], Awaitable[os.stat_result | Any] | os.stat_result | Any]
    # Read directory entries
    readdir: Callable[[str], Awaitable[list[str]] | list[str]]


defaultLsOperations: LsOperations = SimpleNamespace(  # type: ignore[assignment]
    exists=pathExists,
    stat=lambda path: asyncio.to_thread(withSyscall, "stat", os.stat, path, path=path),
    readdir=lambda path: asyncio.to_thread(withSyscall, "scandir", os.listdir, path, path=path),
)


@dataclass(slots=True)
class LsToolOptions:
    # Custom operations for directory listing. Default: local filesystem
    operations: LsOperations | None = None


def _coerce_options(options: LsToolOptions | Mapping[str, Any] | None) -> LsToolOptions | None:
    """An option bag passed as a mapping, as JavaScript callers pass an object literal."""
    if options is None or isinstance(options, LsToolOptions):
        return options
    return LsToolOptions(operations=options.get("operations"))


def _isDirectory(stats: Any) -> bool:
    """``stats.isDirectory()``, for an ``os.stat_result`` or an object shaped like Node's ``Stats``."""
    if hasattr(stats, "is_dir"):
        return bool(stats.is_dir())
    if hasattr(stats, "isDirectory"):
        return bool(stats.isDirectory())
    mode = getattr(stats, "st_mode", None)
    if isinstance(mode, int):
        return stat.S_ISDIR(mode)
    raise TypeError("Unsupported stat result")


def createLsToolDefinition(
    cwd: str,
    options: LsToolOptions | Mapping[str, Any] | None = None,
) -> ToolDefinition[LsToolInput | dict[str, Any], LsToolDetails | None]:
    options = _coerce_options(options)
    ops = (options.operations if options else None) or defaultLsOperations

    async def execute(
        _toolCallId: str,
        params: LsToolInput | dict[str, Any],
        signal: Any | None = None,
        _onUpdate: Callable[[AgentToolResult], None] | None = None,
        ctx: Any = None,
    ) -> AgentToolResult:
        if signal_aborted(signal):
            raise RuntimeError("Operation aborted")
        parsed = LsToolInput.model_validate(params)
        path, limit = parsed.path, parsed.limit

        async def run() -> AgentToolResult:
            dirPath = resolveToCwd(path or ".", read_field(ctx, "cwd") or cwd)
            effectiveLimit = limit if limit is not None else DEFAULT_LIMIT

            # Check if path exists.
            if not await maybe_await(ops.exists(dirPath)):
                raise RuntimeError(f"Path not found: {dirPath}")

            # Check if path is a directory.
            stats = await maybe_await(ops.stat(dirPath))
            if not _isDirectory(stats):
                raise RuntimeError(f"Not a directory: {dirPath}")

            # Read directory entries.
            try:
                entries = await maybe_await(ops.readdir(dirPath))
            except Exception as e:  # noqa: BLE001 - any readdir failure becomes the user-facing error
                raise RuntimeError(f"Cannot read directory: {getattr(e, 'uvMessage', None) or e}") from None

            # Sort alphabetically, case-insensitive.
            entries.sort(key=cmp_to_key(lambda a, b: localeCompare(a.lower(), b.lower())))

            # Format entries with directory indicators.
            results: list[str] = []
            entryLimitReached = False
            for entry in entries:
                if len(results) >= effectiveLimit:
                    entryLimitReached = True
                    break

                fullPath = os.path.join(dirPath, entry)
                suffix = ""
                try:
                    entryStat = await maybe_await(ops.stat(fullPath))
                    if _isDirectory(entryStat):
                        suffix = "/"
                except Exception:  # noqa: BLE001, S112
                    # Skip entries we cannot stat.
                    continue
                results.append(entry + suffix)

            if len(results) == 0:
                return AgentToolResult(content=[TextContent(text="(empty directory)")], details=None)

            rawOutput = "\n".join(results)
            # Apply byte truncation. There is no separate line limit because entry count is already capped.
            truncation = truncateHead(rawOutput, TruncationOptions(maxLines=2**53 - 1))
            output = truncation.content
            details = LsToolDetails()
            # Build actionable notices for truncation and entry limits.
            notices: list[str] = []
            if entryLimitReached:
                notices.append(f"{effectiveLimit} entries limit reached. Use limit={effectiveLimit * 2} for more")
                details.entryLimitReached = effectiveLimit
            if truncation.truncated:
                notices.append(f"{formatSize(DEFAULT_MAX_BYTES)} limit reached")
                details.truncation = truncation
            if len(notices) > 0:
                output += f"\n\n[{'. '.join(notices)}]"

            return AgentToolResult(
                content=[TextContent(text=output)],
                details=details if any(getattr(details, field.name) is not None for field in fields(details)) else None,
            )

        task = asyncio.create_task(run())
        async with abort_race(signal) as abortTask:
            if abortTask is None:
                return await task
            done, _pending = await asyncio.wait({task, abortTask}, return_when=asyncio.FIRST_COMPLETED)
            if abortTask in done and task not in done:
                _ignore_background_task_result(task)
                raise RuntimeError("Operation aborted")
            return await task

    return ToolDefinition(
        name="ls",
        label="ls",
        description=f"List directory contents. Returns entries sorted alphabetically, with '/' suffix for directories. Includes dotfiles. Output is truncated to {DEFAULT_LIMIT} entries or {formatNumber(DEFAULT_MAX_BYTES / 1024)}KB (whichever is hit first).",
        promptSnippet=lsToolSystemPromptContribution["snippet"],
        parameters=LsToolInput,
        execute=execute,
        **lsRenderers,
    )


def createLsTool(cwd: str, options: LsToolOptions | Mapping[str, Any] | None = None) -> AgentTool:
    return wrapToolDefinition(createLsToolDefinition(cwd, options))


__all__ = [
    "LsOperations",
    "LsToolDetails",
    "LsToolInput",
    "LsToolOptions",
    "createLsTool",
    "createLsToolDefinition",
    "lsToolSystemPromptContribution",
]
