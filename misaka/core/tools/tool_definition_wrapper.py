"""Adapters between extension tool definitions and agent runtime tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from misaka.agent.types import AgentTool
from misaka.core.extensions.types import ExtensionContext, ToolDefinition


def wrap_tool_definition[TDetails](
    definition: ToolDefinition[Any, TDetails],
    ctx_factory: Callable[[], ExtensionContext] | None = None,
) -> AgentTool:
    async def execute(tool_call_id: str, params: Any, signal: Any | None, on_update: Any | None) -> Any:
        return await definition.execute(tool_call_id, params, signal, on_update, ctx_factory() if ctx_factory else None)

    return AgentTool(
        name=definition.name,
        label=definition.label,
        description=definition.description,
        parameters=definition.parameters,
        prepareArguments=definition.prepareArguments,
        executionMode=definition.executionMode,
        execute=execute,
    )


def wrap_tool_definitions(
    definitions: list[ToolDefinition[Any, Any]],
    ctx_factory: Callable[[], ExtensionContext] | None = None,
) -> list[AgentTool]:
    return [wrap_tool_definition(definition, ctx_factory) for definition in definitions]


def create_tool_definition_from_agent_tool(tool: Any) -> ToolDefinition[Any, Any]:
    async def execute(tool_call_id: str, params: Any, signal: Any | None, on_update: Any | None, _ctx: Any) -> Any:
        return await tool.execute(tool_call_id, params, signal, on_update)

    return ToolDefinition(
        name=tool.name,
        label=tool.label,
        description=tool.description,
        parameters=tool.parameters,
        prepareArguments=getattr(tool, "prepareArguments", None),
        executionMode=getattr(tool, "executionMode", None),
        execute=execute,
    )


__all__ = [
]
