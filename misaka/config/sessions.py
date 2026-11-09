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


# A role's contact session is the one in her dm folder named this: Hermes' canonical Bot Chat, which
# Bot Mode finds by its exact title (hermes_state.py CANONICAL_BOT_CHAT_TITLE, ``get_session_by_title``).
CONTACT_TITLE = "Bot Chat"


def contact_role(session_file: str | None) -> str | None:
    """The role whose dm folder holds ``session_file`` itself (not a child session below it), else None."""
    if not session_file:
        return None
    folder = os.path.dirname(os.path.realpath(os.path.expanduser(session_file)))
    role_folder = os.path.dirname(folder)
    if os.path.basename(folder) != "dm" or os.path.dirname(role_folder) != os.path.realpath(sessions_root()):
        return None
    return os.path.basename(role_folder)


def canonical_contact(session_file: str | None, name: str | None, parent_session: str | None) -> str | None:
    """The role whose canonical Bot Chat this session is, else None.

    Hermes' guard covers the canonical chat and not "a visible session merely named 'Bot Chat'"
    (hermes_state_titles.py); here that is a session in a role's dm folder holding the title. A branch
    of it (``/fork``, ``/clone``) copies the title but names its parent: like Hermes' "Bot Chat #N"
    sibling, it is never the Bot Chat."""
    role = contact_role(session_file)
    return role if role and name == CONTACT_TITLE and not parent_session else None


def contact_rename_refusal(role: str) -> str:
    """Hermes' refusal, verbatim up to its last sentence: Hermes says to create a new bot, and MISAKA
    starts a role's Bot Chat afresh when the old one is deleted (``session_catalog.deletable``)."""
    return (f"This is {role}'s canonical Bot Chat — its name is its identity, and renaming it would orphan "
            f"the conversation. To start fresh, delete it: the next message to {role} starts a new one.")


def contact_lock_path(role: str) -> str:
    """The lock a delivery to ``role``'s contact session holds from claiming its mail to settling it
    (``misaka.cli.dm``); deleting the Bot Chat takes it too."""
    return str(home.path("locks") / f"dm-{role}.lock")


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


__all__ = ["CONTACT_TITLE", "COORDINATOR", "canonical_contact", "card_session_dir", "chat_dir", "contact_lock_path",
           "contact_rename_refusal", "contact_role", "dm_dir", "role_dir", "sessions_root", "subagent_session_dir"]
