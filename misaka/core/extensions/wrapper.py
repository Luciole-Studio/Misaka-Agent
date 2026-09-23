"""Wrappers for extension-registered tools."""

from __future__ import annotations

from misaka.agent.types import AgentTool
from misaka.core.extensions.runner import ExtensionRunner
from misaka.core.extensions.types import RegisteredTool
from misaka.core.tools.tool_definition_wrapper import wrap_tool_definition


def wrap_registered_tool(registered_tool: RegisteredTool, runner: ExtensionRunner) -> AgentTool:
    """Wrap a registered tool into an AgentTool.

    Uses the runner's createContext() for consistent context across tools and event handlers.
    Tools a call turned on are no longer tagged on its result (pi 0.87): the agent loop
    declares tool changes to the model with a system message before the next request.
    """
    return wrap_tool_definition(registered_tool.definition, lambda: runner.createContext())


def wrap_registered_tools(registered_tools: list[RegisteredTool], runner: ExtensionRunner) -> list[AgentTool]:
    return [wrap_registered_tool(registered_tool, runner) for registered_tool in registered_tools]


wrapRegisteredTool = wrap_registered_tool
wrapRegisteredTools = wrap_registered_tools

__all__ = [
    "wrapRegisteredTool",
    "wrapRegisteredTools",
]
