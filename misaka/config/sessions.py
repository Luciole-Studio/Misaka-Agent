"""Where MISAKA keeps conversations: one root, then the role, then the folder.

pi keeps one agent's sessions under its engine home, bucketed by working directory
(``~/.pi/agent/sessions/<encoded cwd>/``). MISAKA runs several roles out of one install,
so the product tree adds the role above the bucket::

    ~/.misaka/sessions/<role>/<folder bucket>/*.jsonl        the role's chats in that folder
    ~/.misaka/sessions/<role>/dm/*.jsonl                     her DM inbox conversation
    ~/.misaka/sessions/<sister>/<bucket>/cards/<card>/       a card she ran there (one dir per card)
    ~/.misaka/sessions/last-order/<bucket>/intake/           Last Order drafting that folder's brief

Everything a session can be lives under this root, and it is the only root the three ways
of finding one (``/resume``, the panel sidebar, ``--session <id>``) look at. Card and intake
conversations sit one level *below* a bucket on purpose: ``/resume`` lists a bucket's own
files, so they never show up as chats to continue, while a card keeps the one-directory
invariant its resume logic relies on (the newest file in its dir is its transcript).

Until this module, the engine's own default (``<agent dir>/sessions/<bucket>``, pi's
layout) stayed reachable: a session created without an explicit directory landed there,
where nothing in the product ever looked. The engine default now resolves here too.
"""

import os

ENV = "MISAKA_SESSIONS"
COORDINATOR = "last-order"


def sessions_root() -> str:
    return os.path.expanduser(os.environ.get("MISAKA_SESSIONS") or "~/.misaka/sessions")


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
    """One directory per card, under its Sister's bucket for the card's workspace.

    ``task`` is a board row or a dict with ``id``, ``assignee`` and ``workspace``.
    """
    return os.path.join(chat_dir(task["assignee"], task["workspace"]), "cards", str(task["id"]))


def intake_session_dir(workspace: str) -> str:
    """Where Last Order drafts a folder's PROJECT.md: under her bucket for that folder."""
    return os.path.join(chat_dir(COORDINATOR, workspace), "intake")


__all__ = ["COORDINATOR", "ENV", "card_session_dir", "chat_dir", "dm_dir", "intake_session_dir",
           "role_dir", "sessions_root"]
