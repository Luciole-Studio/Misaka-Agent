"""Write tool for creating and overwriting files."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools._common import _drain_worker, abort_race, write_file_text
from misaka.core.tools.file_mutation_queue import with_file_mutation_queue
from misaka.core.tools.path_utils import resolve_to_cwd
from misaka.core.tools.render_utils import (
    normalize_display_text,
    render_tool_path,
    replace_tabs,
    str_value,
)
from misaka.core.tools.tool_definition_wrapper import wrap_tool_definition
from misaka.ui.tui import Container, Text
from misaka.ui.tui.interactive.theme.theme import get_language_from_path, highlight_code
from misaka.utils.values import read_field, signal_aborted


class WriteToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str = Field(description="Path to the file to write (relative or absolute)")
    content: str = Field(description="Content to write to the file")


write_tool_system_prompt_contribution = {
    "snippet": "Create or overwrite files",
    "guidelines": ["Use write only for new files or complete rewrites."],
}
writeToolSystemPromptContribution = write_tool_system_prompt_contribution


class WriteOperations(Protocol):
    writeFile: Callable[[str, str], Awaitable[None]]
    mkdir: Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class WriteToolOptions:
    operations: WriteOperations | None = None


@dataclass(slots=True)
class WriteHighlightCache:
    rawPath: str | None
    lang: str
    rawContent: str
    normalizedLines: list[str]
    highlightedLines: list[str]


class WriteCallRenderComponent(Text):
    def __init__(self) -> None:
        super().__init__("", 0, 0)
        self.cache: WriteHighlightCache | None = None


@dataclass(slots=True)
class _DefaultWriteOperations:
    async def writeFile(self, absolute_path: str, content: str) -> None:
        await asyncio.to_thread(write_file_text, absolute_path, content)   # follows symlinks; keeps the file's mode

    async def mkdir(self, directory: str) -> None:
        await asyncio.to_thread(os.makedirs, directory, exist_ok=True)


WRITE_PARTIAL_FULL_HIGHLIGHT_LINES = 50


def _coerce_options(options: WriteToolOptions | Mapping[str, Any] | None) -> WriteToolOptions:
    if options is None:
        return WriteToolOptions()
    if isinstance(options, WriteToolOptions):
        return options
    return WriteToolOptions(operations=options.get("operations"))


def _ensure_write_call_render_component(component: Any) -> WriteCallRenderComponent:
    if not hasattr(component, "cache"):
        component.cache = None
    return component  # type: ignore[return-value]


def _highlight_single_line(line: str, lang: str) -> str:
    highlighted = highlight_code(line, lang)
    return highlighted[0] if highlighted else ""


def _refresh_write_highlight_prefix(cache: WriteHighlightCache) -> None:
    prefix_count = min(WRITE_PARTIAL_FULL_HIGHLIGHT_LINES, len(cache.normalizedLines))
    if prefix_count == 0:
        return
    prefix_source = "\n".join(cache.normalizedLines[:prefix_count])
    prefix_highlighted = highlight_code(prefix_source, cache.lang)
    for index in range(prefix_count):
        cache.highlightedLines[index] = (
            prefix_highlighted[index]
            if index < len(prefix_highlighted)
            else _highlight_single_line(cache.normalizedLines[index] if index < len(cache.normalizedLines) else "", cache.lang)
        )


def _rebuild_write_highlight_cache_full(raw_path: str | None, file_content: str) -> WriteHighlightCache | None:
    lang = get_language_from_path(raw_path) if raw_path else None
    if not lang:
        return None
    display_content = normalize_display_text(file_content)
    normalized = replace_tabs(display_content)
    return WriteHighlightCache(
        rawPath=raw_path,
        lang=lang,
        rawContent=file_content,
        normalizedLines=normalized.split("\n"),
        highlightedLines=highlight_code(normalized, lang),
    )


def _update_write_highlight_cache_incremental(
    cache: WriteHighlightCache | None,
    raw_path: str | None,
    file_content: str,
) -> WriteHighlightCache | None:
    lang = get_language_from_path(raw_path) if raw_path else None
    if not lang:
        return None
    if cache is None:
        return _rebuild_write_highlight_cache_full(raw_path, file_content)
    if cache.lang != lang or cache.rawPath != raw_path:
        return _rebuild_write_highlight_cache_full(raw_path, file_content)
    if not file_content.startswith(cache.rawContent):
        return _rebuild_write_highlight_cache_full(raw_path, file_content)
    if len(file_content) == len(cache.rawContent):
        return cache

    delta_raw = file_content[len(cache.rawContent) :]
    delta_display = normalize_display_text(delta_raw)
    delta_normalized = replace_tabs(delta_display)
    cache.rawContent = file_content
    if not cache.normalizedLines:
        cache.normalizedLines.append("")
        cache.highlightedLines.append("")

    segments = delta_normalized.split("\n")
    last_index = len(cache.normalizedLines) - 1
    cache.normalizedLines[last_index] += segments[0]
    cache.highlightedLines[last_index] = _highlight_single_line(cache.normalizedLines[last_index], cache.lang)
    for segment in segments[1:]:
        cache.normalizedLines.append(segment)
        cache.highlightedLines.append(_highlight_single_line(segment, cache.lang))
    _refresh_write_highlight_prefix(cache)
    return cache


def _trim_trailing_empty_lines(lines: list[str]) -> list[str]:
    end = len(lines)
    while end > 0 and lines[end - 1] == "":
        end -= 1
    return lines[:end]


def _format_write_call(
    args: Any,
    *,
    expanded: bool,
    theme_obj: Any,
    cache: WriteHighlightCache | None,
    cwd: str,
) -> str:
    from misaka.ui.tui.interactive.components.keybinding_hints import key_hint

    raw_path = str_value(read_field(args, "file_path", read_field(args, "path")))
    file_content = str_value(read_field(args, "content"))
    text = f"{theme_obj.fg('toolTitle', theme_obj.bold('write'))} {render_tool_path(raw_path, theme_obj, cwd)}"

    if file_content is None:
        text += f"\n\n{theme_obj.fg('error', '[invalid content arg - expected string]')}"
    elif file_content:
        lang = get_language_from_path(raw_path) if raw_path else None
        rendered_lines = (
            cache.highlightedLines
            if lang and cache is not None
            else highlight_code(replace_tabs(normalize_display_text(file_content)), lang)
            if lang
            else normalize_display_text(file_content).split("\n")
        )
        lines = _trim_trailing_empty_lines(rendered_lines)
        total_lines = len(lines)
        max_lines = len(lines) if expanded else 10
        display_lines = lines[:max_lines]
        remaining = len(lines) - max_lines
        text += "\n\n" + "\n".join(
            line if lang else theme_obj.fg("toolOutput", replace_tabs(line)) for line in display_lines
        )
        if remaining > 0:
            text += (
                theme_obj.fg("muted", f"\n... ({remaining} more lines, {total_lines} total,")
                + f" {key_hint('app.tools.expand', 'to expand')}"
                + theme_obj.fg("muted", ")")
            )

    return text


def _format_write_result(result: Any, theme_obj: Any) -> str | None:
    if not read_field(result, "isError"):
        return None
    content = read_field(result, "content", [])
    if not isinstance(content, list):
        return None
    output = "\n".join((read_field(block, "text") or "") for block in content if read_field(block, "type") == "text")
    if not output:
        return None
    return f"\n{theme_obj.fg('error', output)}"


def _refuse_office_package(absolute_path: str, tool: str) -> None:
    """Send a .docx/.xlsx/.pptx to the office tool instead of corrupting it.

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


# .csv and .tsv are text and both tools have always handled them; only the packages are
# refused here.
_TEXT_OFFICE_SUFFIXES = frozenset({".csv", ".tsv"})


def create_write_tool_definition(
    cwd: str,
    options: WriteToolOptions | Mapping[str, Any] | None = None,
) -> ToolDefinition[WriteToolInput | dict[str, Any], None]:
    resolved_options = _coerce_options(options)
    operations = (
        resolved_options.operations
        if resolved_options.operations is not None
        else _DefaultWriteOperations()
    )

    async def execute(
        _tool_call_id: str,
        params: WriteToolInput | dict[str, Any],
        signal: Any | None = None,
        _on_update: Callable[[AgentToolResult], None] | None = None,
        _ctx: Any = None,
    ) -> AgentToolResult:
        parsed = WriteToolInput.model_validate(params)
        absolute_path = resolve_to_cwd(parsed.path, cwd)
        _refuse_office_package(absolute_path, "write")
        directory = os.path.dirname(absolute_path)

        async def mutate() -> AgentToolResult:
            if signal_aborted(signal):
                raise RuntimeError("Operation aborted")

            aborted = False

            def throw_if_aborted() -> None:
                if aborted or signal_aborted(signal):
                    raise RuntimeError("Operation aborted")

            async def worker() -> AgentToolResult | None:
                try:
                    throw_if_aborted()
                    await operations.mkdir(directory)
                    throw_if_aborted()
                    await operations.writeFile(absolute_path, parsed.content)
                    throw_if_aborted()
                    return AgentToolResult(
                        content=[TextContent(text=f"Successfully wrote {len(parsed.content.encode('utf-8'))} bytes to {parsed.path}")],
                        details=None,
                    )
                except Exception:
                    if aborted:
                        return None
                    raise

            worker_task = asyncio.create_task(worker())
            async with abort_race(signal) as abort_task:
                try:
                    if abort_task is None:
                        result = await asyncio.shield(worker_task)
                        if result is None:
                            raise RuntimeError("Operation aborted")
                        return result

                    done, _pending = await asyncio.wait({worker_task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
                    if abort_task in done and worker_task not in done:
                        aborted = True
                        # A to-thread/custom write cannot be stopped by cancelling its Task.
                        # Hold the per-file lock until it really ends so it cannot overtake
                        # and overwrite a later mutation.
                        if await _drain_worker(worker_task):
                            raise asyncio.CancelledError
                        raise RuntimeError("Operation aborted")

                    result = await asyncio.shield(worker_task)
                    if aborted or result is None:
                        raise RuntimeError("Operation aborted")
                    return result
                except BaseException as error:
                    aborted = True
                    cancelled = await _drain_worker(worker_task)
                    if cancelled and not isinstance(error, asyncio.CancelledError):
                        raise asyncio.CancelledError from error
                    raise

        return await with_file_mutation_queue(absolute_path, mutate)

    def render_call(args: Any, theme_obj: Any, context: Any) -> WriteCallRenderComponent:
        raw_path = str_value(read_field(args, "file_path", read_field(args, "path")))
        file_content = str_value(read_field(args, "content"))
        component = (
            _ensure_write_call_render_component(context.lastComponent)
            if context.lastComponent is not None
            else WriteCallRenderComponent()
        )
        if file_content is not None:
            component.cache = (
                _rebuild_write_highlight_cache_full(raw_path, file_content)
                if context.argsComplete
                else _update_write_highlight_cache_incremental(component.cache, raw_path, file_content)
            )
        else:
            component.cache = None
        component.setText(
            _format_write_call(
                args,
                expanded=context.expanded,
                theme_obj=theme_obj,
                cache=component.cache,
                cwd=context.cwd,
            )
        )
        return component

    def render_result(result: Any, _options: Any, theme_obj: Any, context: Any) -> Any:
        output = _format_write_result({"content": read_field(result, "content", []), "isError": context.isError}, theme_obj)
        if not output:
            component = context.lastComponent if context.lastComponent is not None else Container()
            component.clear()
            return component
        text = context.lastComponent if context.lastComponent is not None else Text("", 0, 0)
        text.setText(output)
        return text

    return ToolDefinition(
        name="write",
        label="write",
        description=(
            "Write content to a file. Creates the file if it doesn't exist, overwrites if it does. "
            "Automatically creates parent directories."
        ),
        promptSnippet=write_tool_system_prompt_contribution["snippet"],
        promptGuidelines=list(write_tool_system_prompt_contribution["guidelines"]),
        parameters=WriteToolInput,
        constrainedSampling={"type": "json_schema", "strict": "prefer"},
        execute=execute,
        renderCall=render_call,
        renderResult=render_result,
    )


def create_write_tool(cwd: str, options: WriteToolOptions | Mapping[str, Any] | None = None) -> AgentTool:
    return wrap_tool_definition(create_write_tool_definition(cwd, options))


createWriteTool = create_write_tool
createWriteToolDefinition = create_write_tool_definition

__all__ = [
    "WriteOperations",
    "WriteToolInput",
    "WriteToolOptions",
    "createWriteTool",
    "createWriteToolDefinition",
    "writeToolSystemPromptContribution",
]
