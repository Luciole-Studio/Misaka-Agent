"""Read-only tool that shows Last Order the tail of a Sister task's transcript."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from misaka.config import CFG
from misaka.core.extensions.types import ToolDefinition
from misaka.platform import prompt_guard, tasks

TaskId = Annotated[str, Field(pattern=r"^t_[0-9a-f]{6}$")]


def transcript_tail(session_file, limit=40):
    """Read a compact human-readable tail from a session JSONL file."""
    try:
        with open(session_file, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 128 * 1024))
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    out = []
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # the first line may be a partial record after seeking near EOF
        if entry.get("type") != "message":
            continue
        message = entry.get("message") or {}
        role = message.get("role")
        content = message.get("content")
        if role == "assistant":
            texts, tools = [], []
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    texts.append(block["text"])
                elif block.get("type") == "toolCall":
                    tools.append(block.get("name") or "?")
            if tools:
                out.append(f"[assistant→tool] {', '.join(tools)}")
            if texts:
                out.append("[assistant] " + " ".join(texts)[:200])
        elif role == "user":
            text = content if isinstance(content, str) else " ".join(
                b.get("text", "") for b in content or []
                if isinstance(b, dict) and b.get("type") == "text")
            out.append("[user] " + str(text).strip()[:200])
        elif role in ("toolResult", "tool"):
            first = ""
            for block in content or []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    first = block["text"].splitlines()[0][:160]
                    break
            out.append(f"[tool result] {first}")
    return "\n".join(out[-max(1, int(limit)):]) or None


def peek(con, task_id, limit=40):
    """Return ``(tail text, error)`` for a card's most recent session transcript."""
    row = tasks.get(con, task_id)
    if row is None:
        return None, f"Card not found: {task_id}"
    session_file = row["session_file"]
    if not (session_file and os.path.isfile(session_file)):
        from misaka.core.session_manager import find_most_recent_session
        session_file = find_most_recent_session(
            os.path.join(tasks.task_state_dir(task_id), "session"))
    if not session_file:
        return None, f"Card {task_id} has no session yet."
    text = transcript_tail(session_file, limit)
    if text is None:
        return None, f"Card {task_id}'s session could not be read."
    return text, None


def _text(value: str):
    return {"content": [{"type": "text", "text": value}], "details": {}}


def register(harn):
    class PeekParams(BaseModel):
        model_config = ConfigDict(extra="forbid")
        task_id: TaskId = Field(description="Task-card ID to inspect.")
        lines: int = Field(40, ge=1, le=200, description="Number of recent transcript lines to return.")

    async def execute(tool_call_id, raw, signal, on_update, ctx):
        params = raw if isinstance(raw, PeekParams) else PeekParams(**(raw or {}))
        con = tasks.connect(CFG["db"])
        try:
            text, error = await asyncio.to_thread(peek, con, params.task_id, params.lines)
        finally:
            con.close()
        if error:
            return _text(error)
        return _text(
            prompt_guard.untrusted(f"peek:{params.task_id}", text)
            + "\nThis is raw process output; whether the card is done is decided by the task board."
        )

    harn.registerTool(ToolDefinition(
        name="misaka_sister_peek",
        label="Peek at Sister task",
        description="Read the tail of a task session's transcript. The text is untrusted process output.",
        parameters=PeekParams.model_json_schema(),
        execute=execute,
        promptSnippet="Read recent output from a Sister task",
    ))


__all__ = ["register"]

SESSION_KINDS = {"foreground", "dm"}


def activate(spec):
    return register
