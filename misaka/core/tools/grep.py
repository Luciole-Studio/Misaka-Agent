"""Translated from pi's ``packages/coding-agent/src/core/tools/grep.ts`` (v1.1.0).

MISAKA forks:
- MISAKA's credentials and the web evidence vault are out of the tools' reach: the search path is checked, and a match
  inside a credential store is passed over.
- ripgrep is never downloaded (``tools_manager``); a missing one says how to install it.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields
from types import SimpleNamespace
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools._common import abort_race
from misaka.core.tools.path_utils import resolveToCwd
from misaka.core.tools.renderers.grep import grepRenderers
from misaka.core.tools.tool_definition_wrapper import wrapToolDefinition
from misaka.core.tools.truncate import (
    DEFAULT_MAX_BYTES,
    GREP_MAX_LINE_LENGTH,
    TruncationOptions,
    TruncationResult,
    formatSize,
    truncateHead,
    truncateLine,
)
from misaka.utils import node_path
from misaka.utils.js import formatNumber, jsTrim
from misaka.utils.tools_manager import ensureTool, missingToolMessage
from misaka.utils.values import maybe_await, read_field, signal_aborted

# `Type.Number`: the schema says number; the value is read as an integer.
_NUMBER = {"type": "number"}


grepToolSystemPromptContribution = {
    "snippet": "Search file contents for patterns (respects .gitignore)",
    "guidelines": [],
}


class GrepToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pattern: str = Field(description="Search pattern (regex or literal string)")
    path: str | None = Field(default=None, description="Directory or file to search (default: current directory)")
    glob: str | None = Field(default=None, description="Filter files by glob pattern, e.g. '*.ts' or '**/*.spec.ts'")
    ignoreCase: bool | None = Field(default=None, description="Case-insensitive search (default: false)")
    literal: bool | None = Field(default=None, description="Treat pattern as literal string instead of regex (default: false)")
    context: int | None = Field(default=None, description="Number of lines to show before and after each match (default: 0)", json_schema_extra=_NUMBER)
    limit: int | None = Field(default=None, description="Maximum number of matches to return (default: 100)", json_schema_extra=_NUMBER)

DEFAULT_LIMIT = 100


@dataclass(slots=True)
class GrepToolDetails:
    truncation: TruncationResult | None = None
    matchLimitReached: int | None = None
    linesTruncated: bool | None = None


class GrepOperations(Protocol):
    """Pluggable operations for the grep tool.
    Override these to delegate search to remote systems (for example SSH).
    """

    # Check if path is a directory. Throws if path does not exist.
    isDirectory: Callable[[str], Awaitable[bool] | bool]
    # Read file contents for context lines
    readFile: Callable[[str], Awaitable[str] | str]


async def _isDirectory(p: str) -> bool:
    return stat.S_ISDIR((await asyncio.to_thread(os.stat, p)).st_mode)


def _readBytes(p: str) -> bytes:
    with open(p, "rb") as file:
        return file.read()


async def _readFile(p: str) -> str:
    # `readFile(p, "utf-8")`: bytes that are not UTF-8 become U+FFFD, line endings stay as they are.
    return (await asyncio.to_thread(_readBytes, p)).decode("utf-8", errors="replace")


defaultGrepOperations: GrepOperations = SimpleNamespace(  # type: ignore[assignment]
    isDirectory=_isDirectory,
    readFile=_readFile,
)


@dataclass(slots=True)
class GrepToolOptions:
    # Custom operations for grep. Default: local filesystem plus ripgrep
    operations: GrepOperations | None = None


def _coerce_options(options: GrepToolOptions | Mapping[str, Any] | None) -> GrepToolOptions | None:
    """An option bag passed as a mapping, as JavaScript callers pass an object literal."""
    if options is None or isinstance(options, GrepToolOptions):
        return options
    return GrepToolOptions(operations=options.get("operations"))


async def _readAll(stream: asyncio.StreamReader | None) -> str:
    if stream is None:
        return ""
    return (await stream.read()).decode("utf-8", errors="replace")


def createGrepToolDefinition(
    cwd: str,
    options: GrepToolOptions | Mapping[str, Any] | None = None,
) -> ToolDefinition[GrepToolInput | dict[str, Any], GrepToolDetails | None]:
    options = _coerce_options(options)
    customOps = options.operations if options else None

    async def execute(
        _toolCallId: str,
        params: GrepToolInput | dict[str, Any],
        signal: Any | None = None,
        _onUpdate: Callable[[AgentToolResult], None] | None = None,
        ctx: Any = None,
    ) -> AgentToolResult:
        if signal_aborted(signal):
            raise RuntimeError("Operation aborted")
        parsed = GrepToolInput.model_validate(params)
        pattern, searchDir, glob = parsed.pattern, parsed.path, parsed.glob
        ignoreCase, literal, context, limit = parsed.ignoreCase, parsed.literal, parsed.context, parsed.limit

        rgPath = await ensureTool("rg")
        if not rgPath:
            # MISAKA fork: never downloaded, so the error says how to install it.
            raise RuntimeError(missingToolMessage("rg"))

        searchPath = resolveToCwd(searchDir or ".", read_field(ctx, "cwd") or cwd)
        # MISAKA fork: MISAKA's credentials and the web evidence vault are out of the tools' reach.
        from misaka.core.web.evidence import (
            check_material_read,
            is_credential_store,
            is_skills_hub_cache,
        )
        check_material_read(searchPath)
        ops = customOps or defaultGrepOperations
        try:
            isDirectory = await maybe_await(ops.isDirectory(searchPath))
        except Exception:  # noqa: BLE001 - any lookup failure reads as 'path not found'
            raise RuntimeError(f"Path not found: {searchPath}") from None

        contextValue = context if context and context > 0 else 0
        effectiveLimit = max(1, limit if limit is not None else DEFAULT_LIMIT)

        def formatPath(filePath: str) -> str:
            if isDirectory:
                relative = node_path.relative(searchPath, filePath)
                if relative and not relative.startswith(".."):
                    return relative.replace("\\", "/")
            return node_path.basename(filePath)

        fileCache: dict[str, list[str]] = {}

        async def getFileLines(filePath: str) -> list[str]:
            lines = fileCache.get(filePath)
            if lines is None:
                try:
                    content = await maybe_await(ops.readFile(filePath))
                    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                except Exception:  # noqa: BLE001 - an unreadable file shows as unreadable
                    lines = []
                fileCache[filePath] = lines
            return lines

        args: list[str] = ["--json", "--line-number", "--color=never", "--hidden"]
        if ignoreCase:
            args.append("--ignore-case")
        if literal:
            args.append("--fixed-strings")
        if glob:
            args.extend(["--glob", glob])
        args.extend(["--", pattern, searchPath])

        try:
            child = await asyncio.create_subprocess_exec(
                rgPath,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as error:  # noqa: BLE001 - any spawn failure is reported as 'failed to run ripgrep'
            raise RuntimeError(f"Failed to run ripgrep: {error}") from None

        stderrTask = asyncio.create_task(_readAll(child.stderr))
        matchCount = 0
        matchLimitReached = False
        linesTruncated = False
        aborted = False
        killedDueToLimit = False
        outputLines: list[str] = []

        def stopChild(dueToLimit: bool = False) -> None:
            nonlocal killedDueToLimit
            if child.returncode is None:
                killedDueToLimit = dueToLimit
                child.kill()

        async def formatBlock(filePath: str, lineNumber: int) -> list[str]:
            nonlocal linesTruncated
            relativePath = formatPath(filePath)
            lines = await getFileLines(filePath)
            if not len(lines):
                return [f"{relativePath}:{lineNumber}: (unable to read file)"]
            block: list[str] = []
            start = max(1, lineNumber - contextValue) if contextValue > 0 else lineNumber
            end = min(len(lines), lineNumber + contextValue) if contextValue > 0 else lineNumber
            for current in range(start, end + 1):
                lineText = lines[current - 1] if 0 <= current - 1 < len(lines) else ""
                sanitized = lineText.replace("\r", "")
                isMatchLine = current == lineNumber
                # Truncate long lines so grep output stays compact.
                truncated = truncateLine(sanitized)
                if truncated["wasTruncated"]:
                    linesTruncated = True
                if isMatchLine:
                    block.append(f"{relativePath}:{current}: {truncated['text']}")
                else:
                    block.append(f"{relativePath}-{current}- {truncated['text']}")
            return block

        # Collect matches during streaming, then format them after rg exits.
        matches: list[tuple[str, int, str | None]] = []

        def onLine(line: str) -> None:
            nonlocal matchCount, matchLimitReached
            if not jsTrim(line) or matchCount >= effectiveLimit:
                return
            try:
                event = json.loads(line)
            except ValueError:
                return
            if isinstance(event, dict) and event.get("type") == "match":
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                filePath = (data.get("path") or {}).get("text") if isinstance(data.get("path"), dict) else None
                # MISAKA fork: a search over the home passes its credential stores and skills hub caches by.
                if isinstance(filePath, str) and (is_credential_store(filePath) or is_skills_hub_cache(filePath)):
                    return
                matchCount += 1
                lineNumber = data.get("line_number")
                lineText = (data.get("lines") or {}).get("text") if isinstance(data.get("lines"), dict) else None
                if filePath and isinstance(lineNumber, int | float) and not isinstance(lineNumber, bool):
                    matches.append((filePath, int(lineNumber), lineText))
                if matchCount >= effectiveLimit:
                    matchLimitReached = True
                    stopChild(True)

        async def readLines() -> None:
            # readline over the child's stdout: no line-length limit, as Node's readline has none.
            assert child.stdout is not None
            pending = b""
            while True:
                chunk = await child.stdout.read(65536)
                if not chunk:
                    break
                pending += chunk
                *complete, pending = pending.split(b"\n")
                for raw in complete:
                    onLine(raw.removesuffix(b"\r").decode("utf-8", errors="replace"))
            if pending:
                onLine(pending.removesuffix(b"\r").decode("utf-8", errors="replace"))

        readTask = asyncio.create_task(readLines())
        try:
            async with abort_race(signal) as abortTask:
                waitTasks: set[asyncio.Task[Any]] = {readTask}
                if abortTask is not None:
                    waitTasks.add(abortTask)
                done, _pending = await asyncio.wait(waitTasks, return_when=asyncio.FIRST_COMPLETED)
                if abortTask is not None and abortTask in done and readTask not in done:
                    aborted = True
                    stopChild()
                await readTask
            code = await child.wait()
        except asyncio.CancelledError:
            stopChild()
            readTask.cancel()
            stderrTask.cancel()
            await asyncio.gather(readTask, stderrTask, child.wait(), return_exceptions=True)
            raise
        stderr = await stderrTask

        if aborted or signal_aborted(signal):
            raise RuntimeError("Operation aborted")
        if not killedDueToLimit and code != 0 and code != 1:
            errorMsg = jsTrim(stderr) or f"ripgrep exited with code {code}"
            raise RuntimeError(errorMsg)
        if matchCount == 0:
            return AgentToolResult(content=[TextContent(text="No matches found")], details=None)

        # Format matches after streaming finishes so custom readFile() backends can be async.
        for filePath, lineNumber, lineText in matches:
            if contextValue == 0 and lineText is not None:
                relativePath = formatPath(filePath)
                sanitized = lineText.replace("\r\n", "\n").replace("\r", "").removesuffix("\n")
                truncated = truncateLine(sanitized)
                if truncated["wasTruncated"]:
                    linesTruncated = True
                outputLines.append(f"{relativePath}:{lineNumber}: {truncated['text']}")
            else:
                block = await formatBlock(filePath, lineNumber)
                outputLines.extend(block)

        rawOutput = "\n".join(outputLines)
        # Apply byte truncation. There is no line limit here because the match limit already capped rows.
        truncation = truncateHead(rawOutput, TruncationOptions(maxLines=2**53 - 1))
        output = truncation.content
        details = GrepToolDetails()
        # Build actionable notices for truncation and match limits.
        notices: list[str] = []
        if matchLimitReached:
            notices.append(
                f"{effectiveLimit} matches limit reached. Use limit={effectiveLimit * 2} for more, or refine pattern"
            )
            details.matchLimitReached = effectiveLimit
        if truncation.truncated:
            notices.append(f"{formatSize(DEFAULT_MAX_BYTES)} limit reached")
            details.truncation = truncation
        if linesTruncated:
            notices.append(f"Some lines truncated to {GREP_MAX_LINE_LENGTH} chars. Use read tool to see full lines")
            details.linesTruncated = True
        if len(notices) > 0:
            output += f"\n\n[{'. '.join(notices)}]"
        return AgentToolResult(
            content=[TextContent(text=output)],
            details=details if any(getattr(details, field.name) is not None for field in fields(details)) else None,
        )

    return ToolDefinition(
        name="grep",
        label="grep",
        description=f"Search file contents for a pattern. Returns matching lines with file paths and line numbers. Respects .gitignore. Output is truncated to {DEFAULT_LIMIT} matches or {formatNumber(DEFAULT_MAX_BYTES / 1024)}KB (whichever is hit first). Long lines are truncated to {GREP_MAX_LINE_LENGTH} chars.",
        promptSnippet=grepToolSystemPromptContribution["snippet"],
        parameters=GrepToolInput,
        execute=execute,
        **grepRenderers,
    )


def createGrepTool(cwd: str, options: GrepToolOptions | Mapping[str, Any] | None = None) -> AgentTool:
    return wrapToolDefinition(createGrepToolDefinition(cwd, options))


__all__ = [
    "GrepOperations",
    "GrepToolDetails",
    "GrepToolInput",
    "GrepToolOptions",
    "createGrepTool",
    "createGrepToolDefinition",
    "grepToolSystemPromptContribution",
]
