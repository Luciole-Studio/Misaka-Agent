"""Pinned CCB forkSubagent / agentToolFilter through MISAKA's isolated process host.

Native JSONL stores tool results separately; the Anthropic provider coalesces
them on the wire. Never clone a parent's writer or execute its bound callbacks.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from misaka.core.subagent.agents import AgentDefinition
from misaka.core.subagent.background import _truthy, background_disabled
from misaka.core.subagent.tool_policy import (
    ALL_AGENT_DISALLOWED_TOOLS,
    canonical_tool_name,
)
from misaka.utils.values import read_field

FORK_TAG = "fork-boilerplate"
PLACEHOLDER = "Fork started — processing in background"


def enabled(context: Any) -> bool:
    from misaka.config.product import setting

    return (
        _truthy(os.environ.get("MISAKA_FORK_SUBAGENT"))
        and bool(getattr(context, "hasUI", False))
        and not setting("subagents", "coordinator_mode", False, bool)
        and not background_disabled()
    )


def in_fork_child(messages: list[Any]) -> bool:
    for message in messages:
        if read_field(message, "role") != "user":
            continue
        content = read_field(message, "content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if any(f"<{FORK_TAG}>" in str(read_field(block, "text", "")) for block in content):
            return True
    return False


def child_message(directive: str) -> str:
    # Source wording, with the commit instruction adapted for shared dirty trees.
    return f"""<{FORK_TAG}>
STOP. READ THIS FIRST.

You are a forked worker process. You are NOT the main agent.

RULES (non-negotiable):
1. Your system prompt says "default to forking." IGNORE IT — that's for the parent. You ARE the fork. Do NOT spawn sub-agents; execute directly.
2. Do NOT converse, ask questions, or suggest next steps
3. Do NOT editorialize or add meta-commentary
4. USE your tools directly: bash, read, write, etc.
5. Preserve other sessions' changes. Commit only if your directive explicitly requests a commit.
6. Do NOT emit text between tool calls. Use tools silently, then report once at the end.
7. Stay strictly within your directive's scope. If you discover related systems outside your scope, mention them in one sentence at most — other workers cover those areas.
8. Keep your report under 500 words unless the directive specifies otherwise. Be factual and concise.
9. Your response MUST begin with "Scope:". No preamble, no thinking-out-loud.
10. REPORT structured facts, then stop

Output format (plain text labels, not markdown headers):
  Scope: <echo back your assigned scope in one sentence>
  Result: <the answer or key findings, limited to the scope above>
  Key files: <relevant file paths — include for research tasks>
  Files changed: <list; include commit hash only if a commit was requested>
  Issues: <list — include only if there are issues to flag>
</{FORK_TAG}>

Your directive: {directive}"""


def definition(session: Any) -> AgentDefinition:
    tools = [tool.name for tool in session.agent.state.tools
             if canonical_tool_name(tool.name) not in ALL_AGENT_DISALLOWED_TOOLS]
    return AgentDefinition(
        name="fork", description="Implicit fork inheriting the parent conversation.",
        prompt="", source="fork", tools=tools, max_turns=200, model="inherit",
        permission_mode="bubble", background=True, omit_context_files=True,
    )


def snapshot_path(task: Any) -> Path:
    return task.metadata_path.with_suffix(".fork.json")


def capture(task: Any, session: Any, *, seed: bool) -> None:
    from misaka.core.session_manager import SessionManager
    from misaka.core.subagent.runtime import _atomic_text
    from misaka.modes.jsonl import to_jsonable

    if seed and task.transcript.exists():
        raise ValueError("Fork seed transcript already exists")
    history = copy.deepcopy(to_jsonable(session.agent.state.messages))
    if in_fork_child(history):
        raise ValueError("Fork children execute directly; recursive forks are disabled")
    # Concurrent calls must clone the same assistant prefix, not whichever sibling
    # happens to have finished first. Search by this call's identity.
    if seed and task.tool_call_id:
        index = next((index for index in range(len(history) - 1, -1, -1)
                      if history[index].get("role") == "assistant"
                      and any(block.get("type") == "toolCall" and block.get("id") == task.tool_call_id
                              for block in history[index].get("content", []))), None)
        if index is None:
            raise ValueError("Fork launch has no matching parent assistant tool call")
        history = history[:index + 1]
        for block in history[-1].get("content", []):
            if block.get("type") == "toolCall":
                history.append({
                    "role": "toolResult", "toolCallId": block["id"], "toolName": block["name"],
                    "content": [{"type": "text", "text": PLACEHOLDER}], "isError": False,
                    "timestamp": 0,
                })
    tools = [tool for tool in session.agent.state.tools
             if canonical_tool_name(tool.name) not in ALL_AGENT_DISALLOWED_TOOLS]
    payload = {
        "schemaVersion": 1, "agentId": task.id, "parentSessionId": task.parent_session_id,
        "systemPrompt": session.agent.state.systemPrompt,
        "tools": [{"name": tool.name, "description": tool.description,
                   "parameters": tool.parameters_json_schema(),
                   "constrainedSampling": to_jsonable(tool.constrainedSampling)}
                  for tool in tools],
    }
    _atomic_text(snapshot_path(task), json.dumps(payload, ensure_ascii=False))
    if seed:
        writer = SessionManager(task.cwd, str(task.transcript.parent), str(task.transcript), True)
        for message in history:
            writer.appendMessage(message)
        # A command may fork before the first assistant response. Native UI
        # sessions intentionally defer that write; a child seed must be durable.
        writer._rewriteFile()
    task.definition = definition(session)


def install(session: Any, snapshot: dict[str, Any]) -> None:
    """Keep rendered prompt/schema bytes; resolve implementations in THIS worker."""
    from misaka.agent.stream_fn import get_default_stream_fn
    from misaka.ai.types import Tool

    wire_tools = [Tool.model_validate(item) for item in snapshot["tools"]]
    names = [tool.name for tool in wire_tools]
    session.setActiveToolsByName(names)
    # Missing tools are fatal for an exact fork, unlike an ordinary worker's
    # best-effort missing-tool warning. Do not silently claim a cache-identical fork.
    assembled = {tool.name: tool for tool in session.agent.state.tools}
    missing = set(names) - assembled.keys()
    if missing:
        raise ValueError(f"Fork parent tools are unavailable in this worker: {', '.join(sorted(missing))}")
    previous = session.agent.streamFn or get_default_stream_fn()
    session.agent._subagent_base_stream_fn = previous

    def stream(model, context, options):
        active = {tool.name: tool for tool in session.agent.state.tools}
        for tool in wire_tools:
            if tool.name not in active:
                raise ValueError(f"Fork tool became unavailable: {tool.name}")
            # Advertised tool descriptions can legitimately differ by role. Input
            # schemas may not: otherwise the child could execute different args.
            if tool.parameters_json_schema() != active[tool.name].parameters_json_schema():
                raise ValueError(f"Fork tool schema differs from the parent: {tool.name}")
        exact = context.model_copy(update={"systemPrompt": snapshot["systemPrompt"], "tools": wire_tools})
        return previous(model, exact, options)

    session.agent.streamFn = stream
