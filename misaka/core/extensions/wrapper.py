"""Wrappers for extension-registered tools."""

from __future__ import annotations

from misaka.agent.types import AgentTool
from misaka.core.extensions.runner import ExtensionRunner
from misaka.core.extensions.types import RegisteredTool
from misaka.core.tools.tool_definition_wrapper import wrapToolDefinition


def wrapRegisteredTool(registered_tool: RegisteredTool, runner: ExtensionRunner) -> AgentTool:
    """Wrap a registered tool into an AgentTool.

    Each call gets the runner's tool context (``createToolContext()``): the extension context
    plus ``tools`` and ``executeTool()``. Tools a call turned on are no longer tagged on its
    result (pi 0.87): the agent loop declares tool changes to the model with a system message
    before the next request.
    """
    return wrapToolDefinition(
        registered_tool.definition,
        lambda tool_call_id, signal: runner.createToolContext(tool_call_id, signal),
    )


def wrapRegisteredTools(registered_tools: list[RegisteredTool], runner: ExtensionRunner) -> list[AgentTool]:
    return [wrapRegisteredTool(registered_tool, runner) for registered_tool in registered_tools]



__all__ = [
    "wrapRegisteredTool",
    "wrapRegisteredTools",
]
