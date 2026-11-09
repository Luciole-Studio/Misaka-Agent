"""Translated from pi's ``core/tools/renderers/edit.ts`` (v1.1.0).

Presentation for the edit tool.

Renderers live apart from the implementation so a process that only displays tool output does not
load the execution path or its typebox parameter schema. `edit.ts` spreads these into its
definition, so the tool's public shape is unchanged.

MISAKA forks: the preview is computed only while an event loop runs (``computeEditsDiff`` is a coroutine); a ``Box``
handed back as ``lastComponent`` that is not an ``EditCallRenderComponent`` gets the component's fields
(``Object.assign`` upstream gives them to every new component, and a missing field reads as ``undefined``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypedDict, cast

from misaka.core.extensions.types import (
    ToolRenderContext,
    ToolRenderers,
    ToolRenderResultOptions,
)
from misaka.core.tools import render_utils
from misaka.core.tools.edit_diff import EditDiffError, EditDiffResult, computeEditsDiff
from misaka.core.tools.render_utils import argOf, renderToolPath
from misaka.modes.interactive.components.diff import RenderDiffOptions, renderDiff
from misaka.ui.tui import Box, Container, Spacer, Text
from misaka.utils.js import UNDEFINED, jsonStringify
from misaka.utils.values import read_field

type EditPreview = EditDiffResult | EditDiffError


class EditCallRenderComponent(Box):
    def __init__(self) -> None:
        super().__init__(1, 1, lambda text: text)
        self.preview: EditPreview | None = None
        self.previewArgsKey: str | None = None
        self.previewPending = False
        self.settledError = False


class EditRenderState(TypedDict, total=False):
    callComponent: EditCallRenderComponent


def createEditCallRenderComponent() -> EditCallRenderComponent:
    return EditCallRenderComponent()


def _asEditCallRenderComponent(component: Box) -> EditCallRenderComponent:
    """MISAKA fork: see the module docstring."""
    for name, default in (("preview", None), ("previewArgsKey", None), ("previewPending", False), ("settledError", False)):
        if not hasattr(component, name):
            setattr(component, name, default)
    return cast(EditCallRenderComponent, component)


def getEditCallRenderComponent(state: dict[str, Any], lastComponent: Any) -> EditCallRenderComponent:
    if isinstance(lastComponent, Box):
        component = _asEditCallRenderComponent(lastComponent)
        state["callComponent"] = component
        return component
    if state.get("callComponent"):
        return cast(EditCallRenderComponent, state["callComponent"])
    component = createEditCallRenderComponent()
    state["callComponent"] = component
    return component


class _PreviewInput(TypedDict):
    path: str
    edits: list[Any]


def getRenderablePreviewInput(args: Any) -> _PreviewInput | None:
    if not args:
        return None

    pathArg = argOf(args, "path")
    filePathArg = argOf(args, "file_path")
    path = pathArg if isinstance(pathArg, str) else filePathArg if isinstance(filePathArg, str) else None
    if not path:
        return None

    edits = argOf(args, "edits")
    if (
        isinstance(edits, list)
        and len(edits) > 0
        and all(isinstance(read_field(edit, "oldText"), str) and isinstance(read_field(edit, "newText"), str) for edit in edits)
    ):
        return {"path": path, "edits": edits}

    oldText = argOf(args, "oldText")
    newText = argOf(args, "newText")
    if isinstance(oldText, str) and isinstance(newText, str):
        return {"path": path, "edits": [{"oldText": oldText, "newText": newText}]}

    return None


def _rawPathOf(args: Any) -> str | None:
    """``str(args?.file_path ?? args?.path)``"""
    filePath = argOf(args, "file_path")
    return render_utils.str(filePath if filePath is not None and filePath is not UNDEFINED else argOf(args, "path"))


def formatEditCall(args: Any, theme: Any, cwd: str) -> str:
    pathDisplay = renderToolPath(_rawPathOf(args), theme, cwd)
    return f"{theme.fg('toolTitle', theme.bold('edit'))} {pathDisplay}"


def formatEditResult(
    args: Any,
    preview: EditPreview | None,
    result: Any,
    theme: Any,
    isError: bool,
) -> str | None:
    rawPath = _rawPathOf(args)
    previewDiff = preview.diff if isinstance(preview, EditDiffResult) else None
    previewError = preview.error if isinstance(preview, EditDiffError) else None
    if isError:
        errorText = "\n".join(
            read_field(c, "text") or "" for c in read_field(result, "content") or [] if read_field(c, "type") == "text"
        )
        if not errorText or errorText == previewError:
            return None
        return theme.fg("error", errorText)

    resultDiff = read_field(read_field(result, "details"), "diff")
    if resultDiff and resultDiff != previewDiff:
        return renderDiff(resultDiff, RenderDiffOptions(filePath=rawPath if rawPath is not None else None))

    return None


def getEditHeaderBg(preview: EditPreview | None, settledError: bool | None, theme: Any) -> Callable[[str], str]:
    if preview:
        if isinstance(preview, EditDiffError):
            return lambda text: theme.bg("toolErrorBg", text)
        return lambda text: theme.bg("toolSuccessBg", text)
    if settledError:
        return lambda text: theme.bg("toolErrorBg", text)
    return lambda text: theme.bg("toolPendingBg", text)


def buildEditCallComponent(
    component: EditCallRenderComponent,
    args: Any,
    theme: Any,
    cwd: str,
    outputPad: int,
) -> EditCallRenderComponent:
    component.setBgFn(getEditHeaderBg(component.preview, component.settledError, theme))
    component.setPaddingX(outputPad)
    component.clear()
    component.addChild(Text(formatEditCall(args, theme, cwd), 0, 0))

    if not component.preview:
        return component

    body = (
        theme.fg("error", component.preview.error)
        if isinstance(component.preview, EditDiffError)
        else renderDiff(component.preview.diff)
    )
    component.addChild(Spacer(1))
    component.addChild(Text(body, 0, 0))
    return component


def setEditPreview(component: EditCallRenderComponent, preview: EditPreview, argsKey: str | None) -> bool:
    current = component.preview
    changed = (
        current is None
        or (
            current.error != preview.error
            if isinstance(current, EditDiffError) and isinstance(preview, EditDiffError)
            else isinstance(current, EditDiffError) != isinstance(preview, EditDiffError)
        )
        or (
            isinstance(current, EditDiffResult)
            and isinstance(preview, EditDiffResult)
            and (current.diff != preview.diff or current.firstChangedLine != preview.firstChangedLine)
        )
    )
    component.preview = preview
    component.previewArgsKey = argsKey
    component.previewPending = False
    return changed


def _argsKey(previewInput: _PreviewInput | None) -> str | None:
    return (
        jsonStringify({"path": previewInput["path"], "edits": previewInput["edits"]}) if previewInput is not None else None
    )


def renderCall(args: Any, theme: Any, context: ToolRenderContext) -> EditCallRenderComponent:
    component = getEditCallRenderComponent(context.state, context.lastComponent)
    previewInput = getRenderablePreviewInput(args)
    argsKey = _argsKey(previewInput)

    if component.previewArgsKey != argsKey:
        component.preview = None
        component.previewArgsKey = argsKey
        component.previewPending = False
        component.settledError = False

    if context.argsComplete and previewInput is not None and not component.preview and not component.previewPending:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            component.previewPending = True
            requestKey = argsKey
            path, edits = previewInput["path"], previewInput["edits"]

            async def loadPreview() -> None:
                preview = await computeEditsDiff(path, edits, context.cwd)
                if component.previewArgsKey == requestKey:
                    setEditPreview(component, preview, requestKey)
                    context.invalidate()

            loop.create_task(loadPreview())

    return buildEditCallComponent(component, args, theme, context.cwd, context.outputPad)


def renderResult(result: Any, _options: ToolRenderResultOptions, theme: Any, context: ToolRenderContext) -> Container:
    callComponent: EditCallRenderComponent | None = context.state.get("callComponent")
    previewInput = getRenderablePreviewInput(context.args)
    argsKey = _argsKey(previewInput)
    typedResult = result
    resultDiff = read_field(read_field(typedResult, "details"), "diff") if not context.isError else None
    changed = False
    if callComponent:
        if isinstance(resultDiff, str):
            changed = (
                setEditPreview(
                    callComponent,
                    EditDiffResult(
                        diff=resultDiff, firstChangedLine=read_field(read_field(typedResult, "details"), "firstChangedLine")
                    ),
                    argsKey,
                )
                or changed
            )
        if callComponent.settledError != context.isError:
            callComponent.settledError = context.isError
            changed = True
        if changed:
            buildEditCallComponent(callComponent, context.args, theme, context.cwd, context.outputPad)

    output = formatEditResult(
        context.args,
        callComponent.preview if callComponent else None,
        typedResult,
        theme,
        context.isError,
    )
    component = cast(Container, context.lastComponent) if context.lastComponent is not None else Container()
    component.clear()
    if not output:
        return component
    component.addChild(Spacer(1))
    component.addChild(Text(output, context.outputPad, 0))
    return component


editRenderers: ToolRenderers = {"renderCall": renderCall, "renderResult": renderResult}
