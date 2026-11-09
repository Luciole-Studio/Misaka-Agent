"""Translated from pi's ``packages/coding-agent/src/modes/json-event.ts`` (v1.1.0): the JSONL wire form for engine events.

MISAKA fork: ``to_jsonable`` is MISAKA's helper for turning models and dataclasses into plain data (other modules use
it too). The wire form itself is ``toJsonEvent`` written with ``jsonStringify``: a model field that is not set (``None``)
is left out, as ``JSON.stringify`` leaves out an ``undefined`` property, and numbers print as JavaScript prints them.
"""

from __future__ import annotations

import dataclasses
from typing import Any


def to_jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return to_jsonable(value.model_dump())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return to_jsonable(vars(value))
    return value


def _wireValue(value: Any) -> Any:
    """The event as ``JSON.stringify`` sees it: model fields that are ``None`` are not set, so they are left out."""
    if hasattr(value, "model_dump"):
        return _wireValue(value.model_dump(exclude_none=True))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _wireValue({key: item for key, item in dataclasses.asdict(value).items() if item is not None})
    if isinstance(value, dict):
        return {str(key): _wireValue(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_wireValue(item) for item in value]
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return _wireValue({key: item for key, item in vars(value).items() if item is not None})
    return value


def toJsonAssistantMessageEvent(event: Any) -> Any:
    if not isinstance(event, dict):
        return event

    if event.get("type") == "toolcall_start":
        partial = event.get("partial")
        content = partial.get("content") if isinstance(partial, dict) else None
        index = event.get("contentIndex")
        tool_call = (
            content[index]
            if isinstance(content, list) and isinstance(index, int) and 0 <= index < len(content)
            else None
        )
        if not isinstance(tool_call, dict) or tool_call.get("type") != "toolCall":
            raise ValueError(f"toolcall_start content at index {index} is not a tool call")
        delta_event = {key: item for key, item in event.items() if key != "partial"}
        return {**delta_event, "id": tool_call.get("id"), "toolName": tool_call.get("name")}

    if "partial" in event:
        return {key: item for key, item in event.items() if key != "partial"}
    return event


def toJsonEvent(event: Any) -> Any:
    """Convert an event to its wire form.

    ``message_update`` events carry only the delta: the accumulated ``partial``
    snapshot is stripped so output does not grow quadratically with message
    length. ``message_end`` remains the authoritative full message.

    Cumulative ``usage`` is kept alongside the delta, and ``toolcall_start`` — whose
    identity would otherwise only live in the dropped snapshot — is re-tagged with the
    tool call's ``id`` and ``toolName``. Both are constant-size, so consumers can bill
    tokens and pair a tool call with its result without the snapshot.
    """
    data = _wireValue(event)
    if not isinstance(data, dict) or data.get("type") != "message_update":
        return data

    message = data.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ValueError("message_update message is not an assistant message")

    return {
        "type": "message_update",
        "usage": message.get("usage"),
        "assistantMessageEvent": toJsonAssistantMessageEvent(data.get("assistantMessageEvent")),
    }
