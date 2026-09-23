"""Session paths: one product root, with roles, cards, research and nested agents.

Explicit session directories and SDK engine homes remain supported. Child sessions
stay beside a persisted parent; children of in-memory parents use this root too.
SessionManager owns transcript storage; the catalog adds identity and live state.
"""

import os

from misaka.config import home

COORDINATOR = "last-order"


def sessions_root() -> str:
    return str(home.path("sessions"))


def role_dir(role: str | None = None) -> str:
    """The root of one role's conversations; ``None`` is the coordinator."""
    return os.path.join(sessions_root(), role or COORDINATOR)


def chat_dir(role: str | None, cwd: str) -> str:
    """The bucket for this role's chats in ``cwd`` (created if needed)."""
    from misaka.core.session_manager import get_session_dir_for_cwd

    return get_session_dir_for_cwd(cwd, role_dir(role))


def dm_dir(role: str) -> str:
    return os.path.join(role_dir(role), "dm")


def card_session_dir(task) -> str:
    """One immutable storage address per card; resetting its runtime does not reset this.

    Newly allocated cards use their id, independent of assignee and execution workspace.
    A persisted address is authoritative (including existing directories retained in place).
    """
    directory = task["session_dir"] if "session_dir" in task.keys() else None  # noqa: SIM118 - sqlite3.Row membership checks values
    return directory or os.path.join(sessions_root(), "cards", str(task["id"]))


def subagent_session_dir(parent_id: str, parent_file: str | None = None) -> str:
    """One child namespace per parent, including parents with no transcript yet."""
    if not parent_id or parent_id in {".", ".."} or "/" in parent_id or "\\" in parent_id:
        raise ValueError("A parent session ID must be a single path component.")
    if parent_file:
        return os.path.join(os.path.dirname(os.path.realpath(os.path.expanduser(parent_file))), parent_id, "subagents")
    return os.path.join(sessions_root(), "subagents", parent_id)


__all__ = ["COORDINATOR", "card_session_dir", "chat_dir", "dm_dir", "role_dir",
           "sessions_root", "subagent_session_dir"]
