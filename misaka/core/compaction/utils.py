"""Translated from pi's ``packages/coding-agent/src/core/compaction/utils.ts`` (v1.3.0).

Shared utilities for compaction and branch summarization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from misaka.ai.types import MessageValue
from misaka.ai.utils.text import contentText
from misaka.utils.js import jsLength, jsonStringify, jsSlice
from misaka.utils.values import read_field

# ============================================================================
# File Operation Tracking
# ============================================================================


@dataclass(slots=True)
class FileOperations:
    read: set[str] = field(default_factory=set)
    written: set[str] = field(default_factory=set)
    edited: set[str] = field(default_factory=set)


def createFileOps() -> FileOperations:
    return FileOperations()


def extractFileOpsFromMessage(message: Any, fileOps: FileOperations) -> None:
    """Extract file operations from tool calls in an assistant message, or from the nested calls
    recorded on a tool result.
    """
    if read_field(message, "role") == "toolResult":
        # Calls made from codemode scripts are recorded on the script's result.
        nestedCalls = read_field(message, "nestedCalls")
        for call in (read_field(nestedCalls, "calls") or []) if nestedCalls is not None else []:
            addFileOp(read_field(call, "name"), read_field(call, "arguments"), fileOps)
        return
    if read_field(message, "role") != "assistant":
        return
    content = read_field(message, "content")
    if not isinstance(content, list):
        return

    for block in content:
        if block is None:
            continue
        if read_field(block, "type") != "toolCall":
            continue
        addFileOp(read_field(block, "name"), read_field(block, "arguments"), fileOps)


def addFileOp(toolName: Any, args: Any, fileOps: FileOperations) -> None:
    path = args.get("path") if isinstance(args, dict) and isinstance(args.get("path"), str) else None
    if not path:
        return
    match toolName:
        case "read":
            fileOps.read.add(path)
        case "write":
            fileOps.written.add(path)
        case "edit":
            fileOps.edited.add(path)


def computeFileLists(fileOps: FileOperations) -> dict[str, list[str]]:
    """Compute final file lists from file operations.
    Returns readFiles (files only read, not modified) and modifiedFiles.
    """
    modified = {*fileOps.edited, *fileOps.written}
    readOnly = sorted(f for f in fileOps.read if f not in modified)
    modifiedFiles = sorted(modified)
    return {"readFiles": readOnly, "modifiedFiles": modifiedFiles}


def formatFileOperations(readFiles: list[str], modifiedFiles: list[str]) -> str:
    """Format file operations as XML tags for summary."""
    sections: list[str] = []
    if len(readFiles) > 0:
        sections.append(f"<read-files>\n{chr(10).join(readFiles)}\n</read-files>")
    if len(modifiedFiles) > 0:
        sections.append(f"<modified-files>\n{chr(10).join(modifiedFiles)}\n</modified-files>")
    if len(sections) == 0:
        return ""
    return "\n\n" + "\n\n".join(sections)


# ============================================================================
# Message Serialization
# ============================================================================

# Maximum characters for a tool result in serialized summaries.
TOOL_RESULT_MAX_CHARS = 2000


def truncateForSummary(text: str, maxChars: int) -> str:
    """Truncate text to a maximum character length for summarization.
    Keeps the beginning and appends a truncation marker.
    """
    if jsLength(text) <= maxChars:
        return text
    truncatedChars = jsLength(text) - maxChars
    return f"{jsSlice(text, 0, maxChars)}\n\n[... {truncatedChars} more characters truncated]"


def serializeConversation(messages: list[MessageValue]) -> str:
    """Serialize LLM messages to text for summarization.
    This prevents the model from treating it as a conversation to continue.
    Call convertToLlm() first to handle custom message types.

    Tool results are truncated to keep the summarization request within
    reasonable token budgets. Full content is not needed for summarization.

    Thinking is omitted. Providers return summaries of the real reasoning, and
    replayed thinking is often not part of the model's live context: for example,
    a block that does not match its request is dropped. Including it can make the
    summarization request larger than the context it summarizes. Visible text,
    tool calls, and tool results record what happened.
    """
    parts: list[str] = []

    for msg in messages:
        role = read_field(msg, "role")
        if role == "user":
            content = contentText(read_field(msg, "content") or [], "")
            if content:
                parts.append(f"[User]: {content}")
        elif role == "assistant":
            toolCalls: list[str] = []
            blocks = read_field(msg, "content") or []

            for block in blocks:
                blockType = read_field(block, "type")
                if blockType == "toolCall":
                    args = read_field(block, "arguments")
                    argsStr = ", ".join(
                        f"{k}={jsonStringify(v)}" for k, v in (args.items() if isinstance(args, dict) else [])
                    )
                    toolCalls.append(f"{read_field(block, 'name')}({argsStr})")

            if any(read_field(block, "type") == "text" for block in blocks):
                parts.append(f"[Assistant]: {contentText(blocks)}")
            if len(toolCalls) > 0:
                parts.append(f"[Assistant tool calls]: {'; '.join(toolCalls)}")
        elif role == "toolResult":
            content = contentText(read_field(msg, "content") or [], "")
            if content:
                parts.append(f"[Tool result]: {truncateForSummary(content, TOOL_RESULT_MAX_CHARS)}")

    return "\n\n".join(parts)


# ============================================================================
# Summarization System Prompt
# ============================================================================

SUMMARIZATION_SYSTEM_PROMPT = """You are a context summarization assistant. Your task is to read a conversation between a user and an AI assistant, then produce a structured summary following the exact format specified.

Do NOT continue the conversation. Do NOT respond to any questions in the conversation. ONLY output the structured summary."""


__all__ = [
    "SUMMARIZATION_SYSTEM_PROMPT",
    "FileOperations",
    "computeFileLists",
    "createFileOps",
    "extractFileOpsFromMessage",
    "formatFileOperations",
    "serializeConversation",
]
