"""The JSONL wire form for engine events."""

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


def to_json_event(value: Any) -> Any:
    """Convert an event to its wire form.

    ``message_update`` events carry only the delta: the accumulated ``partial``
    snapshot is stripped so output does not grow quadratically with message
    length. ``message_end`` remains the authoritative full message.
    """
    data = to_jsonable(value)
    if isinstance(data, dict) and data.get("type") == "message_update":
        ame = data.get("assistantMessageEvent")
        if isinstance(ame, dict):
            ame = {k: v for k, v in ame.items() if k != "partial"}
        return {"type": "message_update", "assistantMessageEvent": ame}
    return data
