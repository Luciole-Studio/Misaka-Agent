"""Shared utilities for coding-agent compaction and branch summarization."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from misaka.ai.types import MessageValue

_TOOL_RESULT_MAX_CHARS = 2000

SUMMARIZATION_SYSTEM_PROMPT = (
    "You are a context summarization assistant. Your task is to read a conversation between a user "
    "and an AI coding assistant, then produce a structured summary following the exact format specified.\n\n"
    "Do NOT continue the conversation. Do NOT respond to any questions in the conversation. "
    "ONLY output the structured summary."
)


@dataclass(slots=True)
class FileOperations:
    read: set[str] = field(default_factory=set)
    written: set[str] = field(default_factory=set)
    edited: set[str] = field(default_factory=set)


def create_file_ops() -> FileOperations:
    return FileOperations()


def extract_file_ops_from_message(message: Any, file_ops: FileOperations) -> None:
    if _message_field(message, "role") != "assistant":
        return
    content = _message_field(message, "content")
    if not isinstance(content, list):
        return

    for block in content:
        if _block_field(block, "type") != "toolCall":
            continue
        arguments = _block_field(block, "arguments")
        if not isinstance(arguments, dict):
            continue
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            continue

        name = _block_field(block, "name")
        if name == "read":
            file_ops.read.add(path)
        elif name == "write":
            file_ops.written.add(path)
        elif name == "edit":
            file_ops.edited.add(path)


def compute_file_lists(file_ops: FileOperations) -> dict[str, list[str]]:
    modified = {*file_ops.edited, *file_ops.written}
    read_files = sorted(path for path in file_ops.read if path not in modified)
    modified_files = sorted(modified)
    return {"readFiles": read_files, "modifiedFiles": modified_files}


def format_file_operations(read_files: list[str], modified_files: list[str]) -> str:
    sections: list[str] = []
    if read_files:
        read_block = "\n".join(read_files)
        sections.append(f"<read-files>\n{read_block}\n</read-files>")
    if modified_files:
        modified_block = "\n".join(modified_files)
        sections.append(f"<modified-files>\n{modified_block}\n</modified-files>")
    if not sections:
        return ""
    return f"\n\n{'\n\n'.join(sections)}"


def serialize_conversation(messages: list[MessageValue]) -> str:
    parts: list[str] = []

    for message in messages:
        role = _message_field(message, "role")
        if role == "user":
            content = _serialize_user_content(_message_field(message, "content"))
            if content:
                parts.append(f"[User]: {content}")
            continue

        if role == "assistant":
            text_parts: list[str] = []
            thinking_parts: list[str] = []
            tool_calls: list[str] = []
            for block in _message_field(message, "content") or []:
                block_type = _block_field(block, "type")
                if block_type == "text":
                    text = _block_field(block, "text")
                    if isinstance(text, str):
                        text_parts.append(text)
                elif block_type == "thinking":
                    thinking = _block_field(block, "thinking")
                    if isinstance(thinking, str):
                        thinking_parts.append(thinking)
                elif block_type == "toolCall":
                    arguments = _block_field(block, "arguments")
                    items = arguments.items() if isinstance(arguments, dict) else []
                    args_str = ", ".join(f"{key}={_safe_json_stringify(value)}" for key, value in items)
                    tool_calls.append(f"{_block_field(block, 'name')}({args_str})")
            if thinking_parts:
                parts.append(f"[Assistant thinking]: {'\n'.join(thinking_parts)}")
            if text_parts:
                parts.append(f"[Assistant]: {'\n'.join(text_parts)}")
            if tool_calls:
                parts.append(f"[Assistant tool calls]: {'; '.join(tool_calls)}")
            continue

        if role == "toolResult":
            content = _serialize_tool_result_content(_message_field(message, "content"))
            if content:
                parts.append(f"[Tool result]: {_truncate_for_summary(content, _TOOL_RESULT_MAX_CHARS)}")

    return "\n\n".join(parts)


def _serialize_user_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if _block_field(block, "type") == "text":
            text = _block_field(block, "text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _serialize_tool_result_content(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if _block_field(block, "type") == "text":
            text = _block_field(block, "text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _truncate_for_summary(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    truncated_chars = len(text) - max_chars
    return f"{text[:max_chars]}\n\n[... {truncated_chars} more characters truncated]"


def _safe_json_stringify(value: Any) -> str:
    try:
        serialized = json.dumps(value)
    except Exception:
        return "[unserializable]"
    return serialized if serialized is not None else "undefined"


def _message_field(message: Any, name: str) -> Any:
    if isinstance(message, dict):
        return message.get(name)
    return getattr(message, name, None)


def _block_field(block: Any, name: str) -> Any:
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


__all__ = [
    "FileOperations",
    "SUMMARIZATION_SYSTEM_PROMPT",
    ]
