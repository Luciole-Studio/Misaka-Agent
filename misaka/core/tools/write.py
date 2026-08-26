"""Write tool for creating and overwriting files."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools.file_mutation_queue import with_file_mutation_queue
from misaka.core.tools.path_utils import resolve_to_cwd
from misaka.core.tools.render_utils import (
    invalid_arg_text,
    normalize_display_text,
    replace_tabs,
    shorten_path,
    str_value,
)
from misaka.core.tools.tool_definition_wrapper import wrap_tool_definition
from misaka.ui.tui import Container, Text
from misaka.ui.tui.interactive.theme.theme import get_language_from_path, highlight_code


class WriteToolInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str = Field(description="Path to the file to write (relative or absolute)")
    content: str = Field(description="Content to write to the file")


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
        await asyncio.to_thread(Path(absolute_path).write_text, content, encoding="utf-8", newline="")

    async def mkdir(self, directory: str) -> None:
        await asyncio.to_thread(os.makedirs, directory, exist_ok=True)


WRITE_PARTIAL_FULL_HIGHLIGHT_LINES = 50


def _coerce_options(options: WriteToolOptions | Mapping[str, Any] | None) -> WriteToolOptions:
    if options is None:
        return WriteToolOptions()
    if isinstance(options, WriteToolOptions):
        return options
    return WriteToolOptions(operations=options.get("operations"))


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _is_aborted(signal: Any | None) -> bool:
    return bool(getattr(signal, "aborted", False))


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
        while not _is_aborted(signal):
            await asyncio.sleep(0.01)

    return asyncio.create_task(_poll_abort()), lambda: None


async def _drain_worker(task: asyncio.Task[Any]) -> bool:
    """Wait through repeated caller cancellation; return whether another cancel arrived."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:  # noqa: BLE001 - draining must survive every worker outcome
            break
    if task.done():
        try:
            task.result()
        except BaseException:  # noqa: BLE001, S110 - observing the drained outcome is sufficient
            pass
    return cancelled


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
) -> str:
    from misaka.ui.tui.interactive.components.keybinding_hints import key_hint

    raw_path = str_value(_value(args, "file_path", _value(args, "path")))
    file_content = str_value(_value(args, "content"))
    path = shorten_path(raw_path) if raw_path is not None else None
    invalid_arg = invalid_arg_text(theme_obj)
    if path is None:
        path_display = invalid_arg
    elif path:
        path_display = theme_obj.fg("accent", path)
    else:
        path_display = theme_obj.fg("toolOutput", "...")
    text = f"{theme_obj.fg('toolTitle', theme_obj.bold('write'))} {path_display}"

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
                + f" {key_hint('app.tools.expand', 'to expand')})"
            )

    return text


def _format_write_result(result: Any, theme_obj: Any) -> str | None:
    if not _value(result, "isError"):
        return None
    content = _value(result, "content", [])
    if not isinstance(content, list):
        return None
    output = "\n".join((_value(block, "text") or "") for block in content if _value(block, "type") == "text")
    if not output:
        return None
    return f"\n{theme_obj.fg('error', output)}"


def create_write_tool_definition(
    cwd: str,
    options: WriteToolOptions | Mapping[str, Any] | None = None,
) -> ToolDefinition[WriteToolInput | dict[str, Any], None]:
    resolved_options = _coerce_options(options)
    operations = resolved_options.operations or _DefaultWriteOperations()

    async def execute(
        _tool_call_id: str,
        params: WriteToolInput | dict[str, Any],
        signal: Any | None = None,
        _on_update: Callable[[AgentToolResult], None] | None = None,
        _ctx: Any = None,
    ) -> AgentToolResult:
        parsed = WriteToolInput.model_validate(params)
        absolute_path = resolve_to_cwd(parsed.path, cwd)
        directory = os.path.dirname(absolute_path)

        async def mutate() -> AgentToolResult:
            if _is_aborted(signal):
                raise RuntimeError("Operation aborted")

            aborted = False

            async def worker() -> AgentToolResult | None:
                try:
                    await operations.mkdir(directory)
                    if aborted:
                        return None
                    await operations.writeFile(absolute_path, parsed.content)
                    if aborted:
                        return None
                    return AgentToolResult(
                        content=[TextContent(text=f"Successfully wrote {len(parsed.content)} bytes to {parsed.path}")],
                        details=None,
                    )
                except Exception:
                    if aborted:
                        return None
                    raise

            worker_task = asyncio.create_task(worker())
            abort_task, cleanup_abort = _create_abort_wait_task(signal)

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
            finally:
                cleanup_abort()
                if abort_task is not None and not abort_task.done():
                    abort_task.cancel()
                if abort_task is not None:
                    await asyncio.gather(abort_task, return_exceptions=True)

        return await with_file_mutation_queue(absolute_path, mutate)

    def render_call(args: Any, theme_obj: Any, context: Any) -> WriteCallRenderComponent:
        raw_path = str_value(_value(args, "file_path", _value(args, "path")))
        file_content = str_value(_value(args, "content"))
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
            )
        )
        return component

    def render_result(result: Any, _options: Any, theme_obj: Any, context: Any) -> Any:
        output = _format_write_result({"content": _value(result, "content", []), "isError": context.isError}, theme_obj)
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
        promptSnippet="Create or overwrite files",
        promptGuidelines=["Use write only for new files or complete rewrites."],
        parameters=WriteToolInput,
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
]
