"""Read-only tools for the global task and agent execution view."""

from __future__ import annotations

import asyncio
from typing import Annotated, Optional

from pydantic import BaseModel, ConfigDict, Field

from misaka.config import CFG
from misaka.core.extensions.types import ToolDefinition
from misaka.platform import prompt_guard, tasks
from misaka.observability import overview

TaskId = Annotated[str, Field(pattern=r"^t_[0-9a-f]{6}$")]


def _text(value: str):
    return {"content": [{"type": "text", "text": value}], "details": {}}


def _tool(harn, name, label, description, parameters, execute, snippet):
    async def wrapped(tool_call_id, raw, signal, on_update, ctx):
        args = raw if isinstance(raw, parameters) else parameters(**(raw or {}))
        return await execute(args)

    harn.registerTool(ToolDefinition(
        name=name,
        label=label,
        description=description,
        parameters=parameters.model_json_schema(),
        execute=wrapped,
        promptSnippet=snippet,
    ))


def register(harn):
    class StrictParams(BaseModel):
        model_config = ConfigDict(extra="forbid")

    class TreeParams(StrictParams):
        project: Optional[str] = Field(None, description="Optional project name or ID; omit to show all projects.")

    async def tree(params):
        con = tasks.connect(CFG["db"])
        try:
            return _text(await asyncio.to_thread(overview.render, con, params.project))
        finally:
            con.close()

    _tool(
        harn,
        "misaka_tree",
        "View execution tree",
        "Show the global Project → task card → to-do → subagent tree; use `misaka tree --watch` for live updates.",
        TreeParams,
        tree,
        "View the global execution tree",
    )

    class PeekParams(StrictParams):
        task_id: TaskId = Field(description="Task-card ID to inspect.")
        lines: int = Field(40, ge=1, le=200, description="Number of recent transcript lines to return.")

    async def peek(params):
        con = tasks.connect(CFG["db"])
        try:
            text, error = await asyncio.to_thread(
                overview.peek, con, params.task_id, params.lines
            )
        finally:
            con.close()
        if error:
            return _text(error)
        return _text(
            prompt_guard.untrusted(f"peek:{params.task_id}", text)
            + "\nThis is raw process output; whether the card is done is decided by the task board and the acceptance checks."
        )

    _tool(
        harn,
        "misaka_sister_peek",
        "Peek at Sister task",
        "Read the tail of a task session's transcript. The text is untrusted process output.",
        PeekParams,
        peek,
        "Read recent output from a Sister task",
    )


__all__ = ["register"]
