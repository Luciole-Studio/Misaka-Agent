"""Interactive renderer for tool calls and tool results."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

from misaka.core.tools import create_all_tool_definitions
from misaka.core.tools.render_utils import get_text_output
from misaka.ui.tui import (
    Box,
    Container,
    Image,
    ImageOptions,
    ImageTheme,
    Spacer,
    Text,
    getCapabilities,
)
from misaka.ui.tui.interactive.theme.theme import theme
from misaka.utils.image_convert import convert_to_png
from misaka.utils.values import read_field


@dataclass(slots=True)
class ToolExecutionOptions:
    showImages: bool = True
    imageWidthCells: int = 60


@dataclass(slots=True)
class ToolRenderContext:
    args: Any
    toolCallId: str
    invalidate: Any
    lastComponent: Any | None
    state: dict[str, Any]
    cwd: str
    executionStarted: bool
    argsComplete: bool
    isPartial: bool
    expanded: bool
    showImages: bool
    isError: bool


@dataclass(slots=True)
class _ToolResultPayload:
    content: list[Any]
    details: Any = None
    isError: bool = False


class ToolExecutionComponent(Container):
    def __init__(
        self,
        toolName: str,
        toolCallId: str,
        args: Any,
        options: ToolExecutionOptions | dict[str, Any] | None = None,
        toolDefinition: Any | None = None,
        ui: Any | None = None,
        cwd: str | None = None,
    ) -> None:
        super().__init__()
        resolved_options = (
            options if isinstance(options, ToolExecutionOptions) else ToolExecutionOptions(**(options or {}))
        )
        self.toolName = toolName
        self.toolCallId = toolCallId
        self.args = args
        self.toolDefinition = toolDefinition
        self.showImages = bool(resolved_options.showImages)
        self.imageWidthCells = max(1, int(resolved_options.imageWidthCells))
        self.ui = ui
        self.cwd = cwd or os.getcwd()
        self.expanded = False
        self.isPartial = True
        self.executionStarted = False
        self.argsComplete = False
        self.result: _ToolResultPayload | None = None
        self.rendererState: dict[str, Any] = {}
        self.callRendererComponent: Any | None = None
        self.resultRendererComponent: Any | None = None
        self.hideComponent = False
        self.imageComponents: list[Image] = []
        self.imageSpacers: list[Spacer] = []
        self._converted_images: dict[int, tuple[str, str]] = {}
        self._image_conversion_tasks: dict[int, asyncio.Task[Any]] = {}

        builtin_definitions = create_all_tool_definitions(self.cwd)
        self.builtInToolDefinition = builtin_definitions.get(toolName)

        self.addChild(Spacer(1))
        self.contentBox = Box(1, 1, lambda text: theme.bg("toolPendingBg", text))
        self.contentText = Text("", 1, 1, lambda text: theme.bg("toolPendingBg", text))
        self.selfRenderContainer = Container()
        if self.hasRendererDefinition():
            self.addChild(self.selfRenderContainer if self.getRenderShell() == "self" else self.contentBox)
        else:
            self.addChild(self.contentText)
        self.updateDisplay()

    def _request_render(self) -> None:
        request_render = getattr(self.ui, "requestRender", None)
        if callable(request_render):
            request_render()

    def _get_renderer(self, name: str) -> Any | None:
        builtin = getattr(self.builtInToolDefinition, name, None) if self.builtInToolDefinition is not None else None
        custom = getattr(self.toolDefinition, name, None) if self.toolDefinition is not None else None
        if self.builtInToolDefinition is None:
            return custom
        if self.toolDefinition is None:
            return builtin
        return custom if custom is not None else builtin

    def getCallRenderer(self) -> Any | None:
        return self._get_renderer("renderCall")

    def getResultRenderer(self) -> Any | None:
        return self._get_renderer("renderResult")

    def hasRendererDefinition(self) -> bool:
        return self.builtInToolDefinition is not None or self.toolDefinition is not None

    def getRenderShell(self) -> str:
        builtin = (
            getattr(self.builtInToolDefinition, "renderShell", None)
            if self.builtInToolDefinition is not None
            else None
        )
        custom = getattr(self.toolDefinition, "renderShell", None) if self.toolDefinition is not None else None
        if self.builtInToolDefinition is None:
            return custom or "default"
        if self.toolDefinition is None:
            return builtin or "default"
        return custom or builtin or "default"

    def getRenderContext(self, lastComponent: Any | None) -> ToolRenderContext:
        return ToolRenderContext(
            args=self.args,
            toolCallId=self.toolCallId,
            invalidate=lambda: (self.invalidate(), self._request_render()),
            lastComponent=lastComponent,
            state=self.rendererState,
            cwd=self.cwd,
            executionStarted=self.executionStarted,
            argsComplete=self.argsComplete,
            isPartial=self.isPartial,
            expanded=self.expanded,
            showImages=self.showImages,
            isError=bool(self.result.isError if self.result is not None else False),
        )

    def _format_args(self) -> str:
        return json.dumps(self.args, indent=2, ensure_ascii=False)

    def createCallFallback(self) -> Text:
        return Text(theme.fg("toolTitle", theme.bold(self.toolName)), 0, 0)

    def createResultFallback(self) -> Text | None:
        output = self.getTextOutput()
        if not output:
            return None
        return Text(theme.fg("toolOutput", output), 0, 0)

    def updateArgs(self, args: Any) -> None:
        self.args = args
        self.updateDisplay()

    def markExecutionStarted(self) -> None:
        self.executionStarted = True
        self.updateDisplay()
        self._request_render()

    def setArgsComplete(self) -> None:
        self.argsComplete = True
        self.updateDisplay()
        self._request_render()

    def updateResult(self, result: dict[str, Any] | _ToolResultPayload, isPartial: bool = False) -> None:
        if isinstance(result, _ToolResultPayload):
            self.result = result
        else:
            self.result = _ToolResultPayload(
                content=list(read_field(result, "content", []) or []),
                details=read_field(result, "details"),
                isError=bool(read_field(result, "isError", False)),
            )
        self.isPartial = isPartial
        self.updateDisplay()
        self._maybe_convert_images_for_kitty()

    def setExpanded(self, expanded: bool) -> None:
        self.expanded = expanded
        self.updateDisplay()

    def setShowImages(self, show: bool) -> None:
        self.showImages = bool(show)
        self.updateDisplay()

    def setImageWidthCells(self, width: int) -> None:
        self.imageWidthCells = max(1, int(width))
        self.updateDisplay()

    def invalidate(self) -> None:
        super().invalidate()
        self.updateDisplay()

    def render(self, width: int) -> list[str]:
        if self.hideComponent:
            return []
        return super().render(width)

    def _clear_dynamic_media(self) -> None:
        for component in self.imageComponents:
            self.removeChild(component)
        for spacer in self.imageSpacers:
            self.removeChild(spacer)
        self.imageComponents = []
        self.imageSpacers = []

    def _maybe_convert_images_for_kitty(self) -> None:
        caps = getCapabilities()
        if getattr(caps, "images", None) != "kitty":
            return
        if self.result is None:
            return

        image_blocks = [
            block
            for block in self.result.content
            if read_field(block, "type") == "image"
        ]
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        for index, block in enumerate(image_blocks):
            data = read_field(block, "data")
            mime_type = read_field(block, "mimeType")
            if not isinstance(data, str) or not isinstance(mime_type, str):
                continue
            if mime_type == "image/png":
                continue
            if index in self._converted_images or index in self._image_conversion_tasks:
                continue

            async def convert_image(image_index: int = index, image_data: str = data, image_mime: str = mime_type) -> None:
                try:
                    converted = await convert_to_png(image_data, image_mime)
                    if converted is not None:
                        self._converted_images[image_index] = (converted.data, converted.mimeType)
                        self.updateDisplay()
                        self._request_render()
                finally:
                    self._image_conversion_tasks.pop(image_index, None)

            self._image_conversion_tasks[index] = loop.create_task(convert_image())

    def updateDisplay(self) -> None:
        def pending_bg(text: str) -> str:
            return theme.bg("toolPendingBg", text)

        def error_bg(text: str) -> str:
            return theme.bg("toolErrorBg", text)

        def success_bg(text: str) -> str:
            return theme.bg("toolSuccessBg", text)

        if self.isPartial:
            bg_fn = pending_bg
        elif self.result is not None and self.result.isError:
            bg_fn = error_bg
        else:
            bg_fn = success_bg

        has_content = False
        self.hideComponent = False

        if self.hasRendererDefinition():
            render_container = self.selfRenderContainer if self.getRenderShell() == "self" else self.contentBox
            if isinstance(render_container, Box):
                render_container.setBgFn(bg_fn)
            render_container.clear()

            call_renderer = self.getCallRenderer()
            if not callable(call_renderer):
                render_container.addChild(self.createCallFallback())
                has_content = True
            else:
                try:
                    component = call_renderer(self.args, theme, self.getRenderContext(self.callRendererComponent))
                except Exception:  # noqa: BLE001 - a failing tool renderer falls back to the default
                    self.callRendererComponent = None
                    component = None
                if component is None:
                    render_container.addChild(self.createCallFallback())
                else:
                    self.callRendererComponent = component
                    render_container.addChild(component)
                has_content = True

            if self.result is not None:
                result_renderer = self.getResultRenderer()
                if not callable(result_renderer):
                    component = self.createResultFallback()
                    if component is not None:
                        render_container.addChild(component)
                        has_content = True
                else:
                    try:
                        component = result_renderer(
                            {
                                "content": self.result.content,
                                "details": self.result.details,
                            },
                            {"expanded": self.expanded, "isPartial": self.isPartial},
                            theme,
                            self.getRenderContext(self.resultRendererComponent),
                        )
                    except Exception:  # noqa: BLE001 - a failing tool renderer falls back to the default
                        self.resultRendererComponent = None
                        component = None
                    if component is None:
                        fallback = self.createResultFallback()
                        if fallback is not None:
                            render_container.addChild(fallback)
                            has_content = True
                    else:
                        self.resultRendererComponent = component
                        render_container.addChild(component)
                        has_content = True
        else:
            self.contentText.setCustomBgFn(bg_fn)
            self.contentText.setText(self.formatToolExecution())
            has_content = True

        self._clear_dynamic_media()
        if self.result is not None:
            caps = getCapabilities()
            image_index = 0
            for block in self.result.content:
                if read_field(block, "type") != "image":
                    continue
                data = read_field(block, "data")
                mime_type = read_field(block, "mimeType")
                converted = self._converted_images.get(image_index)
                if converted is not None:
                    data, mime_type = converted
                if not (caps.images and self.showImages and isinstance(data, str) and isinstance(mime_type, str)):
                    image_index += 1
                    continue
                if caps.images == "kitty" and mime_type != "image/png":
                    image_index += 1
                    continue
                spacer = Spacer(1)
                image = Image(
                    data,
                    mime_type,
                    ImageTheme(fallbackColor=lambda text: theme.fg("toolOutput", text)),
                    ImageOptions(maxWidthCells=self.imageWidthCells),
                )
                self.addChild(spacer)
                self.addChild(image)
                self.imageSpacers.append(spacer)
                self.imageComponents.append(image)
                image_index += 1

        if self.hasRendererDefinition() and not has_content and not self.imageComponents:
            self.hideComponent = True

    def getTextOutput(self) -> str:
        if self.result is None:
            return ""
        return get_text_output(self.result, self.showImages)

    def formatToolExecution(self) -> str:
        text = theme.fg("toolTitle", theme.bold(self.toolName))
        args_text = self._format_args()
        if args_text:
            text += f"\n\n{args_text}"
        output = self.getTextOutput()
        if output:
            text += f"\n{output}"
        return text


__all__ = [
    "ToolExecutionComponent",
    "ToolExecutionOptions",
]
