"""An ally's turns, written as a pi session file.

The same file a Sister card leaves, in the same place (the card's session folder): whatever
reads a card's conversation -- ``misaka_sister_peek``, the sidebar, the read-only viewer, a
workflow's list of conversations -- reads an ally's too. It is a record only: the
ally's own agent keeps the conversation it continues from; nothing replays this file to a model.

An ACP turn arrives as a stream of updates. Text and thought accumulate until a tool call
starts, which closes an assistant message carrying the call; the call's result becomes a tool
result message when the agent reports it finished.
"""
from __future__ import annotations

import json
import time

from misaka.ai.types import (
    AssistantMessage,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UsageCost,
    UserMessage,
)

STOP_REASONS = {"end_turn": "stop", "max_tokens": "length", "max_turn_requests": "length",
                "cancelled": "aborted", "refusal": "error"}


def _now():
    return int(time.time() * 1000)


def _tool_name(update):
    meta = update.get("_meta") if isinstance(update.get("_meta"), dict) else {}
    claude = meta.get("claudeCode") if isinstance(meta.get("claudeCode"), dict) else {}
    return str(claude.get("toolName") or update.get("kind") or "tool")


def _tool_output(update):
    """What a finished tool call produced, as text: its raw output when it gave one (Claude's
    adapter sends the same text again inside a markdown fence under ``content``), else the
    text of its content blocks, else the files it changed."""
    raw = update.get("rawOutput")
    if isinstance(raw, str) and raw.strip():
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("output"), str):
        return raw["output"]
    parts = []
    for block in update.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "content" and isinstance(block.get("content"), dict):
            parts.append(str(block["content"].get("text") or ""))
        elif block.get("type") == "diff":
            parts.append(f"edited {block.get('path')}")
    text = "\n".join(part for part in parts if part)
    if text:
        return text
    return json.dumps(raw, ensure_ascii=False) if raw not in (None, "", {}) else ""


class Transcript:
    def __init__(self, manager, ally):
        self.manager, self.ally = manager, ally
        self._text, self._thought = [], []
        self._calls: dict[str, str] = {}      # tool call id -> name, for calls still open
        self.produced = False                  # the turn in progress said or did anything
        self.last_text = ""

    @property
    def session_id(self):
        return self.manager.getSessionId()

    @property
    def path(self):
        return self.manager.getSessionFile()

    @property
    def calls_open(self):
        return bool(self._calls)

    def _assistant(self, content, stop, error=None):
        self.manager.appendMessage(AssistantMessage(
            content=content, api="acp", provider=self.ally, model=self.ally, stopReason=stop,
            errorMessage=error, timestamp=_now(),
            usage=Usage(input=0, output=0, cacheRead=0, cacheWrite=0, totalTokens=0,
                        cost=UsageCost(input=0, output=0, cacheRead=0, cacheWrite=0, total=0))))

    def _pending_content(self):
        content = []
        if self._thought:
            content.append(ThinkingContent(thinking="".join(self._thought)))
        if self._text:
            text = "".join(self._text)
            content.append(TextContent(text=text))
            if text.strip():
                self.last_text = text.strip()
        self._text, self._thought = [], []
        return content

    def user(self, text):
        self.produced = False
        self.manager.appendMessage(UserMessage(content=[TextContent(text=text)], timestamp=_now()))

    def text(self, chunk):
        self.produced = self.produced or bool(chunk.strip())
        self._text.append(chunk)

    def thought(self, chunk):
        self.produced = self.produced or bool(chunk.strip())
        self._thought.append(chunk)

    def tool_call(self, update):
        call_id = str(update.get("toolCallId") or "")
        if not call_id or call_id in self._calls:
            return
        self.produced = True
        name = _tool_name(update)
        self._calls[call_id] = name
        raw = update.get("rawInput")
        arguments = raw if isinstance(raw, dict) else {"title": str(update.get("title") or name)}
        self._assistant([*self._pending_content(), ToolCall(id=call_id, name=name, arguments=arguments)],
                        "toolUse")

    def tool_update(self, update):
        call_id = str(update.get("toolCallId") or "")
        status = update.get("status")
        if call_id not in self._calls or status not in ("completed", "failed"):
            return
        self._result(call_id, _tool_output(update), failed=status == "failed")

    def _result(self, call_id, text, *, failed):
        name = self._calls.pop(call_id)
        self.manager.appendMessage(ToolResultMessage(
            toolCallId=call_id, toolName=name, content=[TextContent(text=text or "(no output)")],
            isError=failed, timestamp=_now()))

    def plan(self, update):
        entries = [str(entry.get("content") or "") for entry in update.get("entries") or []
                   if isinstance(entry, dict)]
        if entries:
            self.manager.appendCustomMessageEntry(
                "ally-plan", "Plan:\n" + "\n".join(f"- {entry}" for entry in entries), True)

    def end(self, stop_reason, error=None):
        """Close the turn: what is still open is written, calls with no result get one."""
        for call_id in list(self._calls):
            self._result(call_id, "The turn ended before this call reported a result.", failed=True)
        content = self._pending_content()
        stop = "error" if error else STOP_REASONS.get(stop_reason, "stop")
        if content or error:
            self._assistant(content or [TextContent(text="")], stop, error)
