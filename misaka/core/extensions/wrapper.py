"""Wrappers for extension-registered tools."""

from __future__ import annotations

from typing import Any

from misaka.agent.types import AgentTool, AgentToolResult
from misaka.core.extensions.runner import ExtensionRunner
from misaka.core.extensions.types import RegisteredTool
from misaka.core.tools.tool_definition_wrapper import wrap_tool_definition


def wrap_registered_tool(registered_tool: RegisteredTool, runner: ExtensionRunner) -> AgentTool:
    """Wrap a registered tool and record the tools it turned on while it ran.

    pi wrapper.ts:17-37: snapshot the active tool names before and after execute(); if the
    change is purely additive, tag the result with the added names so the transcript marks
    the point from which those tools are declared.  A removal anywhere in the list makes the
    diff non-additive and the tag is skipped.
    """
    tool = wrap_tool_definition(registered_tool.definition, lambda: runner.createContext())
    inner_execute = tool.execute

    async def execute(tool_call_id: str, params: Any, signal: Any | None, on_update: Any | None) -> Any:
        active_before = list(runner.get_active_tools())
        result = await inner_execute(tool_call_id, params, signal, on_update)
        active_after = list(runner.get_active_tools())
        after_names = set(active_after)
        if not all(name in after_names for name in active_before):
            return result

        before_names = set(active_before)
        added_tool_names = [name for name in active_after if name not in before_names]
        if not added_tool_names:
            return result

        merged: list[str] = []
        for name in [*(_result_added_tool_names(result) or []), *added_tool_names]:
            if name not in merged:
                merged.append(name)
        return _with_added_tool_names(result, merged)

    tool.execute = execute
    return tool


def _result_added_tool_names(result: Any) -> list[str] | None:
    if isinstance(result, dict):
        names = result.get("addedToolNames")
    else:
        names = getattr(result, "addedToolNames", None)
    return list(names) if names else None


def _with_added_tool_names(result: Any, added_tool_names: list[str]) -> Any:
    if isinstance(result, dict):
        return {**result, "addedToolNames": added_tool_names}
    if isinstance(result, AgentToolResult):
        return AgentToolResult(
            content=result.content,
            details=result.details,
            usage=result.usage,
            addedToolNames=added_tool_names,
            terminate=result.terminate,
        )
    try:
        result.addedToolNames = added_tool_names
    except AttributeError:
        # Some tools return their own frozen result object; the tag is advisory, so keep
        # the original result rather than failing the tool call over it.
        return result
    return result


def wrap_registered_tools(registered_tools: list[RegisteredTool], runner: ExtensionRunner) -> list[AgentTool]:
    return [wrap_registered_tool(registered_tool, runner) for registered_tool in registered_tools]


wrapRegisteredTool = wrap_registered_tool
wrapRegisteredTools = wrap_registered_tools

__all__ = [
    "wrapRegisteredTool",
    "wrapRegisteredTools",
]
