"""Translated from pi's ``packages/coding-agent/src/core/tools/find.ts`` (v1.1.0).

MISAKA forks:
- MISAKA's credentials and the web evidence vault are out of the tools' reach: the search path is checked, and a
  result inside a credential store or a skills hub cache is passed over (Hermes ``_filter_read_blocked_search_results``).
- fd is never downloaded (``tools_manager``); a missing one says how to install it.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields
from types import SimpleNamespace
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools._common import _ignore_background_task_result, abort_race
from misaka.core.tools.path_utils import pathExists, resolveToCwd
from misaka.core.tools.renderers.find import findRenderers
from misaka.core.tools.tool_definition_wrapper import wrapToolDefinition
from misaka.core.tools.truncate import (
    DEFAULT_MAX_BYTES,
    TruncationOptions,
    TruncationResult,
    formatSize,
    truncateHead,
)
from misaka.utils import node_path
from misaka.utils.js import formatNumber, jsTrim
from misaka.utils.tools_manager import ensureTool, missingToolMessage
from misaka.utils.values import maybe_await, read_field, signal_aborted


def relativizeFindResultPath(
    resultPath: str,
    searchPath: str,
    pathModule: Any = node_path,
) -> str:
    """Relativize a find result against the search root and normalize it to posix separators."""
    hadTrailingSeparator = resultPath.endswith(pathModule.sep) or (pathModule.sep == "\\" and resultPath.endswith("/"))
    relativePath = pathModule.relative(searchPath, resultPath) if pathModule.isAbsolute(resultPath) else resultPath
    posixPath = "/".join(relativePath.split(pathModule.sep))
    return f"{posixPath}/" if hadTrailingSeparator and not posixPath.endswith("/") else posixPath


# `Type.Number`: the schema says number; the value is read as an integer.
_NUMBER = {"type": "number"}


findToolSystemPromptContribution = {
    "snippet": "Find files by glob pattern (respects .gitignore)",
    "guidelines": [],
}


class FindToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pattern: str = Field(description="Glob pattern to match files, e.g. '*.ts', '**/*.json', or 'src/**/*.spec.ts'")
    path: str | None = Field(default=None, description="Directory to search in (default: current directory)")
    limit: int | None = Field(default=None, description="Maximum number of results (default: 1000)", json_schema_extra=_NUMBER)

DEFAULT_LIMIT = 1000


@dataclass(slots=True)
class FindToolDetails:
    truncation: TruncationResult | None = None
    resultLimitReached: int | None = None


class FindOperations(Protocol):
    """Pluggable operations for the find tool.
    Override these to delegate file search to remote systems (for example SSH).
    """

    # Check if path exists
    exists: Callable[[str], Awaitable[bool] | bool]
    # Find files matching glob pattern. Returns relative or absolute paths.
    glob: Callable[[str, str, dict[str, Any]], Awaitable[list[str]] | list[str]]


defaultFindOperations: FindOperations = SimpleNamespace(  # type: ignore[assignment]
    exists=pathExists,
    # This is a placeholder. Actual fd execution happens in execute() when no custom glob is provided.
    glob=lambda *_args: [],
)


@dataclass(slots=True)
class FindToolOptions:
    # Custom operations for find. Default: local filesystem plus fd
    operations: FindOperations | None = None


def _coerce_options(options: FindToolOptions | Mapping[str, Any] | None) -> FindToolOptions | None:
    """An option bag passed as a mapping, as JavaScript callers pass an object literal."""
    if options is None or isinstance(options, FindToolOptions):
        return options
    return FindToolOptions(operations=options.get("operations"))


def _details(details: FindToolDetails) -> FindToolDetails | None:
    """``Object.keys(details).length > 0 ? details : undefined``."""
    return details if any(getattr(details, field.name) is not None for field in fields(details)) else None


def _out_of_reach(path: str, searchPath: str) -> bool:
    # MISAKA fork: a search over the home passes its credential stores and skills hub caches by.
    from misaka.core.web.evidence import is_credential_store, is_skills_hub_cache
    full = os.path.join(searchPath, path)
    return is_credential_store(full) or is_skills_hub_cache(full)


def _readlineLines(data: bytes) -> list[str]:
    """The lines ``readline`` emits for ``data``: split at ``\\n``, ``\\r\\n`` or ``\\r``, no empty last line."""
    lines = re.split(r"\r\n|\r|\n", data.decode("utf-8", errors="replace"))
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def createFindToolDefinition(
    cwd: str,
    options: FindToolOptions | Mapping[str, Any] | None = None,
) -> ToolDefinition[FindToolInput | dict[str, Any], FindToolDetails | None]:
    options = _coerce_options(options)
    customOps = options.operations if options else None

    async def execute(
        _toolCallId: str,
        params: FindToolInput | dict[str, Any],
        signal: Any | None = None,
        _onUpdate: Callable[[AgentToolResult], None] | None = None,
        ctx: Any = None,
    ) -> AgentToolResult:
        if signal_aborted(signal):
            raise RuntimeError("Operation aborted")
        parsed = FindToolInput.model_validate(params)
        pattern, searchDir, limit = parsed.pattern, parsed.path, parsed.limit

        searchPath = resolveToCwd(searchDir or ".", read_field(ctx, "cwd") or cwd)
        # MISAKA fork: MISAKA's credentials and the web evidence vault are out of the tools' reach.
        from misaka.core.web.evidence import check_material_read
        check_material_read(searchPath)
        effectiveLimit = limit if limit is not None else DEFAULT_LIMIT
        ops = customOps or defaultFindOperations

        async def run() -> AgentToolResult:
            # If custom operations provide glob(), use that instead of fd.
            if customOps is not None and callable(getattr(customOps, "glob", None)):
                if not await maybe_await(ops.exists(searchPath)):
                    raise RuntimeError(f"Path not found: {searchPath}")
                if signal_aborted(signal):
                    raise RuntimeError("Operation aborted")
                results = await maybe_await(
                    ops.glob(pattern, searchPath, {"ignore": ["**/node_modules/**", "**/.git/**"], "limit": effectiveLimit})
                )
                if signal_aborted(signal):
                    raise RuntimeError("Operation aborted")
                found = len(results)
                results = [p for p in results if not _out_of_reach(p, searchPath)]
                if len(results) == 0:
                    return AgentToolResult(content=[TextContent(text="No files found matching pattern")], details=None)

                # Relativize paths against the search root for stable output.
                relativized = [relativizeFindResultPath(p, searchPath) for p in results]
                resultLimitReached = found >= effectiveLimit
                rawOutput = "\n".join(relativized)
                truncation = truncateHead(rawOutput, TruncationOptions(maxLines=2**53 - 1))
                resultOutput = truncation.content
                details = FindToolDetails()
                notices: list[str] = []
                if resultLimitReached:
                    notices.append(f"{effectiveLimit} results limit reached")
                    details.resultLimitReached = effectiveLimit
                if truncation.truncated:
                    notices.append(f"{formatSize(DEFAULT_MAX_BYTES)} limit reached")
                    details.truncation = truncation
                if len(notices) > 0:
                    resultOutput += f"\n\n[{'. '.join(notices)}]"
                return AgentToolResult(content=[TextContent(text=resultOutput)], details=_details(details))

            # Default implementation uses fd.
            fdPath = await ensureTool("fd")
            if signal_aborted(signal):
                raise RuntimeError("Operation aborted")
            if not fdPath:
                # MISAKA fork: never downloaded, so the error says how to install it.
                raise RuntimeError(missingToolMessage("fd"))

            args: list[str] = ["--glob", "--color=never", "--hidden"]

            # fd normally ignores .gitignore outside git repos, so keep --no-require-git
            # there. Inside repos, use fd's default git-aware behavior so parent
            # .gitignore rules stop at nested repo boundaries:
            # https://github.com/earendil-works/pi/issues/5960
            insideGitRepo = False
            current = searchPath
            while True:
                if await pathExists(os.path.join(current, ".git")):
                    insideGitRepo = True
                    break
                parent = os.path.dirname(current)
                if parent == current:
                    break
                current = parent
            if not insideGitRepo:
                args.append("--no-require-git")
            args.extend(["--max-results", str(effectiveLimit)])

            # fd --glob matches against the basename unless --full-path is set; in --full-path
            # mode it matches against the absolute candidate path, so a path-containing
            # pattern like 'src/**/*.spec.ts' needs a leading '**/' to match anything.
            effectivePattern = pattern
            if "/" in pattern:
                args.append("--full-path")
                if not pattern.startswith("/") and not pattern.startswith("**/") and pattern != "**":
                    effectivePattern = f"**/{pattern}"
                # fd matches full paths using native separators on Windows.
                if sys.platform == "win32":
                    effectivePattern = effectivePattern.replace("/", r"[/\\]")
            args.extend(["--", effectivePattern, searchPath])

            try:
                child = await asyncio.create_subprocess_exec(
                    fdPath,
                    *args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except Exception as error:  # noqa: BLE001 - any spawn failure is reported as 'failed to run fd'
                raise RuntimeError(f"Failed to run fd: {error}") from None
            try:
                stdout, stderrBytes = await child.communicate()
            except asyncio.CancelledError:
                if child.returncode is None:
                    child.kill()
                await child.wait()
                raise
            code = child.returncode
            if signal_aborted(signal):
                raise RuntimeError("Operation aborted")
            lines = _readlineLines(stdout)
            stderr = stderrBytes.decode("utf-8", errors="replace")
            output = "\n".join(lines)
            if code != 0:
                errorMsg = jsTrim(stderr) or f"fd exited with code {code}"
                if not output:
                    raise RuntimeError(errorMsg)
            if not output:
                return AgentToolResult(content=[TextContent(text="No files found matching pattern")], details=None)

            relativized = []
            found = 0
            for rawLine in lines:
                line = jsTrim(rawLine.removesuffix("\r"))
                if not line:
                    continue
                found += 1
                if _out_of_reach(line, searchPath):
                    continue
                relativized.append(relativizeFindResultPath(line, searchPath))
            if not relativized:
                return AgentToolResult(content=[TextContent(text="No files found matching pattern")], details=None)

            resultLimitReached = found >= effectiveLimit
            rawOutput = "\n".join(relativized)
            truncation = truncateHead(rawOutput, TruncationOptions(maxLines=2**53 - 1))
            resultOutput = truncation.content
            details = FindToolDetails()
            notices = []
            if resultLimitReached:
                notices.append(
                    f"{effectiveLimit} results limit reached. Use limit={effectiveLimit * 2} for more, or refine pattern"
                )
                details.resultLimitReached = effectiveLimit
            if truncation.truncated:
                notices.append(f"{formatSize(DEFAULT_MAX_BYTES)} limit reached")
                details.truncation = truncation
            if len(notices) > 0:
                resultOutput += f"\n\n[{'. '.join(notices)}]"
            return AgentToolResult(content=[TextContent(text=resultOutput)], details=_details(details))

        task = asyncio.create_task(run())
        try:
            async with abort_race(signal) as abortTask:
                if abortTask is None:
                    return await task
                done, _pending = await asyncio.wait({task, abortTask}, return_when=asyncio.FIRST_COMPLETED)
                if abortTask in done and task not in done:
                    # Stop the child (stopChild): cancelling the run kills fd.
                    task.cancel()
                    _ignore_background_task_result(task)
                    raise RuntimeError("Operation aborted")
                try:
                    return await task
                except Exception:
                    if signal_aborted(signal):
                        raise RuntimeError("Operation aborted") from None
                    raise
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    return ToolDefinition(
        name="find",
        label="find",
        description=f"Search for files by glob pattern. Returns matching file paths relative to the search directory. Respects .gitignore. Output is truncated to {DEFAULT_LIMIT} results or {formatNumber(DEFAULT_MAX_BYTES / 1024)}KB (whichever is hit first).",
        promptSnippet=findToolSystemPromptContribution["snippet"],
        parameters=FindToolInput,
        execute=execute,
        **findRenderers,
    )


def createFindTool(cwd: str, options: FindToolOptions | Mapping[str, Any] | None = None) -> AgentTool:
    return wrapToolDefinition(createFindToolDefinition(cwd, options))


__all__ = [
    "FindOperations",
    "FindToolDetails",
    "FindToolInput",
    "FindToolOptions",
    "createFindTool",
    "createFindToolDefinition",
    "findToolSystemPromptContribution",
    "relativizeFindResultPath",
]
