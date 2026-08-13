"""File-operation extraction and conversation serialization helpers for compaction."""

from __future__ import annotations

import json
from typing import Any

from misaka.ai.types import AssistantMessage, MessageValue

from misaka.agent.harness.types import FileOperations

TOOL_RESULT_MAX_CHARS = 2000


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
        path = _tool_call_path(block)
        if not path:
            continue
        name = _block_field(block, "name")
        if name == "read":
            file_ops.read.add(path)
        elif name == "write":
            file_ops.written.add(path)
        elif name == "edit":
            file_ops.edited.add(path)


def compute_file_lists(file_ops: FileOperations) -> tuple[list[str], list[str]]:
    modified = {*file_ops.edited, *file_ops.written}
    read_files = sorted(path for path in file_ops.read if path not in modified)
    modified_files = sorted(modified)
    return read_files, modified_files


def format_file_operations(read_files: list[str], modified_files: list[str]) -> str:
    sections: list[str] = []
    if read_files:
        sections.append(f"<read-files>\n{'\n'.join(read_files)}\n</read-files>")
    if modified_files:
        sections.append(f"<modified-files>\n{'\n'.join(modified_files)}\n</modified-files>")
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
            assistant = message if isinstance(message, AssistantMessage) else message
            for block in _message_field(assistant, "content") or []:
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
                    args = _block_field(block, "arguments")
                    args_items = args.items() if isinstance(args, dict) else []
                    args_str = ", ".join(f"{key}={_safe_json_stringify(value)}" for key, value in args_items)
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
                parts.append(f"[Tool result]: {_truncate_for_summary(content, TOOL_RESULT_MAX_CHARS)}")

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


def _tool_call_path(block: Any) -> str | None:
    arguments = _block_field(block, "arguments")
    if not isinstance(arguments, dict):
        return None
    path = arguments.get("path")
    return path if isinstance(path, str) and path else None


def _block_field(block: Any, name: str) -> Any:
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def _message_field(message: Any, name: str) -> Any:
    if isinstance(message, dict):
        return message.get(name)
    return getattr(message, name, None)


def _safe_json_stringify(value: Any) -> str:
    try:
        serialized = json.dumps(value)
    except Exception:
        return "[unserializable]"
    return serialized if serialized is not None else "undefined"


def _truncate_for_summary(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    truncated_chars = len(text) - max_chars
    return f"{text[:max_chars]}\n\n[... {truncated_chars} more characters truncated]"


computeFileLists = compute_file_lists
createFileOps = create_file_ops
extractFileOpsFromMessage = extract_file_ops_from_message
formatFileOperations = format_file_operations
serializeConversation = serialize_conversation

__all__ = [
    "FileOperations",
    "TOOL_RESULT_MAX_CHARS",
    "computeFileLists",
    "compute_file_lists",
    "createFileOps",
    "create_file_ops",
    "extractFileOpsFromMessage",
    "extract_file_ops_from_message",
    "formatFileOperations",
    "format_file_operations",
    "serializeConversation",
    "serialize_conversation",
]
