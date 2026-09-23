"""Shared message transformation helpers for cross-provider replay."""

from __future__ import annotations

import time
from collections.abc import Callable

from misaka.ai.types import (
    AssistantMessage,
    ImageContent,
    MessageValue,
    Model,
    SystemMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)

_NON_VISION_USER_IMAGE_PLACEHOLDER = "(image omitted: model does not support images)"
_NON_VISION_TOOL_IMAGE_PLACEHOLDER = "(tool image omitted: model does not support images)"


def _replace_images_with_placeholder(content: list[TextContent | ImageContent], placeholder: str) -> list[TextContent]:
    result: list[TextContent] = []
    previous_was_placeholder = False

    for block in content:
        if block.type == "image":
            if not previous_was_placeholder:
                result.append(TextContent(text=placeholder))
            previous_was_placeholder = True
            continue

        result.append(block)
        previous_was_placeholder = block.text == placeholder

    return result


def _downgrade_unsupported_images(messages: list[MessageValue], model: Model) -> list[MessageValue]:
    if "image" in model.input:
        return messages

    downgraded: list[MessageValue] = []
    for message in messages:
        if isinstance(message, UserMessage) and isinstance(message.content, list):
            downgraded.append(
                message.model_copy(
                    update={"content": _replace_images_with_placeholder(message.content, _NON_VISION_USER_IMAGE_PLACEHOLDER)}
                )
            )
            continue

        if isinstance(message, ToolResultMessage):
            downgraded.append(
                message.model_copy(
                    update={"content": _replace_images_with_placeholder(message.content, _NON_VISION_TOOL_IMAGE_PLACEHOLDER)}
                )
            )
            continue

        downgraded.append(message)

    return downgraded


def transform_messages(
    messages: list[MessageValue],
    model: Model,
    normalize_tool_call_id: Callable[[str, Model, AssistantMessage], str] | None = None,
) -> list[MessageValue]:
    tool_call_id_map: dict[str, str] = {}
    image_aware_messages = _downgrade_unsupported_images(messages, model)

    transformed: list[MessageValue] = []
    for message in image_aware_messages:
        # System and user messages pass through unchanged
        if isinstance(message, SystemMessage | UserMessage):
            transformed.append(message)
            continue

        if isinstance(message, ToolResultMessage):
            normalized_id = tool_call_id_map.get(message.toolCallId)
            if normalized_id and normalized_id != message.toolCallId:
                transformed.append(message.model_copy(update={"toolCallId": normalized_id}))
            else:
                transformed.append(message)
            continue

        if isinstance(message, AssistantMessage):
            is_same_model = (
                message.provider == model.provider and message.api == model.api and message.model == model.id
            )
            transformed_content: list[TextContent | ToolCall | object] = []

            for block in message.content:
                if block.type == "thinking":
                    if block.redacted:
                        if is_same_model:
                            transformed_content.append(block)
                        continue
                    if is_same_model and block.thinkingSignature:
                        transformed_content.append(block)
                        continue
                    if not block.thinking or block.thinking.strip() == "":
                        continue
                    if is_same_model:
                        transformed_content.append(block)
                    else:
                        transformed_content.append(TextContent(text=block.thinking))
                    continue

                if block.type == "text":
                    transformed_content.append(block if is_same_model else TextContent(text=block.text))
                    continue

                if block.type == "toolCall":
                    normalized_tool_call = block
                    if not is_same_model and block.thoughtSignature:
                        normalized_tool_call = block.model_copy(update={"thoughtSignature": None})

                    if not is_same_model and normalize_tool_call_id is not None:
                        normalized_id = normalize_tool_call_id(block.id, model, message)
                        if normalized_id != block.id:
                            tool_call_id_map[block.id] = normalized_id
                            normalized_tool_call = normalized_tool_call.model_copy(update={"id": normalized_id})

                    transformed_content.append(normalized_tool_call)
                    continue

                transformed_content.append(block)

            transformed.append(message.model_copy(update={"content": transformed_content}))
            continue

        transformed.append(message)

    result: list[MessageValue] = []
    pending_tool_calls: list[ToolCall] = []
    existing_tool_result_ids: set[str] = set()
    # System messages are transparent to tool-call accounting: one that lands between a tool
    # call and its results is held back and emitted after the results (synthetic ones
    # included), so it never causes a duplicate result for a call that is answered later.
    held_system_messages: list[SystemMessage] = []

    def close_pending_tool_calls() -> None:
        nonlocal pending_tool_calls, existing_tool_result_ids
        if pending_tool_calls:
            for tool_call in pending_tool_calls:
                if tool_call.id in existing_tool_result_ids:
                    continue
                result.append(
                    ToolResultMessage(
                        toolCallId=tool_call.id,
                        toolName=tool_call.name,
                        content=[TextContent(text="No result provided")],
                        isError=True,
                        timestamp=time.time_ns() // 1_000_000,
                    )
                )
            pending_tool_calls = []
            existing_tool_result_ids = set()
        result.extend(held_system_messages)
        held_system_messages.clear()

    for message in transformed:
        if isinstance(message, AssistantMessage):
            # If we have pending orphaned tool calls from a previous assistant, insert synthetic results now
            close_pending_tool_calls()
            # Skip errored/aborted assistant messages entirely.
            # These are incomplete turns that shouldn't be replayed:
            # - May have partial content (reasoning without message, incomplete tool calls)
            # - Tool calls from errored messages don't have results
            if message.stopReason in {"error", "aborted"}:
                continue
            tool_calls = [block for block in message.content if block.type == "toolCall"]
            if tool_calls:
                pending_tool_calls = tool_calls
                existing_tool_result_ids = set()
            result.append(message)
            continue

        if isinstance(message, ToolResultMessage):
            existing_tool_result_ids.add(message.toolCallId)
            result.append(message)
            continue

        if isinstance(message, SystemMessage):
            if pending_tool_calls:
                held_system_messages.append(message)
            else:
                result.append(message)
            continue

        if isinstance(message, UserMessage):
            # A new user turn interrupts tool flow - insert synthetic results for orphaned calls
            close_pending_tool_calls()
            result.append(message)
            continue

        result.append(message)

    # If the conversation ends with unresolved tool calls, synthesize results now.
    close_pending_tool_calls()
    return result


__all__ = [
    ]
