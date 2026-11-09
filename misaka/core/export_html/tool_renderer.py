"""Tool HTML renderer for custom tools in HTML export.

Renders custom tool calls and results to HTML by invoking their TUI renderers
and converting the ANSI output to HTML.

Translated from pi's ``packages/coding-agent/src/core/export-html/tool-renderer.ts`` (v1.1.0).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from misaka.core.export_html.ansi_to_html import ansiLinesToHtml
from misaka.utils.js import jsTrim
from misaka.utils.values import read_field


class _Component(Protocol):
    def render(self, width: int) -> list[str]: ...


@dataclass(slots=True)
class ToolHtmlRendererDeps:
    # Renderers of calls to a tool, as resolved by extensions and the registered tool
    getToolRenderers: Callable[[str], Any]
    # Theme for styling
    theme: Any
    # Working directory for render context
    cwd: str
    # Terminal width for rendering (default: 100)
    width: int = 100


@dataclass(slots=True)
class ToolHtmlResult:
    """MISAKA fork: upstream's `{ collapsed?, expanded? }` object."""

    collapsed: str | None = None
    expanded: str | None = None


class ToolHtmlRenderer(Protocol):
    def renderCall(self, toolCallId: str, toolName: str, args: Any) -> str | None:
        """Render a tool call to HTML. Returns None if tool has no custom renderer."""
        ...

    def renderResult(
        self,
        toolCallId: str,
        toolName: str,
        result: list[dict[str, Any]],
        details: Any,
        isError: bool,
    ) -> ToolHtmlResult | None:
        """Render a tool result to collapsed/expanded HTML. Returns None if tool has no custom renderer."""
        ...


@dataclass(slots=True)
class _ToolRenderContext:
    """The `ToolRenderContext` the export hands to renderers (the protocol in `extensions/types.py`)."""

    args: Any
    toolCallId: str
    invalidate: Callable[[], None]
    lastComponent: Any
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


# Create a tool HTML renderer.
#
# The renderer looks up tool definitions and invokes their renderCall/renderResult
# methods, converting the resulting TUI Component output (ANSI) to HTML.
_ANSI_ESCAPE_REGEX = re.compile(r"\x1b\[[0-9;]*m")


def isBlankRenderedLine(line: str) -> bool:
    return len(jsTrim(_ANSI_ESCAPE_REGEX.sub("", line))) == 0


def trimRenderedResultLines(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and isBlankRenderedLine(lines[start]):
        start += 1
    while end > start and isBlankRenderedLine(lines[end - 1]):
        end -= 1
    return lines[start:end]


class _Renderer:
    """MISAKA fork: the object literal `createToolHtmlRenderer` returns, with the closure state as attributes."""

    def __init__(self, deps: ToolHtmlRendererDeps) -> None:
        self._getToolRenderers = deps.getToolRenderers
        self._theme = deps.theme
        self._cwd = deps.cwd
        self._width = deps.width
        self._renderedCallComponents: dict[str, _Component] = {}
        self._renderedResultComponents: dict[str, _Component] = {}
        self._renderedStates: dict[str, Any] = {}
        self._renderedArgs: dict[str, Any] = {}

    def _getState(self, toolCallId: str) -> Any:
        state = self._renderedStates.get(toolCallId)
        if state is None:
            state = {}
            self._renderedStates[toolCallId] = state
        return state

    def _createRenderContext(
        self,
        toolCallId: str,
        lastComponent: _Component | None,
        expanded: bool,
        isPartial: bool,
        isError: bool,
    ) -> _ToolRenderContext:
        return _ToolRenderContext(
            args=self._renderedArgs.get(toolCallId),
            toolCallId=toolCallId,
            invalidate=lambda: None,
            lastComponent=lastComponent,
            state=self._getState(toolCallId),
            cwd=self._cwd,
            executionStarted=True,
            argsComplete=True,
            isPartial=isPartial,
            expanded=expanded,
            showImages=False,
            isError=isError,
            durationMs=None,
            outputPad=1,
        )

    def renderCall(self, toolCallId: str, toolName: str, args: Any) -> str | None:
        try:
            self._renderedArgs[toolCallId] = args
            toolDef = self._getToolRenderers(toolName)
            renderCall = read_field(toolDef, "renderCall")
            if not renderCall:
                return None

            component = renderCall(
                args,
                self._theme,
                self._createRenderContext(toolCallId, self._renderedCallComponents.get(toolCallId), False, True, False),
            )
            self._renderedCallComponents[toolCallId] = component
            lines = component.render(self._width)
            return ansiLinesToHtml(lines)
        except Exception:  # noqa: BLE001 - On error, return None so HTML export can fall back to structured result rendering
            return None

    def renderResult(
        self,
        toolCallId: str,
        toolName: str,
        result: list[dict[str, Any]],
        details: Any,
        isError: bool,
    ) -> ToolHtmlResult | None:
        try:
            toolDef = self._getToolRenderers(toolName)
            renderResult = read_field(toolDef, "renderResult")
            if not renderResult:
                return None

            # Build AgentToolResult from content array
            agentToolResult = {
                "content": result,
                "details": details,
                "isError": isError,
            }

            # Render collapsed
            collapsedComponent = renderResult(
                agentToolResult,
                {"expanded": False, "isPartial": False},
                self._theme,
                self._createRenderContext(toolCallId, self._renderedResultComponents.get(toolCallId), False, False, isError),
            )
            self._renderedResultComponents[toolCallId] = collapsedComponent
            collapsed = ansiLinesToHtml(trimRenderedResultLines(collapsedComponent.render(self._width)))

            # Render expanded
            expandedComponent = renderResult(
                agentToolResult,
                {"expanded": True, "isPartial": False},
                self._theme,
                self._createRenderContext(toolCallId, self._renderedResultComponents.get(toolCallId), True, False, isError),
            )
            self._renderedResultComponents[toolCallId] = expandedComponent
            expanded = ansiLinesToHtml(trimRenderedResultLines(expandedComponent.render(self._width)))

            return ToolHtmlResult(
                collapsed=collapsed if collapsed and collapsed != expanded else None,
                expanded=expanded,
            )
        except Exception:  # noqa: BLE001 - On error, return None so HTML export can fall back to structured result rendering
            return None


def createToolHtmlRenderer(deps: ToolHtmlRendererDeps) -> ToolHtmlRenderer:
    return _Renderer(deps)


__all__ = ["ToolHtmlRenderer", "ToolHtmlRendererDeps", "ToolHtmlResult", "createToolHtmlRenderer"]
