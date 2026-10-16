"""Fork points for the conversation view.

A turn is what the GUI draws as one reply: the assistant messages and tool results
that follow a user message or a displayed notification. The browser names a turn by
the timestamp of an assistant message in it; history replays and live events carry
the same value, while entry ids never reach the browser.
"""
from __future__ import annotations

from typing import Any

# Entries that belong to the turn before them: they never open a new row on screen.
_QUIET_ENTRIES = frozenset({"model_change", "thinking_level_change", "label", "session_info",
                            "custom", "context_edit"})


def _field(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def message_role(entry: dict) -> Any:
    return _field(entry.get("message"), "role") if entry.get("type") == "message" else None


def _continues_turn(entry: dict) -> bool:
    kind = entry.get("type")
    if kind == "message":
        return message_role(entry) in ("assistant", "toolResult", "system")
    if kind == "custom_message":
        return entry.get("display") is False
    return kind in _QUIET_ENTRIES


def turn_end(branch: list[dict], timestamp: Any) -> str:
    """Id of the last entry of the turn holding the assistant message sent at ``timestamp``."""
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise ValueError("回复标识无效，请刷新后再试")  # noqa: TRY004 - GUI validation uses ValueError for invalid input.
    start = next((index for index, entry in enumerate(branch)
                  if message_role(entry) == "assistant" and _field(entry["message"], "timestamp") == timestamp), None)
    if start is None:
        raise ValueError("找不到这条回复；它可能已被撤回，请刷新后再试")
    end = start
    while end + 1 < len(branch) and _continues_turn(branch[end + 1]):
        end += 1
    return str(branch[end]["id"])


def last_user_entry(branch: list[dict]) -> dict | None:
    return next((entry for entry in reversed(branch) if message_role(entry) == "user"), None)


def fork_file(session_file: str, *, leaf_id: str | None = None, timestamp: Any = None,
              name: str | None = None) -> str:
    """Copy the path up to one turn into a new session file beside the original.

    The original file is only read. Without ``leaf_id`` the turn is looked up on the
    file's own active path, which is how a saved session is displayed.
    """
    from misaka.core.session_manager import SessionManager

    manager = SessionManager.open(session_file)
    if leaf_id is None:
        leaf_id = turn_end(manager.getBranch(), timestamp)
    path = manager.createBranchedSession(leaf_id)
    if not path:
        raise ValueError("无法创建分叉对话")
    if isinstance(name, str) and name.strip():
        manager.appendSessionInfo(name.strip()[:120])
    return path
