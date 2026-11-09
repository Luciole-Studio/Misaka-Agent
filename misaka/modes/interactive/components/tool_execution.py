"""Translated from pi's ``modes/interactive/components/tool-execution.ts`` (v1.1.0).

MISAKA forks, each marked where it happens:

- D1: no ``MouseRegion`` around results and no ``handleMouse``; only the fullscreen renderer delivers clicks.
- A renderer that returns ``None`` gets the default fallback, as one that raises does.
- ``ui`` may be missing (read-only transcripts); a render request then does nothing.
- The render context is the ``ToolRenderContext`` dataclass below (upstream: an object literal), and a tool's
  renderers and result are read with ``read_field`` (``ToolDefinition`` dataclasses, ``ToolRenderers`` dicts, models).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from misaka.core.extensions.types import ToolRenderers
from misaka.core.tools.render_utils import formatToolCallWithArgs
from misaka.core.tools.render_utils import getTextOutput as getRenderedTextOutput
from misaka.modes.interactive.theme.theme import theme
from misaka.ui.tui import (
    Box,
    Component,
    Container,
    Image,
    ImageOptions,
    ImageTheme,
    Spacer,
    Text,
    getCapabilities,
)
from misaka.utils.image_convert import ensurePngTranscoder
from misaka.utils.js import jsonStringify
from misaka.utils.values import read_field

from .keybinding_hints import keyHint

# What this component needs from a tool: how to draw it, without executing it.
__all__ = ["ToolExecutionComponent", "ToolExecutionOptions", "ToolRenderContext", "ToolRenderers"]

FALLBACK_PREVIEW_LINES = 10


class ToolExecutionOptions(TypedDict, total=False):
    showImages: bool
    imageWidthCells: int
    outputPad: int


@dataclass(slots=True)
class ToolRenderContext:
    """MISAKA fork: the object ``getRenderContext`` builds (the type is the ``ToolRenderContext`` protocol of
    ``misaka.core.extensions.types``)."""

    args: Any
    toolCallId: str
    invalidate: Callable[[], None]
    lastComponent: Component | None
    state: Any
    cwd: str
    executionStarted: bool
    argsComplete: bool
    isPartial: bool
    expanded: bool
    showImages: bool
    isError: bool
    durationMs: int | None
    outputPad: int


@dataclass(slots=True)
class _ToolResult:
    """MISAKA fork: a name for the inline type of ``result``."""

    content: list[Any]
    isError: bool
    details: Any = None
    durationMs: int | None = None


@dataclass(slots=True)
class _ImageSource:
    data: str
    mimeType: str
    widthCells: int


class ToolExecutionComponent(Container):
    def __init__(
        self,
        toolName: str,
        toolCallId: str,
        args: Any,
        options: ToolExecutionOptions | None,
        toolDefinition: Any | None,
        ui: Any | None,
        cwd: str,
    ) -> None:
        super().__init__()
        if options is None:
            options = {}
        self.selfRenderContainer: Container
        self.callRendererComponent: Component | None = None
        self.resultRendererComponent: Component | None = None
        self.rendererState: dict[str, Any] = {}
        self.imageComponents: list[Image] = []
        # Inputs of imageComponents, so updateDisplay can reuse images and keep their converted PNG data.
        self.imageSources: list[_ImageSource] = []
        self.imageSpacers: list[Spacer] = []
        self.expanded = False
        self.isPartial = True
        self.executionStarted = False
        self.argsComplete = False
        self.result: _ToolResult | None = None
        self.hideComponent = False
        self.toolName = toolName
        self.toolCallId = toolCallId
        self.args = args
        self.toolDefinition = toolDefinition
        showImages = options.get("showImages")
        self.showImages: bool = True if showImages is None else showImages
        imageWidthCells = options.get("imageWidthCells")
        self.imageWidthCells: int = 60 if imageWidthCells is None else imageWidthCells
        outputPad = options.get("outputPad")
        self.outputPad: int = 1 if outputPad is None else outputPad
        self.ui = ui
        self.cwd = cwd

        self.addChild(Spacer(1))

        # Always create all shell variants. contentBox is used for default renderer-based composition.
        # selfRenderContainer is used when the tool renders its own framing.
        # contentText is reserved for generic fallback rendering when no tool definition exists.
        self.contentBox = Box(1, 1, lambda text: theme.bg("toolPendingBg", text))
        self.contentText = Text("", 1, 1, lambda text: theme.bg("toolPendingBg", text))
        self.selfRenderContainer = Container()

        if self.hasRendererDefinition():
            self.addChild(self.selfRenderContainer if self.getRenderShell() == "self" else self.contentBox)
        else:
            # MISAKA fork (D1): no MouseRegion around the text.
            self.addChild(self.contentText)

        self.updateDisplay()

    def _requestRender(self) -> None:
        """MISAKA fork: ``this.ui.requestRender()`` when there is a UI."""
        if self.ui is not None:
            self.ui.requestRender()

    def getCallRenderer(self) -> Any | None:
        return read_field(self.toolDefinition, "renderCall") if self.toolDefinition is not None else None

    def getResultRenderer(self) -> Any | None:
        return read_field(self.toolDefinition, "renderResult") if self.toolDefinition is not None else None

    def hasRendererDefinition(self) -> bool:
        return self.toolDefinition is not None

    def getRenderShell(self) -> str:
        renderShell = read_field(self.toolDefinition, "renderShell") if self.toolDefinition is not None else None
        return renderShell if renderShell is not None else "default"

    def getRenderContext(self, lastComponent: Component | None) -> ToolRenderContext:
        def invalidate() -> None:
            self.invalidate()
            self._requestRender()

        return ToolRenderContext(
            args=self.args,
            toolCallId=self.toolCallId,
            invalidate=invalidate,
            lastComponent=lastComponent,
            state=self.rendererState,
            cwd=self.cwd,
            executionStarted=self.executionStarted,
            argsComplete=self.argsComplete,
            isPartial=self.isPartial,
            expanded=self.expanded,
            showImages=self.showImages,
            isError=self.result.isError if self.result is not None else False,
            durationMs=None if self.isPartial else self.result.durationMs if self.result is not None else None,
            outputPad=self.outputPad,
        )

    def createCallFallback(self) -> Component:
        return Text(formatToolCallWithArgs(self.toolName, self.args, theme, self.expanded), 0, 0)

    def createResultFallback(self) -> Component | None:
        output = self.getTextOutput()
        if not output:
            return None

        lines = output.split("\n")
        displayLines = lines if self.expanded else lines[0:FALLBACK_PREVIEW_LINES]
        remaining = len(lines) - len(displayLines)
        text = "\n".join(theme.fg("toolOutput", line) for line in displayLines)
        if remaining > 0:
            text += (
                f"{theme.fg('muted', f'\n... ({remaining} more lines,')} "
                f"{keyHint('app.tools.expand', 'to expand')}{theme.fg('muted', ')')}"
            )
        return Text(text, 0, 0)

    def updateArgs(self, args: Any) -> None:
        self.args = args
        self.updateDisplay()

    def markExecutionStarted(self) -> None:
        self.executionStarted = True
        self.updateDisplay()
        self._requestRender()

    def setArgsComplete(self) -> None:
        self.argsComplete = True
        self.updateDisplay()
        self._requestRender()

    def updateResult(self, result: Any, isPartial: bool = False) -> None:
        """``result.durationMs`` is the execution time of a final result."""
        self.result = _ToolResult(
            content=list(read_field(result, "content") or []),
            isError=bool(read_field(result, "isError")),
            details=read_field(result, "details"),
            durationMs=read_field(result, "durationMs"),
        )
        self.isPartial = isPartial
        self.updateDisplay()

    def setExpanded(self, expanded: bool) -> None:
        self.expanded = expanded
        self.updateDisplay()

    def setOutputPad(self, outputPad: int) -> None:
        self.outputPad = outputPad
        self.updateDisplay()

    def setShowImages(self, show: bool) -> None:
        self.showImages = show
        self.updateDisplay()

    def setImageWidthCells(self, width: int) -> None:
        self.imageWidthCells = max(1, int(width // 1))
        self.updateDisplay()

    def invalidate(self) -> None:
        super().invalidate()
        self.updateDisplay()

    def render(self, width: int) -> list[str]:
        if self.hideComponent:
            return []

        if self.hasRendererDefinition() and self.getRenderShell() == "self":
            contentLines = self.selfRenderContainer.render(width)
            if len(contentLines) == 0 and len(self.imageComponents) == 0:
                return []

            lines: list[str] = []
            if len(contentLines) > 0:
                lines.append("")
                lines.extend(contentLines)
            for i in range(len(self.imageComponents)):
                spacer = self.imageSpacers[i] if i < len(self.imageSpacers) else None
                if spacer:
                    lines.extend(spacer.render(width))
                imageComponent = self.imageComponents[i]
                if imageComponent:
                    lines.extend(imageComponent.render(width))
            return lines

        return super().render(width)

    def _addRendered(self, renderContainer: Container | Box, component: Component | None, fallback: bool) -> bool:
        """MISAKA fork: add a renderer's component, or the fallback when it returned ``None``."""
        if component is None:
            component = self.createCallFallback() if fallback else self.createResultFallback()
            if component is None:
                return False
        renderContainer.addChild(component)
        return True

    def updateDisplay(self) -> None:
        if self.isPartial:

            def bgFn(text: str) -> str:
                return theme.bg("toolPendingBg", text)

        elif self.result is not None and self.result.isError:

            def bgFn(text: str) -> str:
                return theme.bg("toolErrorBg", text)

        else:

            def bgFn(text: str) -> str:
                return theme.bg("toolSuccessBg", text)

        hasContent = False
        self.hideComponent = False
        if self.hasRendererDefinition():
            renderContainer = self.selfRenderContainer if self.getRenderShell() == "self" else self.contentBox
            if isinstance(renderContainer, Box):
                renderContainer.setBgFn(bgFn)
                renderContainer.setPaddingX(self.outputPad)
            renderContainer.clear()

            callRenderer = self.getCallRenderer()
            if not callRenderer:
                renderContainer.addChild(self.createCallFallback())
                hasContent = True
            else:
                try:
                    component = callRenderer(self.args, theme, self.getRenderContext(self.callRendererComponent))
                    self.callRendererComponent = component
                    hasContent = self._addRendered(renderContainer, component, True) or hasContent
                except Exception:  # noqa: BLE001 - a failing renderer falls back to the default
                    self.callRendererComponent = None
                    renderContainer.addChild(self.createCallFallback())
                    hasContent = True

            if self.result:
                resultRenderer = self.getResultRenderer()
                if not resultRenderer:
                    fallback = self.createResultFallback()
                    if fallback:
                        renderContainer.addChild(fallback)
                        hasContent = True
                else:
                    try:
                        component = resultRenderer(
                            {"content": self.result.content, "details": self.result.details},
                            {"expanded": self.expanded, "isPartial": self.isPartial},
                            theme,
                            self.getRenderContext(self.resultRendererComponent),
                        )
                        self.resultRendererComponent = component
                        hasContent = self._addRendered(renderContainer, component, False) or hasContent
                    except Exception:  # noqa: BLE001 - a failing renderer falls back to the default
                        self.resultRendererComponent = None
                        fallback = self.createResultFallback()
                        if fallback:
                            renderContainer.addChild(fallback)
                            hasContent = True
        else:
            self.contentText.setCustomBgFn(bgFn)
            self.contentText.setPaddingX(self.outputPad)
            self.contentText.setText(self.formatToolExecution())
            hasContent = True

        previousImages = self.imageComponents
        previousSources = self.imageSources
        for img in self.imageComponents:
            self.removeChild(img)
        self.imageComponents = []
        self.imageSources = []
        for spacer in self.imageSpacers:
            self.removeChild(spacer)
        self.imageSpacers = []

        if self.result:
            imageBlocks = [c for c in self.result.content if read_field(c, "type") == "image"]
            caps = getCapabilities()
            for img in imageBlocks:
                data = read_field(img, "data")
                mimeType = read_field(img, "mimeType")
                if caps.images and self.showImages and data and mimeType:
                    spacer = Spacer(1)
                    self.addChild(spacer)
                    self.imageSpacers.append(spacer)
                    source = _ImageSource(data=data, mimeType=mimeType, widthCells=self.imageWidthCells)
                    index = len(self.imageComponents)
                    previous = previousSources[index] if index < len(previousSources) else None
                    imageComponent = (
                        previousImages[index]
                        if previous is not None
                        and previous.data == source.data
                        and previous.mimeType == source.mimeType
                        and previous.widthCells == source.widthCells
                        else Image(
                            source.data,
                            source.mimeType,
                            ImageTheme(fallbackColor=lambda s: theme.fg("toolOutput", s)),
                            ImageOptions(maxWidthCells=source.widthCells),
                        )
                    )
                    if source.mimeType != "image/png":

                        def onRegistered() -> None:
                            self.invalidate()
                            self._requestRender()

                        ensurePngTranscoder(onRegistered)
                    self.imageComponents.append(imageComponent)
                    self.imageSources.append(source)
                    self.addChild(imageComponent)

        if self.hasRendererDefinition() and not hasContent and len(self.imageComponents) == 0:
            self.hideComponent = True

    def getTextOutput(self) -> str:
        return getRenderedTextOutput(self.result, self.showImages)

    def formatToolExecution(self) -> str:
        text = theme.fg("toolTitle", theme.bold(self.toolName))
        content = jsonStringify(self.args, 2)
        if content:
            text += f"\n\n{content}"
        output = self.getTextOutput()
        if output:
            text += f"\n{output}"
        return text
