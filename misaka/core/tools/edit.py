"""Translated from pi's ``packages/coding-agent/src/core/tools/edit.ts`` (v1.1.0).

MISAKA forks:
- Office packages (.docx/.xlsx/.pptx) are refused and sent to the office tool, as in ``write``.
- A file that is not valid UTF-8 is refused, where pi's ``toString("utf-8")`` replaces the bad bytes and writes the
  replacement characters back.
- The default operations read regular files only, below a size cap, and write through links (``_common``).
"""

from __future__ import annotations

import asyncio
import errno
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools._common import (
    regular_file,
    settled,
    whole_file_bytes,
    write_file_text,
)
from misaka.core.tools.edit_diff import (
    Edit,
    applyEditsToNormalizedContent,
    detectLineEnding,
    generateDiffString,
    generateUnifiedPatch,
    normalizeToLF,
    restoreLineEndings,
)
from misaka.core.tools.file_mutation_queue import withFileMutationQueue
from misaka.core.tools.path_utils import resolveToCwd
from misaka.core.tools.renderers.edit import editRenderers
from misaka.core.tools.tool_definition_wrapper import wrapToolDefinition
from misaka.core.tools.write import _refuse_office_package
from misaka.utils.text import splitBom
from misaka.utils.values import read_field, signal_aborted


class ReplaceEditInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    oldText: str = Field(
        description="Exact text for one targeted replacement. It must be unique in the original file and must not overlap with any other edits[].oldText in the same call."
    )
    newText: str = Field(description="Replacement text for this targeted edit.")


editToolSystemPromptContribution = {
    "snippet": "Make precise file edits with exact text replacement, including multiple disjoint edits in one call",
    "guidelines": [
        "Use edit for precise changes (edits[].oldText must match exactly)",
        "When changing multiple separate locations in one file, use one edit call with multiple entries in edits[] instead of multiple edit calls",
        "Each edits[].oldText is matched against the original file, not after earlier edits are applied. Do not emit overlapping or nested edits. Merge nearby changes into one edit.",
        "Keep edits[].oldText as small as possible while still being unique in the file. Do not pad with large unchanged regions.",
    ],
}


class EditToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str = Field(description="Path to the file to edit (relative or absolute)")
    edits: list[ReplaceEditInput] = Field(
        description="One or more targeted replacements. Each edit is matched against the original file, not incrementally. Do not include overlapping or nested edits. If two changes touch the same block or nearby lines, merge them into one edit instead."
    )


def isSingleEditInput(value: Any) -> bool:
    if not value or not isinstance(value, dict):
        return False

    edit = value
    return isinstance(edit.get("oldText"), str) and isinstance(edit.get("newText"), str)


@dataclass(slots=True)
class EditToolDetails:
    # Display-oriented diff of the changes made
    diff: str
    # Standard unified patch of the changes made
    patch: str
    # Line number of the first change in the new file (for editor navigation)
    firstChangedLine: int | None = None


class EditOperations(Protocol):
    """Pluggable operations for the edit tool.
    Override these to delegate file editing to remote systems (for example SSH).
    """

    # Read file contents as a Buffer
    readFile: Callable[[str], Awaitable[bytes]]
    # Write content to a file
    writeFile: Callable[[str, str], Awaitable[None]]
    # Check if file is readable and writable (throw if not)
    access: Callable[[str], Awaitable[None]]


defaultEditOperations: EditOperations = SimpleNamespace(  # type: ignore[assignment]
    readFile=lambda path: asyncio.to_thread(whole_file_bytes, path, "edit"),
    writeFile=lambda path, content: asyncio.to_thread(write_file_text, path, content),
    access=lambda path: asyncio.to_thread(regular_file, path, writable=True),
)


@dataclass(slots=True)
class EditToolOptions:
    # Custom operations for file editing. Default: local filesystem
    operations: EditOperations | None = None


def _coerce_options(options: EditToolOptions | Mapping[str, Any] | None) -> EditToolOptions | None:
    """An option bag passed as a mapping, as JavaScript callers pass an object literal."""
    if options is None or isinstance(options, EditToolOptions):
        return options
    return EditToolOptions(operations=options.get("operations"))


def prepareEditArguments(input: Any) -> Any:
    if not input or not isinstance(input, dict):
        return input

    args = input

    # Some models (Opus 4.6, GLM-5.1) send edits as a JSON string instead of an array.
    # Others send a single edit object instead of a one-element edits array.
    if isinstance(args.get("edits"), str):
        try:
            parsed = json.loads(args["edits"])
            if isinstance(parsed, list):
                args["edits"] = parsed
            elif isSingleEditInput(parsed):
                args["edits"] = [parsed]
        except ValueError:
            pass
    elif isSingleEditInput(args.get("edits")):
        args["edits"] = [args["edits"]]

    legacy = args
    if not isinstance(legacy.get("oldText"), str) or not isinstance(legacy.get("newText"), str):
        return args

    edits = [*legacy["edits"]] if isinstance(legacy.get("edits"), list) else []
    edits.append({"oldText": legacy["oldText"], "newText": legacy["newText"]})
    rest = {key: value for key, value in legacy.items() if key not in ("oldText", "newText")}
    return {**rest, "edits": edits}


def validateEditInput(input: EditToolInput) -> tuple[str, list[Edit]]:
    if not isinstance(input.edits, list) or len(input.edits) == 0:
        raise RuntimeError("Edit tool input is invalid. edits must contain at least one replacement.")
    return input.path, [Edit(oldText=edit.oldText, newText=edit.newText) for edit in input.edits]


def createEditToolDefinition(
    cwd: str,
    options: EditToolOptions | Mapping[str, Any] | None = None,
) -> ToolDefinition[EditToolInput | dict[str, Any], EditToolDetails | None]:
    options = _coerce_options(options)
    ops = (options.operations if options else None) or defaultEditOperations

    async def execute(
        _toolCallId: str,
        input: EditToolInput | dict[str, Any],
        signal: Any | None = None,
        _onUpdate: Callable[[AgentToolResult], None] | None = None,
        ctx: Any = None,
    ) -> AgentToolResult:
        path, edits = validateEditInput(EditToolInput.model_validate(input))
        absolutePath = resolveToCwd(path, read_field(ctx, "cwd") or cwd)
        # MISAKA fork: Office packages go to the office tool.
        _refuse_office_package(absolutePath, "edit")

        async def mutate() -> AgentToolResult:
            # Do not reject from an abort event listener here: that would release the
            # mutation queue while an in-flight filesystem operation may still finish.
            # Checking signal.aborted after each await observes the same aborts while
            # keeping the queue locked until the current operation has settled.
            def throwIfAborted() -> None:
                if signal_aborted(signal):
                    raise RuntimeError("Operation aborted")

            throwIfAborted()

            # Check if file exists.
            try:
                await settled(ops.access(absolutePath))
            except Exception as error:  # noqa: BLE001 - any access failure becomes the user-facing edit error
                throwIfAborted()
                code = errno.errorcode.get(error.errno) if isinstance(error, OSError) and error.errno else None
                errorMessage = f"Error code: {code}" if code else f"Error: {error}"
                raise RuntimeError(f"Could not edit file: {path}. {errorMessage}.") from None
            throwIfAborted()

            # Read the file.
            buffer = await settled(ops.readFile(absolutePath))
            try:
                rawContent = buffer.decode("utf-8")
            except UnicodeDecodeError as error:
                # MISAKA fork: refuse a file that is not UTF-8 rather than corrupt it.
                raise RuntimeError(
                    f"Could not edit file: {path}. It is not valid UTF-8 text ({error}); "
                    "editing it as text would corrupt it. Use bash for binary files."
                ) from None
            throwIfAborted()

            # Strip BOM before matching. The model will not include an invisible BOM in oldText.
            split = splitBom(rawContent)
            bom, content = split["bom"], split["text"]
            originalEnding = detectLineEnding(content)
            normalizedContent = normalizeToLF(content)
            applied = applyEditsToNormalizedContent(normalizedContent, edits, path)
            baseContent, newContent = applied.baseContent, applied.newContent
            throwIfAborted()

            finalContent = bom + restoreLineEndings(newContent, originalEnding)
            await settled(ops.writeFile(absolutePath, finalContent))
            throwIfAborted()

            diffResult = generateDiffString(baseContent, newContent)
            patch = generateUnifiedPatch(path, baseContent, newContent)
            return AgentToolResult(
                content=[TextContent(text=f"Successfully replaced {len(edits)} block(s) in {path}.")],
                details=EditToolDetails(diff=diffResult.diff, patch=patch, firstChangedLine=diffResult.firstChangedLine),
            )

        return await withFileMutationQueue(absolutePath, mutate)

    return ToolDefinition(
        name="edit",
        label="edit",
        description="Edit a single file using exact text replacement. Every edits[].oldText must match a unique, non-overlapping region of the original file. If two changes affect the same block or nearby lines, merge them into one edit instead of emitting overlapping edits. Do not include large unchanged regions just to connect distant changes.",
        promptSnippet=editToolSystemPromptContribution["snippet"],
        promptGuidelines=[*editToolSystemPromptContribution["guidelines"]],
        parameters=EditToolInput,
        constrainedSampling={"type": "json_schema", "strict": "prefer"},
        renderShell="self",
        prepareArguments=prepareEditArguments,
        execute=execute,
        **editRenderers,
    )


def createEditTool(cwd: str, options: EditToolOptions | Mapping[str, Any] | None = None) -> AgentTool:
    return wrapToolDefinition(createEditToolDefinition(cwd, options))


__all__ = [
    "EditOperations",
    "EditToolDetails",
    "EditToolInput",
    "EditToolOptions",
    "createEditTool",
    "createEditToolDefinition",
    "editToolSystemPromptContribution",
]
