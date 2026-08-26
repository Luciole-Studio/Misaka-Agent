"""Edit tool for exact and fuzzy-aware search/replace operations."""

from __future__ import annotations

import asyncio
import errno as errno_module
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.ai.types import TextContent
from misaka.core.extensions.types import ToolDefinition
from misaka.core.tools._common import (
    _drain_worker,
    _is_aborted,
    _string_arg,
    abort_race,
)
from misaka.core.tools.edit_diff import (
    Edit,
    EditDiffError,
    EditDiffResult,
    apply_edits_to_normalized_content,
    compute_edits_diff,
    detect_line_ending,
    generate_diff_string,
    generate_unified_patch,
    normalize_to_lf,
    restore_line_endings,
    strip_bom,
)
from misaka.core.tools.file_mutation_queue import with_file_mutation_queue
from misaka.core.tools.path_utils import resolve_to_cwd
from misaka.core.tools.render_utils import invalid_arg_text, shorten_path
from misaka.core.tools.tool_definition_wrapper import wrap_tool_definition
from misaka.ui.tui import Box, Container, Spacer, Text
from misaka.utils import atomic
from misaka.utils.values import read_field

type EditPreview = EditDiffResult | EditDiffError


class ReplaceEditInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    oldText: str = Field(
        description=(
            "Exact text for one targeted replacement. It must be unique in the original file "
            "and must not overlap with any other edits[].oldText in the same call."
        )
    )
    newText: str = Field(description="Replacement text for this targeted edit.")


class EditToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Path to the file to edit (relative or absolute)")
    edits: list[ReplaceEditInput] = Field(
        description=(
            "One or more targeted replacements. Each edit is matched against the original file, not incrementally. "
            "Do not include overlapping or nested edits. If two changes touch the same block or nearby lines, "
            "merge them into one edit instead."
        )
    )


@dataclass(slots=True)
class EditToolDetails:
    diff: str
    patch: str
    firstChangedLine: int | None = None


class EditOperations(Protocol):
    readFile: Callable[[str], Awaitable[bytes]]
    writeFile: Callable[[str, str], Awaitable[None]]
    access: Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class EditToolOptions:
    operations: EditOperations | None = None


type RenderableEditArgs = dict[str, Any]


class _EditToolResultLike(Protocol):
    content: list[Any]
    details: EditToolDetails | None


class _EditCallRenderComponent(Box):
    def __init__(self) -> None:
        super().__init__(1, 1, lambda text: text)
        self.preview: EditPreview | None = None
        self.previewArgsKey: str | None = None
        self.previewPending = False
        self.settledError = False


def _ensure_edit_call_render_component(component: Box) -> _EditCallRenderComponent:
    if not hasattr(component, "preview"):
        component.preview = None
    if not hasattr(component, "previewArgsKey"):
        component.previewArgsKey = None
    if not hasattr(component, "previewPending"):
        component.previewPending = False
    if not hasattr(component, "settledError"):
        component.settledError = False
    return component  # type: ignore[return-value]


@dataclass(slots=True)
class _DefaultEditOperations:
    async def readFile(self, absolute_path: str) -> bytes:
        return await asyncio.to_thread(Path(absolute_path).read_bytes)

    async def writeFile(self, absolute_path: str, content: str) -> None:
        await asyncio.to_thread(atomic.write_text, absolute_path, content)   # atomic; keeps the file's mode

    async def access(self, absolute_path: str) -> None:
        def _check() -> None:
            with open(absolute_path, "rb"):
                pass
            with open(absolute_path, "r+b"):
                pass

        await asyncio.to_thread(_check)


def _coerce_options(options: EditToolOptions | Mapping[str, Any] | None) -> EditToolOptions:
    if options is None:
        return EditToolOptions()
    if isinstance(options, EditToolOptions):
        return options
    return EditToolOptions(operations=options.get("operations"))


def prepare_edit_arguments(input_value: Any) -> Any:
    if not isinstance(input_value, dict):
        return input_value

    def _single_edit(value: Any) -> bool:
        # A single edit object (models often send this): wrap it in a one-element list (upstream #7835/ca21c1686)
        return (isinstance(value, dict) and isinstance(value.get("oldText"), str)
                and isinstance(value.get("newText"), str))

    args = input_value
    edits_value = args.get("edits")
    if isinstance(edits_value, str):
        try:
            parsed = json.loads(edits_value)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            args["edits"] = parsed
        elif _single_edit(parsed):
            args["edits"] = [parsed]
    elif _single_edit(edits_value):
        args["edits"] = [edits_value]

    return args


def _validate_edit_input(input_value: EditToolInput) -> tuple[str, list[Edit]]:
    if not input_value.edits:
        raise RuntimeError("Edit tool input is invalid. edits must contain at least one replacement.")
    return input_value.path, [Edit(oldText=edit.oldText, newText=edit.newText) for edit in input_value.edits]


def _format_access_error(error: BaseException) -> str:
    if isinstance(error, OSError) and error.errno is not None:
        code = errno_module.errorcode.get(error.errno)
        if code:
            return f"Error code: {code}"
    if isinstance(error, Exception):
        return f"Error: {error}"
    return str(error)


def _get_edit_call_render_component(state: dict[str, Any], last_component: Any) -> _EditCallRenderComponent:
    if isinstance(last_component, Box):
        component = _ensure_edit_call_render_component(last_component)
        state["callComponent"] = component
        return component
    component = state.get("callComponent")
    if isinstance(component, Box):
        return _ensure_edit_call_render_component(component)
    component = _ensure_edit_call_render_component(_EditCallRenderComponent())
    state["callComponent"] = component
    return component


def _get_renderable_preview_input(args: RenderableEditArgs | None) -> tuple[str, list[Edit]] | None:
    if not args:
        return None

    raw_path = read_field(args, "path")
    if not isinstance(raw_path, str):
        raw_path = read_field(args, "file_path")
    if not isinstance(raw_path, str) or not raw_path:
        return None

    edits_value = read_field(args, "edits")
    if isinstance(edits_value, list) and edits_value:
        edits: list[Edit] = []
        for edit in edits_value:
            old_text = read_field(edit, "oldText")
            new_text = read_field(edit, "newText")
            if not isinstance(old_text, str) or not isinstance(new_text, str):
                return None
            edits.append(Edit(oldText=old_text, newText=new_text))
        return raw_path, edits

    old_text = read_field(args, "oldText")
    new_text = read_field(args, "newText")
    if isinstance(old_text, str) and isinstance(new_text, str):
        return raw_path, [Edit(oldText=old_text, newText=new_text)]

    return None


def _preview_args_key(path: str, edits: list[Edit]) -> str:
    return json.dumps(
        {"path": path, "edits": [{"oldText": edit.oldText, "newText": edit.newText} for edit in edits]},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _format_edit_call(args: RenderableEditArgs | None, theme_obj: Any) -> str:
    invalid_arg = invalid_arg_text(theme_obj)
    raw_path = _string_arg(read_field(args, "file_path", read_field(args, "path")))
    shortened = shorten_path(raw_path) if raw_path is not None else None
    if shortened is None:
        path_display = invalid_arg
    elif shortened:
        path_display = theme_obj.fg("accent", shortened)
    else:
        path_display = theme_obj.fg("toolOutput", "...")
    return f"{theme_obj.fg('toolTitle', theme_obj.bold('edit'))} {path_display}"


def _format_edit_result(
    args: RenderableEditArgs | None,
    preview: EditPreview | None,
    result: _EditToolResultLike | Mapping[str, Any],
    theme_obj: Any,
    is_error: bool,
) -> str | None:
    from misaka.ui.tui.interactive.components.diff import render_diff

    raw_path = _string_arg(read_field(args, "file_path", read_field(args, "path")))
    preview_diff = preview.diff if isinstance(preview, EditDiffResult) else None
    preview_error = preview.error if isinstance(preview, EditDiffError) else None

    if is_error:
        error_text = "\n".join(
            read_field(block, "text") or ""
            for block in (read_field(result, "content", []) or [])
            if read_field(block, "type") == "text"
        )
        if not error_text or error_text == preview_error:
            return None
        return theme_obj.fg("error", error_text)

    details = read_field(result, "details")
    result_diff = read_field(details, "diff")
    if isinstance(result_diff, str) and result_diff != preview_diff:
        return render_diff(result_diff, {"filePath": raw_path if raw_path is not None else None})
    return None


def _get_edit_header_bg(preview: EditPreview | None, settled_error: bool, theme_obj: Any) -> Callable[[str], str]:
    if preview is not None:
        if isinstance(preview, EditDiffError):
            return lambda text: theme_obj.bg("toolErrorBg", text)
        return lambda text: theme_obj.bg("toolSuccessBg", text)
    if settled_error:
        return lambda text: theme_obj.bg("toolErrorBg", text)
    return lambda text: theme_obj.bg("toolPendingBg", text)


def _build_edit_call_component(
    component: _EditCallRenderComponent,
    args: RenderableEditArgs | None,
    theme_obj: Any,
) -> _EditCallRenderComponent:
    from misaka.ui.tui.interactive.components.diff import render_diff

    component.setBgFn(_get_edit_header_bg(component.preview, component.settledError, theme_obj))
    component.clear()
    component.addChild(Text(_format_edit_call(args, theme_obj), 0, 0))

    if component.preview is None:
        return component

    if isinstance(component.preview, EditDiffError):
        body = theme_obj.fg("error", component.preview.error)
    else:
        body = render_diff(component.preview.diff)
    component.addChild(Spacer(1))
    component.addChild(Text(body, 0, 0))
    return component


def _set_edit_preview(
    component: _EditCallRenderComponent,
    preview: EditPreview,
    args_key: str | None,
) -> bool:
    current = component.preview
    changed = (
        current is None
        or (
            isinstance(current, EditDiffError)
            and isinstance(preview, EditDiffError)
            and current.error != preview.error
        )
        or (isinstance(current, EditDiffError) != isinstance(preview, EditDiffError))
        or (
            isinstance(current, EditDiffResult)
            and isinstance(preview, EditDiffResult)
            and (current.diff != preview.diff or current.firstChangedLine != preview.firstChangedLine)
        )
    )
    component.preview = preview
    component.previewArgsKey = args_key
    component.previewPending = False
    return changed


def create_edit_tool_definition(
    cwd: str,
    options: EditToolOptions | Mapping[str, Any] | None = None,
) -> ToolDefinition[EditToolInput | dict[str, Any], EditToolDetails | None]:
    resolved_options = _coerce_options(options)
    operations = resolved_options.operations or _DefaultEditOperations()

    async def execute(
        _tool_call_id: str,
        input_value: EditToolInput | dict[str, Any],
        signal: Any | None = None,
        _on_update: Callable[[AgentToolResult], None] | None = None,
        _ctx: Any = None,
    ) -> AgentToolResult:
        parsed = EditToolInput.model_validate(input_value)
        path, edits = _validate_edit_input(parsed)
        absolute_path = resolve_to_cwd(path, cwd)

        async def mutate() -> AgentToolResult:
            if _is_aborted(signal):
                raise RuntimeError("Operation aborted")

            aborted = False

            async def worker() -> AgentToolResult | None:
                try:
                    try:
                        await operations.access(absolute_path)
                    except Exception as error:  # noqa: BLE001 - any access failure becomes the user-facing edit error
                        raise RuntimeError(f"Could not edit file: {path}. {_format_access_error(error)}.") from None

                    if aborted:
                        return None

                    buffer = await operations.readFile(absolute_path)
                    if aborted:
                        return None

                    try:
                        raw_content = buffer.decode("utf-8")
                    except UnicodeDecodeError as error:
                        raise RuntimeError(
                            f"Could not edit file: {path}. It is not valid UTF-8 text ({error}); "
                            "editing it as text would corrupt it. Use bash for binary files."
                        ) from None
                    bom, content = strip_bom(raw_content)
                    original_ending = detect_line_ending(content)
                    normalized_content = normalize_to_lf(content)
                    applied = apply_edits_to_normalized_content(normalized_content, edits, path)

                    if aborted:
                        return None

                    final_content = bom + restore_line_endings(applied.newContent, original_ending)
                    await operations.writeFile(absolute_path, final_content)
                    if aborted:
                        return None

                    diff_result = generate_diff_string(applied.baseContent, applied.newContent)
                    patch = generate_unified_patch(path, applied.baseContent, applied.newContent)
                    return AgentToolResult(
                        content=[TextContent(text=f"Successfully replaced {len(edits)} block(s) in {path}.")],
                        details=EditToolDetails(
                            diff=diff_result.diff,
                            patch=patch,
                            firstChangedLine=diff_result.firstChangedLine,
                        ),
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

    def render_call(args: RenderableEditArgs | None, theme_obj: Any, context: Any) -> _EditCallRenderComponent:
        component = _get_edit_call_render_component(context.state, context.lastComponent)
        preview_input = _get_renderable_preview_input(args)
        args_key = _preview_args_key(*preview_input) if preview_input is not None else None

        if component.previewArgsKey != args_key:
            component.preview = None
            component.previewArgsKey = args_key
            component.previewPending = False
            component.settledError = False

        if context.argsComplete and preview_input is not None and component.preview is None and not component.previewPending:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                component.previewPending = True
                request_key = args_key
                path_value, edits_value = preview_input

                async def _load_preview() -> None:
                    preview = await compute_edits_diff(path_value, edits_value, context.cwd)
                    if component.previewArgsKey == request_key:
                        _set_edit_preview(component, preview, request_key)
                        context.invalidate()

                loop.create_task(_load_preview())

        return _build_edit_call_component(component, args, theme_obj)

    def render_result(
        result: _EditToolResultLike | Mapping[str, Any],
        _options: Mapping[str, Any],
        theme_obj: Any,
        context: Any,
    ) -> Container:
        raw_call_component = context.state.get("callComponent")
        call_component = _ensure_edit_call_render_component(raw_call_component) if isinstance(raw_call_component, Box) else None
        preview_input = _get_renderable_preview_input(context.args)
        args_key = _preview_args_key(*preview_input) if preview_input is not None else None
        result_diff = read_field(read_field(result, "details"), "diff") if not context.isError else None

        changed = False
        if call_component is not None:
            if isinstance(result_diff, str):
                changed = _set_edit_preview(
                    call_component,
                    EditDiffResult(diff=result_diff, firstChangedLine=read_field(read_field(result, "details"), "firstChangedLine")),
                    args_key,
                ) or changed
            if call_component.settledError != context.isError:
                call_component.settledError = context.isError
                changed = True
            if changed:
                _build_edit_call_component(call_component, context.args, theme_obj)

        output = _format_edit_result(
            context.args,
            call_component.preview if call_component is not None else None,
            result,
            theme_obj,
            context.isError,
        )
        component = context.lastComponent if context.lastComponent is not None else Container()
        component.clear()
        if not output:
            return component
        component.addChild(Spacer(1))
        component.addChild(Text(output, 1, 0))
        return component

    return ToolDefinition(
        name="edit",
        label="edit",
        description=(
            "Edit a single file using exact text replacement. Every edits[].oldText must match a unique, "
            "non-overlapping region of the original file. If two changes affect the same block or nearby lines, "
            "merge them into one edit instead of emitting overlapping edits. Do not include large unchanged regions "
            "just to connect distant changes."
        ),
        promptSnippet=(
            "Make precise file edits with exact text replacement, including multiple disjoint edits in one call"
        ),
        promptGuidelines=[
            "Use edit for precise changes (edits[].oldText must match exactly)",
            "When changing multiple separate locations in one file, use one edit call with multiple entries in edits[] instead of multiple edit calls",
            "Each edits[].oldText is matched against the original file, not after earlier edits are applied. Do not emit overlapping or nested edits. Merge nearby changes into one edit.",
            "Keep edits[].oldText as small as possible while still being unique in the file. Do not pad with large unchanged regions.",
        ],
        parameters=EditToolInput,
        renderShell="self",
        prepareArguments=prepare_edit_arguments,
        execute=execute,
        renderCall=render_call,
        renderResult=render_result,
    )


def create_edit_tool(cwd: str, options: EditToolOptions | Mapping[str, Any] | None = None) -> AgentTool:
    return wrap_tool_definition(create_edit_tool_definition(cwd, options))


createEditTool = create_edit_tool
createEditToolDefinition = create_edit_tool_definition

__all__ = [
    "EditOperations",
    "EditToolDetails",
    "EditToolInput",
    "EditToolOptions",
    "createEditTool",
    "createEditToolDefinition",
]
