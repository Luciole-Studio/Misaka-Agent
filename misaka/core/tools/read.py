"""Read tool for text and image files."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import Api, ImageContent, Model, TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools._common import (
    _ignore_background_task_result,
    _string_arg,
    abort_race,
)
from misaka.core.tools.path_utils import resolve_read_path_async, resolve_to_cwd
from misaka.core.tools.render_utils import (
    get_text_output,
    render_tool_path,
    replace_tabs,
)
from misaka.core.tools.tool_definition_wrapper import wrap_tool_definition
from misaka.core.tools.truncate import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    TruncationResult,
    format_size,
    truncate_head,
)
from misaka.ui.tui import Text
from misaka.ui.tui.interactive.theme.theme import get_language_from_path, highlight_code
from misaka.utils.image_process import ProcessImageOptions, process_image
from misaka.utils.image_resize import ImageResizeOptions
from misaka.utils.mime import detect_supported_image_mime_type_from_file
from misaka.utils.paths import format_path_relative_to_cwd_or_absolute
from misaka.utils.values import read_field, signal_aborted


class ReadToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str = Field(description="Path to the file to read (relative or absolute)")
    offset: int | None = Field(default=None, description="Line number to start reading from (1-indexed)")
    limit: int | None = Field(default=None, description="Maximum number of lines to read")
    cell_range: str | None = Field(
        default=None,
        description=(
            "Spreadsheet only: an A1-style range such as \"Sheet1!A3:D15\" to read just that "
            "region instead of the whole workbook. Use it to re-read part of a large sheet."
        ),
    )


@dataclass(slots=True)
class ReadToolDetails:
    truncation: TruncationResult | None = None


@dataclass(slots=True)
class _CompactReadClassification:
    kind: str
    label: str


COMPACT_RESOURCE_FILE_NAMES = {"AGENTS.override.md", "AGENTS.md", "AGENTS.MD", "CLAUDE.md", "CLAUDE.MD"}


class ReadOperations(Protocol):
    readFile: Callable[[str], Awaitable[bytes]]
    access: Callable[[str], Awaitable[None]]
    detectImageMimeType: Callable[[str], Awaitable[str | None | Any]] | None


@dataclass(slots=True)
class ReadToolOptions:
    autoResizeImages: bool = True
    # Used when the session's model declares no `inputLimits.images.resize` of its own.
    resizeOptions: ImageResizeOptions | None = None
    operations: ReadOperations | None = None


@dataclass(slots=True)
class _DefaultReadOperations:
    async def readFile(self, absolute_path: str) -> bytes:
        return await asyncio.to_thread(Path(absolute_path).read_bytes)

    async def access(self, absolute_path: str) -> None:
        def _check() -> None:
            with open(absolute_path, "rb"):
                return

        await asyncio.to_thread(_check)

    async def detectImageMimeType(self, absolute_path: str) -> str | None:
        return await detect_supported_image_mime_type_from_file(absolute_path)


def _coerce_options(options: ReadToolOptions | Mapping[str, Any] | None) -> ReadToolOptions:
    if options is None:
        return ReadToolOptions()
    if isinstance(options, ReadToolOptions):
        return options
    return ReadToolOptions(
        autoResizeImages=options.get("autoResizeImages", True),
        operations=options.get("operations"),
    )


def _format_read_line_range(args: Mapping[str, Any] | None, theme_obj: Any) -> str:
    if read_field(args, "offset") is None and read_field(args, "limit") is None:
        return ""
    start_line = read_field(args, "offset") or 1
    limit = read_field(args, "limit")
    end_line = start_line + limit - 1 if limit is not None else ""
    return theme_obj.fg("warning", f":{start_line}{f'-{end_line}' if end_line else ''}")


def _format_read_call(args: Mapping[str, Any] | None, theme_obj: Any, cwd: str) -> str:
    raw_path = _string_arg(read_field(args, "file_path", read_field(args, "path")))
    return (
        f"{theme_obj.fg('toolTitle', theme_obj.bold('read'))} "
        f"{render_tool_path(raw_path, theme_obj, cwd)}{_format_read_line_range(args, theme_obj)}"
    )


def _trim_trailing_empty_lines(lines: list[str]) -> list[str]:
    end = len(lines)
    while end > 0 and lines[end - 1] == "":
        end -= 1
    return lines[:end]


def _get_non_vision_image_note(model: Model[Api] | None) -> str | None:
    if model is None or "image" in model.input:
        return None
    return "[Current model does not support images. The image will be omitted from this request.]"


def _to_posix_path(file_path: str) -> str:
    return file_path.replace(os.sep, "/")


def _get_compact_read_classification(args: Mapping[str, Any] | None, cwd: str) -> _CompactReadClassification | None:
    raw_path = _string_arg(read_field(args, "file_path", read_field(args, "path")))
    if not raw_path:
        return None

    absolute_path = resolve_to_cwd(raw_path, cwd)
    file_name = os.path.basename(absolute_path)
    if file_name == "SKILL.md":
        return _CompactReadClassification(kind="skill", label=os.path.basename(os.path.dirname(absolute_path)) or file_name)

    if file_name in COMPACT_RESOURCE_FILE_NAMES:
        return _CompactReadClassification(
            kind="resource",
            label=format_path_relative_to_cwd_or_absolute(absolute_path, cwd),
        )
    return None


def _format_compact_read_call(
    classification: _CompactReadClassification,
    args: Mapping[str, Any] | None,
    theme_obj: Any,
) -> str:
    from misaka.ui.tui.interactive.components.keybinding_hints import key_text

    expand_hint = theme_obj.fg("dim", f" ({key_text('app.tools.expand')} to expand)")
    if classification.kind == "skill":
        return (
            theme_obj.fg("customMessageLabel", "\x1b[1m[skill]\x1b[22m ")
            + theme_obj.fg("customMessageText", classification.label)
            + _format_read_line_range(args, theme_obj)
            + expand_hint
        )

    return (
        theme_obj.fg("toolTitle", theme_obj.bold(f"read {classification.kind}"))
        + " "
        + theme_obj.fg("accent", classification.label)
        + _format_read_line_range(args, theme_obj)
        + expand_hint
    )


def _format_read_result(
    args: Mapping[str, Any] | None,
    result: AgentToolResult | Mapping[str, Any],
    options: Any,
    theme_obj: Any,
    show_images: bool,
    cwd: str,
    is_error: bool,
) -> str:
    from misaka.ui.tui.interactive.components.keybinding_hints import key_hint

    if not bool(read_field(options, "expanded")) and not is_error and _get_compact_read_classification(args, cwd):
        return ""

    raw_path = _string_arg(read_field(args, "file_path", read_field(args, "path")))
    output = get_text_output(result, show_images)
    lang = get_language_from_path(raw_path) if raw_path else None
    rendered_lines = highlight_code(replace_tabs(output), lang) if lang else output.split("\n")
    lines = _trim_trailing_empty_lines(rendered_lines)
    max_lines = len(lines) if bool(read_field(options, "expanded")) else 10
    display_lines = lines[:max_lines]
    remaining = len(lines) - max_lines
    text = "\n" + "\n".join(replace_tabs(line) if lang else theme_obj.fg("toolOutput", replace_tabs(line)) for line in display_lines)
    if remaining > 0:
        more_lines_text = theme_obj.fg("muted", f"\n... ({remaining} more lines,")
        text += f"{more_lines_text} {key_hint('app.tools.expand', 'to expand')})"

    details = read_field(result, "details")
    truncation = read_field(details, "truncation")
    if bool(read_field(truncation, "truncated")):
        if read_field(truncation, "firstLineExceedsLimit"):
            warning = f"[First line exceeds {format_size(read_field(truncation, 'maxBytes') or DEFAULT_MAX_BYTES)} limit]"
        elif read_field(truncation, "truncatedBy") == "lines":
            warning = (
                f"[Truncated: showing {read_field(truncation, 'outputLines')} of {read_field(truncation, 'totalLines')} lines "
                f"({read_field(truncation, 'maxLines') or DEFAULT_MAX_LINES} line limit)]"
            )
        else:
            warning = (
                f"[Truncated: {read_field(truncation, 'outputLines')} lines shown "
                f"({format_size(read_field(truncation, 'maxBytes') or DEFAULT_MAX_BYTES)} limit)]"
            )
        text += "\n" + theme_obj.fg("warning", warning)
    return text


def _office_format(absolute_path: str) -> str | None:
    """The Office renderer key for this path, or ``None``.

    A local import: ``documents.office`` pulls in openpyxl, and ``read`` is constructed for
    every session whether or not one ever opens a workbook.
    """
    from misaka.core.documents import office
    return (office.format_of(absolute_path)
            or office.soffice.LEGACY.get(os.path.splitext(absolute_path)[1].lower()))


async def _render_office(absolute_path: str, cell_range: str | None, workspace: str) -> str:
    """The document as markdown, off the event loop.

    Parsing a workbook and walking its cells is seconds of CPU on a large file, and the
    session is a single loop: doing it inline holds every other tool call still. Rendering
    goes through the cache, so paging one document with ``offset`` parses it once.
    ``cell_range`` deliberately bypasses the cache -- it is a different rendering of the
    same file, and keying the store on it would let one range's answer serve another's.
    """
    from misaka.core.documents import office

    if cell_range:
        return await asyncio.to_thread(office.render, absolute_path, cell_range=cell_range)
    return await asyncio.to_thread(
        office.render_cached, absolute_path, lambda: office.render(absolute_path), workspace=workspace)


def _model_resize_options(model: Any) -> ImageResizeOptions | None:
    """The session model's `inputLimits.images.resize`, as the resizer's own options."""
    images = getattr(getattr(model, "inputLimits", None), "images", None)
    resize = getattr(images, "resize", None)
    if resize is None:
        return None
    return ImageResizeOptions(
        maxWidth=resize.maxWidth, maxHeight=resize.maxHeight, maxBytes=resize.maxBytes, jpegQuality=resize.jpegQuality
    )


def create_read_tool_definition(
    cwd: str,
    options: ReadToolOptions | Mapping[str, Any] | None = None,
) -> ToolDefinition[ReadToolInput | dict[str, Any], ReadToolDetails | None]:
    resolved_options = _coerce_options(options)
    auto_resize_images = resolved_options.autoResizeImages
    fallback_resize_options = resolved_options.resizeOptions
    operations = resolved_options.operations or _DefaultReadOperations()

    async def execute(
        _tool_call_id: str,
        params: ReadToolInput | dict[str, Any],
        signal: Any | None = None,
        _on_update: Callable[[AgentToolResult], None] | None = None,
        ctx: Any = None,
    ) -> AgentToolResult:
        parsed = ReadToolInput.model_validate(params)

        if signal_aborted(signal):
            raise RuntimeError("Operation aborted")

        async def worker() -> AgentToolResult:
            absolute_path = await resolve_read_path_async(parsed.path, cwd)
            if signal_aborted(signal):
                return AgentToolResult(content=[], details=None)
            await operations.access(absolute_path)
            if signal_aborted(signal):
                return AgentToolResult(content=[], details=None)

            detect_mime = getattr(operations, "detectImageMimeType", None)
            mime_type = await detect_mime(absolute_path) if callable(detect_mime) else None
            content: list[TextContent | ImageContent]
            details: ReadToolDetails | None = None
            non_vision_image_note = _get_non_vision_image_note(getattr(ctx, "model", None))

            if mime_type:
                buffer = await operations.readFile(absolute_path)
                processed = await process_image(
                    buffer,
                    mime_type,
                    ProcessImageOptions(
                        autoResizeImages=auto_resize_images,
                        resizeOptions=_model_resize_options(getattr(ctx, "model", None)) or fallback_resize_options,
                    ),
                )
                if not processed.ok:
                    text_note = f"Read image file [{mime_type}]\n{processed.message}"
                    if non_vision_image_note:
                        text_note += f"\n{non_vision_image_note}"
                    content = [TextContent(text=text_note)]
                else:
                    text_note = f"Read image file [{processed.mimeType}]"
                    if processed.hints:
                        text_note += "\n" + "\n".join(processed.hints)
                    if non_vision_image_note:
                        text_note += f"\n{non_vision_image_note}"
                    content = [
                        TextContent(text=text_note),
                        ImageContent(data=processed.data, mimeType=processed.mimeType),
                    ]
            else:
                # An Office file is rendered rather than decoded: its bytes are a zip, and
                # ``errors="replace"`` on them produces pages of replacement characters that
                # look like content and quote against nothing. The rendering is the corpus'
                # own (``documents/office``), so what is shown here is what ``doc_read``
                # would show -- a quotation copied out of this verifies against the indexed
                # document. Everything below is unchanged: offset/limit page the rendering
                # exactly as they page a text file.
                if _office_format(absolute_path) is not None:
                    text_content = await _render_office(absolute_path, parsed.cell_range, cwd)
                elif parsed.cell_range:
                    raise RuntimeError(
                        "cell_range names a sheet and a range, and this is not a spreadsheet. "
                        "Call read again without it."
                    )
                else:
                    buffer = await operations.readFile(absolute_path)
                    text_content = buffer.decode("utf-8", errors="replace")
                all_lines = text_content.split("\n")
                total_file_lines = len(all_lines)
                start_line = max(0, parsed.offset - 1) if parsed.offset else 0
                start_line_display = start_line + 1
                if start_line >= len(all_lines):
                    raise RuntimeError(f"Offset {parsed.offset} is beyond end of file ({len(all_lines)} lines total)")

                user_limited_lines: int | None = None
                if parsed.limit is not None:
                    end_line = min(start_line + parsed.limit, len(all_lines))
                    selected_content = "\n".join(all_lines[start_line:end_line])
                    user_limited_lines = end_line - start_line
                else:
                    selected_content = "\n".join(all_lines[start_line:])

                truncation = truncate_head(selected_content)
                if truncation.firstLineExceedsLimit:
                    first_line_size = format_size(len(all_lines[start_line].encode("utf-8")))
                    output_text = (
                        f"[Line {start_line_display} is {first_line_size}, exceeds {format_size(DEFAULT_MAX_BYTES)} limit. "
                        f"Use bash: sed -n '{start_line_display}p' {parsed.path} | head -c {DEFAULT_MAX_BYTES}]"
                    )
                    details = ReadToolDetails(truncation=truncation)
                elif truncation.truncated:
                    end_line_display = start_line_display + truncation.outputLines - 1
                    next_offset = end_line_display + 1
                    output_text = truncation.content
                    if truncation.truncatedBy == "lines":
                        output_text += (
                            f"\n\n[Showing lines {start_line_display}-{end_line_display} of {total_file_lines}. "
                            f"Use offset={next_offset} to continue.]"
                        )
                    else:
                        output_text += (
                            f"\n\n[Showing lines {start_line_display}-{end_line_display} of {total_file_lines} "
                            f"({format_size(DEFAULT_MAX_BYTES)} limit). Use offset={next_offset} to continue.]"
                        )
                    details = ReadToolDetails(truncation=truncation)
                elif user_limited_lines is not None and start_line + user_limited_lines < len(all_lines):
                    remaining = len(all_lines) - (start_line + user_limited_lines)
                    next_offset = start_line + user_limited_lines + 1
                    output_text = (
                        f"{truncation.content}\n\n[{remaining} more lines in file. Use offset={next_offset} to continue.]"
                    )
                else:
                    output_text = truncation.content

                content = [TextContent(text=output_text)]

            if signal_aborted(signal):
                return AgentToolResult(content=[], details=None)
            return AgentToolResult(content=content, details=details)

        worker_task = asyncio.create_task(worker())
        async with abort_race(signal) as abort_task:
            if abort_task is None:
                return await worker_task

            done, _pending = await asyncio.wait({worker_task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
            if abort_task in done and worker_task not in done:
                _ignore_background_task_result(worker_task)
                raise RuntimeError("Operation aborted")

            result = await worker_task
            if signal_aborted(signal):
                raise RuntimeError("Operation aborted")
            return result

    def render_call(args: Mapping[str, Any] | None, theme_obj: Any, context: Any) -> Text:
        text = context.lastComponent if isinstance(context.lastComponent, Text) else Text("", 0, 0)
        classification = _get_compact_read_classification(args, context.cwd) if not context.expanded else None
        text.setText(
            _format_compact_read_call(classification, args, theme_obj)
            if classification is not None
            else _format_read_call(args, theme_obj, context.cwd)
        )
        return text

    def render_result(result: Any, options_obj: Any, theme_obj: Any, context: Any) -> Text:
        text = context.lastComponent if isinstance(context.lastComponent, Text) else Text("", 0, 0)
        text.setText(
            _format_read_result(
                context.args,
                result,
                options_obj,
                theme_obj,
                bool(context.showImages),
                context.cwd,
                bool(context.isError),
            )
        )
        return text

    return ToolDefinition(
        name="read",
        label="read",
        description=(
            "Read the contents of a file. Supports text files, Office documents "
            "(xlsx, xlsm, csv, tsv, docx, docm, pptx, pptm), and images "
            "(jpg, png, gif, webp, bmp). "
            f"Images are sent as attachments. For text files, output is truncated to {DEFAULT_MAX_LINES} "
            f"lines or {DEFAULT_MAX_BYTES // 1024}KB (whichever is hit first). Use offset/limit for large files. "
            "When you need the full file, continue with offset until complete. A spreadsheet "
            "is rendered with real cell coordinates, its formulas and its sheet-level "
            "formatting; pass cell_range to re-read one region of a large sheet. A Word "
            "document keeps its headings, lists, tables in body order and its footnotes; a "
            "deck is rendered slide by slide with chart values and connector flow."
        ),
        promptSnippet="Read file contents",
        promptGuidelines=["Use read to examine files instead of cat or sed."],
        parameters=ReadToolInput,
        constrainedSampling={"type": "json_schema", "strict": "prefer"},
        execute=execute,
        renderCall=render_call,
        renderResult=render_result,
    )


def create_read_tool(cwd: str, options: ReadToolOptions | Mapping[str, Any] | None = None) -> AgentTool:
    return wrap_tool_definition(create_read_tool_definition(cwd, options))


createReadTool = create_read_tool
createReadToolDefinition = create_read_tool_definition

__all__ = [
    "ReadOperations",
    "ReadToolDetails",
    "ReadToolInput",
    "ReadToolOptions",
    "createReadTool",
    "createReadToolDefinition",
]
