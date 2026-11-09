"""Translated from pi's ``packages/coding-agent/src/core/tools/write.ts`` (v1.1.0).

MISAKA forks:
- Office packages (.docx/.xlsx/.pptx) are refused and sent to the office tool, since writing one as text corrupts it.
- The default ``writeFile`` writes through a symlink or hardlink to the file it names and keeps that file's mode
  (``_common.write_file_text``).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools._common import settled, write_file_text
from misaka.core.tools.file_mutation_queue import withFileMutationQueue
from misaka.core.tools.path_utils import resolveToCwd
from misaka.core.tools.renderers.write import writeRenderers
from misaka.core.tools.tool_definition_wrapper import wrapToolDefinition
from misaka.utils.values import read_field, signal_aborted

writeToolSystemPromptContribution = {
    "snippet": "Create or overwrite files",
    "guidelines": ["Use write only for new files or complete rewrites."],
}


class WriteToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str = Field(description="Path to the file to write (relative or absolute)")
    content: str = Field(description="Content to write to the file")


class WriteOperations(Protocol):
    """Pluggable operations for the write tool.
    Override these to delegate file writing to remote systems (for example SSH).
    """

    # Write content to a file
    writeFile: Callable[[str, str], Awaitable[None]]
    # Create directory recursively
    mkdir: Callable[[str], Awaitable[None]]


async def _mkdir(dir: str) -> None:
    await asyncio.to_thread(os.makedirs, dir, exist_ok=True)


defaultWriteOperations: WriteOperations = SimpleNamespace(  # type: ignore[assignment]
    writeFile=lambda path, content: asyncio.to_thread(write_file_text, path, content),
    mkdir=_mkdir,
)


@dataclass(slots=True)
class WriteToolOptions:
    # Custom operations for file writing. Default: local filesystem
    operations: WriteOperations | None = None


def _coerce_options(options: WriteToolOptions | Mapping[str, Any] | None) -> WriteToolOptions | None:
    """An option bag passed as a mapping, as JavaScript callers pass an object literal."""
    if options is None or isinstance(options, WriteToolOptions):
        return options
    return WriteToolOptions(operations=options.get("operations"))


# MISAKA fork: .csv and .tsv are text and both tools have always handled them; only the packages are refused.
_TEXT_OFFICE_SUFFIXES = frozenset({".csv", ".tsv"})


def _refuse_office_package(absolute_path: str, tool: str) -> None:
    """MISAKA fork: send a .docx/.xlsx/.pptx to the office tool instead of corrupting it.

    These are zip packages. Decoding one as text and writing the result back produces a
    file no program will open, and both tools would report success -- the model finds out
    only when someone tries to open the deliverable. A local import: ``documents.office``
    pulls in openpyxl, and these tools are constructed for every session.
    """
    from misaka.core.documents import office

    suffix = os.path.splitext(absolute_path)[1].lower()
    if suffix in office.SUFFIXES and suffix not in _TEXT_OFFICE_SUFFIXES:
        raise RuntimeError(
            f"{tool} cannot write {suffix}: it is a zip package, not text, and writing it "
            f"as text corrupts it. Use the office tool, which edits it through structured "
            f"operations. To read it, use read."
        )


def createWriteToolDefinition(
    cwd: str,
    options: WriteToolOptions | Mapping[str, Any] | None = None,
) -> ToolDefinition[WriteToolInput | dict[str, Any], None]:
    options = _coerce_options(options)
    ops = (options.operations if options else None) or defaultWriteOperations

    async def execute(
        _toolCallId: str,
        params: WriteToolInput | dict[str, Any],
        signal: Any | None = None,
        _onUpdate: Callable[[AgentToolResult], None] | None = None,
        ctx: Any = None,
    ) -> AgentToolResult:
        parsed = WriteToolInput.model_validate(params)
        path, content = parsed.path, parsed.content
        absolutePath = resolveToCwd(path, read_field(ctx, "cwd") or cwd)
        _refuse_office_package(absolutePath, "write")
        dir = os.path.dirname(absolutePath)

        async def mutate() -> AgentToolResult:
            # Do not reject from an abort event listener here: that would release the
            # mutation queue while an in-flight filesystem operation may still finish.
            # Checking signal.aborted after each await observes the same aborts while
            # keeping the queue locked until the current operation has settled.
            def throwIfAborted() -> None:
                if signal_aborted(signal):
                    raise RuntimeError("Operation aborted")

            throwIfAborted()
            # Create parent directories if needed.
            await settled(ops.mkdir(dir))
            throwIfAborted()

            # Write the file contents.
            await settled(ops.writeFile(absolutePath, content))
            throwIfAborted()

            return AgentToolResult(content=[TextContent(text=f"Successfully wrote to {path}")], details=None)

        return await withFileMutationQueue(absolutePath, mutate)

    return ToolDefinition(
        name="write",
        label="write",
        description="Write content to a file. Creates the file if it doesn't exist, overwrites if it does. Automatically creates parent directories.",
        promptSnippet=writeToolSystemPromptContribution["snippet"],
        promptGuidelines=[*writeToolSystemPromptContribution["guidelines"]],
        parameters=WriteToolInput,
        constrainedSampling={"type": "json_schema", "strict": "prefer"},
        execute=execute,
        **writeRenderers,
    )


def createWriteTool(cwd: str, options: WriteToolOptions | Mapping[str, Any] | None = None) -> AgentTool:
    return wrapToolDefinition(createWriteToolDefinition(cwd, options))


__all__ = [
    "WriteOperations",
    "WriteToolInput",
    "WriteToolOptions",
    "createWriteTool",
    "createWriteToolDefinition",
    "writeToolSystemPromptContribution",
]
