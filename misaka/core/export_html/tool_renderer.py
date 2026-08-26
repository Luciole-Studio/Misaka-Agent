"""HTML renderer for custom tool components."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from misaka.core.export_html.ansi_to_html import ansi_lines_to_html

ANSI_ESCAPE_REGEX = re.compile(r"\x1b\[[0-9;]*m")


class RenderableComponent(Protocol):
    def render(self, width: int) -> list[str]: ...


@dataclass(slots=True)
class ToolHtmlRendererDeps:
    getToolDefinition: Any
    theme: Any
    cwd: str
    width: int = 100


@dataclass(slots=True)
class ToolRenderContext:
    args: Any
    toolCallId: str
    invalidate: Any
    lastComponent: RenderableComponent | None
    state: dict[str, Any]
    cwd: str
    executionStarted: bool
    argsComplete: bool
    isPartial: bool
    expanded: bool
    showImages: bool
    isError: bool


@dataclass(slots=True)
class ToolHtmlResult:
    collapsed: str | None = None
    expanded: str | None = None


class ToolHtmlRenderer(Protocol):
    def renderCall(self, toolCallId: str, toolName: str, args: Any) -> str | None: ...

    def renderResult(
        self,
        toolCallId: str,
        toolName: str,
        result: list[dict[str, Any]],
        details: Any,
        isError: bool,
    ) -> ToolHtmlResult | None: ...


def is_blank_rendered_line(line: str) -> bool:
    return len(ANSI_ESCAPE_REGEX.sub("", line).strip()) == 0


def trim_rendered_result_lines(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and is_blank_rendered_line(lines[start]):
        start += 1
    while end > start and is_blank_rendered_line(lines[end - 1]):
        end -= 1
    return lines[start:end]


@dataclass(slots=True)
class _ToolHtmlRenderer:
    deps: ToolHtmlRendererDeps
    rendered_call_components: dict[str, RenderableComponent] = field(default_factory=dict)
    rendered_result_components: dict[str, RenderableComponent] = field(default_factory=dict)
    rendered_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    rendered_args: dict[str, Any] = field(default_factory=dict)

    def _get_state(self, tool_call_id: str) -> dict[str, Any]:
        state = self.rendered_states.get(tool_call_id)
        if state is None:
            state = {}
            self.rendered_states[tool_call_id] = state
        return state

    def _create_render_context(
        self,
        tool_call_id: str,
        last_component: RenderableComponent | None,
        expanded: bool,
        is_partial: bool,
        is_error: bool,
    ) -> ToolRenderContext:
        return ToolRenderContext(
            args=self.rendered_args.get(tool_call_id),
            toolCallId=tool_call_id,
            invalidate=lambda: None,
            lastComponent=last_component,
            state=self._get_state(tool_call_id),
            cwd=self.deps.cwd,
            executionStarted=True,
            argsComplete=True,
            isPartial=is_partial,
            expanded=expanded,
            showImages=False,
            isError=is_error,
        )

    def renderCall(self, toolCallId: str, toolName: str, args: Any) -> str | None:
        try:
            self.rendered_args[toolCallId] = args
            tool_def = self.deps.getToolDefinition(toolName)
            render_call = getattr(tool_def, "renderCall", None)
            if not callable(render_call):
                return None

            component = render_call(
                args,
                self.deps.theme,
                self._create_render_context(
                    toolCallId,
                    self.rendered_call_components.get(toolCallId),
                    False,
                    True,
                    False,
                ),
            )
            self.rendered_call_components[toolCallId] = component
            return ansi_lines_to_html(component.render(self.deps.width))
        except Exception:  # noqa: BLE001 - a renderer that fails falls back to the plain result
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
            tool_def = self.deps.getToolDefinition(toolName)
            render_result = getattr(tool_def, "renderResult", None)
            if not callable(render_result):
                return None

            agent_tool_result = {
                "content": result,
                "details": details,
                "isError": isError,
            }

            collapsed_component = render_result(
                agent_tool_result,
                {"expanded": False, "isPartial": False},
                self.deps.theme,
                self._create_render_context(
                    toolCallId,
                    self.rendered_result_components.get(toolCallId),
                    False,
                    False,
                    isError,
                ),
            )
            self.rendered_result_components[toolCallId] = collapsed_component
            collapsed_html = ansi_lines_to_html(trim_rendered_result_lines(collapsed_component.render(self.deps.width)))

            expanded_component = render_result(
                agent_tool_result,
                {"expanded": True, "isPartial": False},
                self.deps.theme,
                self._create_render_context(
                    toolCallId,
                    self.rendered_result_components.get(toolCallId),
                    True,
                    False,
                    isError,
                ),
            )
            self.rendered_result_components[toolCallId] = expanded_component
            expanded_html = ansi_lines_to_html(trim_rendered_result_lines(expanded_component.render(self.deps.width)))

            return ToolHtmlResult(
                collapsed=collapsed_html if collapsed_html and collapsed_html != expanded_html else None,
                expanded=expanded_html,
            )
        except Exception:  # noqa: BLE001 - a renderer that fails falls back to the plain result
            return None


def create_tool_html_renderer(deps: ToolHtmlRendererDeps | dict[str, Any]) -> ToolHtmlRenderer:
    resolved = deps if isinstance(deps, ToolHtmlRendererDeps) else ToolHtmlRendererDeps(**deps)
    return _ToolHtmlRenderer(resolved)


__all__ = [
    "ToolHtmlRenderer",
    "ToolHtmlRendererDeps",
]
