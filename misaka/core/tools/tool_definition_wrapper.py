"""Adapters between extension tool definitions and agent runtime tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from misaka.agent.types import AgentTool
from misaka.core.extensions.types import ExtensionToolContext, ToolDefinition

# Creates the context for executing the tool call `toolCallId` with the call's signal.
type ToolContextFactory = Callable[[str, Any], ExtensionToolContext]


def wrapToolDefinition[TDetails](
    definition: ToolDefinition[Any, TDetails],
    ctx_factory: ToolContextFactory | None = None,
) -> AgentTool:
    async def execute(
        tool_call_id: str,
        params: Any,
        signal: Any | None = None,
        on_update: Any | None = None,
        ctx: ExtensionToolContext | None = None,
    ) -> Any:
        return await definition.execute(
            tool_call_id,
            params,
            signal,
            on_update,
            ctx
            if ctx is not None
            else (ctx_factory(tool_call_id, signal) if ctx_factory is not None else None),
        )

    return AgentTool(
        name=definition.name,
        aliases=definition.aliases,
        label=definition.label,
        description=definition.description,
        parameters=definition.parameters,
        constrainedSampling=definition.constrainedSampling,
        prepareArguments=definition.prepareArguments,
        outputSchema=definition.outputSchema,
        executionMode=definition.executionMode,
        execute=execute,
    )


def wrapToolDefinitions(
    definitions: list[ToolDefinition[Any, Any]],
    ctx_factory: ToolContextFactory | None = None,
) -> list[AgentTool]:
    return [wrapToolDefinition(definition, ctx_factory) for definition in definitions]


def createToolDefinitionFromAgentTool(tool: Any) -> ToolDefinition[Any, Any]:
    async def execute(
        tool_call_id: str,
        params: Any,
        signal: Any | None,
        on_update: Any | None,
        _ctx: Any,
    ) -> Any:
        return await tool.execute(tool_call_id, params, signal, on_update)

    return ToolDefinition(
        name=tool.name,
        aliases=getattr(tool, "aliases", ()),
        label=tool.label,
        description=tool.description,
        parameters=tool.parameters,
        constrainedSampling=tool.constrainedSampling,
        prepareArguments=getattr(tool, "prepareArguments", None),
        outputSchema=getattr(tool, "outputSchema", None),
        executionMode=getattr(tool, "executionMode", None),
        execute=execute,
    )



__all__ = [
    "ToolContextFactory",
    "createToolDefinitionFromAgentTool",
    "wrapToolDefinition",
    "wrapToolDefinitions",
]
