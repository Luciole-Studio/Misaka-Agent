"""Workflows that own cards on the board, as the board sees them.

A workflow built on the board creates cards, drives them through its own phases,
and reads what they return. The board has to treat those cards a little differently: a card's
session gets the workflow's context and settings, its note takes the workflow's own fields, its
submission is checked by the workflow, an ordinary board tool must not take it over, a message sent
from inside the workflow means someone inside it, and so on. Rather than know the workflow, the board
asks the owners registered here, and each answers for its own cards.

A process registers its owners at its entry (``misaka.cli.bootstrap``). With none registered every
answer is a plain board's, which is what each default below returns. Nothing here imports a workflow.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any


class CardOwner:
    """A workflow that owns some of the board's cards. ``name`` is how the board labels what it says
    about them (the tag of a card's context in a notification, the resource of its progress events);
    ``scope_label`` names one of its scopes in a message ("<name> run"); ``token_cap_setting`` is
    the setting its token cap is read from and ``resume_command`` the command a user resumes one of
    its runs with, for the board to name."""

    name = ""
    scope_label = "run"
    token_cap_setting = ""
    resume_command = ""

    # --- its cards ------------------------------------------------------------------------------

    def card(self, con: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
        """The owner's context for a card it owns, with ``scope`` (the unit of work the card belongs
        to, such as one run of the workflow); None for a card it does not own."""
        return None

    def session_overrides(self, con: sqlite3.Connection, context: dict[str, Any]) -> dict[str, Any]:
        """Settings the card's session runs with instead of the global ones."""
        return {}

    def card_deleted(self, con: sqlite3.Connection, task_id: str) -> None:
        """A card was deleted from the board; forget it, in the same transaction."""

    def note_fields(self) -> dict[str, tuple[Any, Any]]:
        """Fields the card's note tool takes for this owner's cards: name -> (annotation, pydantic Field)."""
        return {}

    def note(self, con: sqlite3.Connection, task_id: str, context: dict[str, Any] | None, values: dict[str, Any],
             record) -> list[str]:
        """Handle the owner's fields of a card note. ``values`` holds those that were given;
        ``record(kind, payload)`` adds an event under the card's current ownership and returns whether
        it could. Returns what was recorded, in words."""
        return []

    def submission(self, con: sqlite3.Connection, task: Any, context: dict[str, Any], submission: dict[str, Any],
                   attempt_payload) -> None:
        """Add the owner's part to a card's submission, or refuse it (``worker.IncompleteSubmission``)
        while something the owner needs is missing. ``attempt_payload(kind)`` reads this attempt's event."""

    def guard(self, con: sqlite3.Connection, task_id: str, context: dict[str, Any]) -> None:
        """Raise ValueError when an ordinary board tool must not act on this card."""

    def reads_results(self, con: sqlite3.Connection, context: dict[str, Any]) -> bool:
        """Whether a driver of the owner is reading this card's results itself, so Last Order need not
        be woken for a success."""
        return False

    def budget_scope(self, con: sqlite3.Connection, task_id: str) -> set[str] | None:
        """The ledger rows ``task_id`` shares a token cap with, or None when the owner sets none for it."""
        return None

    # --- who works where --------------------------------------------------------------------------

    def sender_scope(self, con: sqlite3.Connection, task_id: str | None,
                     session_id: str | None) -> tuple[str | None, str | None]:
        """``(scope, unit)`` a message's sender works in -- the run, and the node inside it -- by her card
        or her session; ``(None, None)`` outside the owner's work."""
        return None, None

    def unit_session_file(self, con: sqlite3.Connection, unit: str) -> str | None:
        """The session file of the coordinator of ``unit`` (a node's Last Order)."""
        return None

    def scope_cards(self, con: sqlite3.Connection, scope: str) -> list[tuple[str, str | None]]:
        """``(task_id, unit)`` for every card of ``scope``."""
        return []

    def chat_scopes(self, con: sqlite3.Connection, session_id: str) -> list[str]:
        """The scopes whose chat this conversation owns: it hears their notifications."""
        return []

    def owns_chat(self, con: sqlite3.Connection, scope: str, session_id: str) -> bool:
        return False

    def progress(self, scope: str, payload: dict[str, Any]) -> tuple[str, str] | None:
        """``(customType, text)`` of a progress event of ``scope`` for the conversation that owns its chat."""
        return None

    # --- sessions -----------------------------------------------------------------------------------

    def system_context(self, session: Any, tool_names: Iterable[str], system_prompt: str,
                       sister_id: str | None) -> str:
        """What a session working inside the owner's work adds to its system prompt."""
        return ""

    def tool_descriptions(self) -> dict[str, str]:
        """One line on each of the owner's coordination tools, for Last Order's tool guide."""
        return {}

    def coordination_note(self, tool_names: Iterable[str]) -> str:
        """A line for that guide when some of those tools are active."""
        return ""

    def card_discipline(self, context: dict[str, Any]) -> str:
        """What an agent without the owner's system prompt (an ally) reads before the card's contract."""
        return ""

    def borrowed_tools(self, collector: Any) -> None:
        """Register the owner's read-only tools an ally may borrow."""

    def owns_window(self) -> bool:
        """Whether this process's foreground window is the owner's (not the user's own desk)."""
        return False

    def catalog(self, con: sqlite3.Connection) -> tuple[dict, dict, dict]:
        """``(scopes, units, cards)`` for the session catalog: rows by scope id, by unit id, and the card
        links by task id."""
        return {}, {}, {}

    def session_dirs(self) -> frozenset[str]:
        """The folders of the session store that hold the owner's own conversations."""
        return frozenset()

    def session_place(self, parts: list[str], units: dict[str, Any]) -> tuple[str, str | None, bool] | None:
        """``(scope, unit, coordinator)`` of a conversation in one of those folders, from its path's parts
        under the store: whether it is a coordinator's (a node's Last Order), and of which unit."""
        return None

    # --- the board's settings and files -------------------------------------------------------------

    def token_cap(self) -> int:
        """The tokens one of the owner's scopes may spend (0: none)."""
        return 0

    def beast_at(self) -> float:
        """The share of the cap at which the scope's cards switch to beast mode."""
        return 1.0

    def read_only_tools(self) -> frozenset[str]:
        """The owner's tools that only look: an ally's card bridge lends these to its runner."""
        return frozenset()

    def owns_card_process(self, argv: list[str]) -> bool:
        """Whether a process with this command line runs an attempt at one of the owner's cards."""
        return False

    def gitignore(self) -> str:
        """Lines a new project's ``.gitignore`` gets for what the owner derives in it."""
        return ""

    def frozen_refusal(self, target: str) -> str | None:
        """Why a file tool may not write ``target`` (a file the owner froze), or None."""
        return None


_OWNERS: list[CardOwner] = []


def register(owner: CardOwner) -> None:
    """Add ``owner``; registering the same kind of owner again replaces the earlier one."""
    _OWNERS[:] = [found for found in _OWNERS if type(found) is not type(owner)] + [owner]


def registered() -> tuple[CardOwner, ...]:
    return tuple(_OWNERS)


def named(name: str | None) -> CardOwner | None:
    return next((owner for owner in _OWNERS if owner.name == name), None) if name else None


def card(con: sqlite3.Connection, task_id: str) -> tuple[CardOwner | None, dict[str, Any] | None]:
    """The owner of a card and its context, or ``(None, None)`` for a plain card."""
    for owner in _OWNERS:
        context = owner.card(con, task_id)
        if context is not None:
            return owner, context
    return None, None


def card_deleted(con: sqlite3.Connection, task_id: str) -> None:
    for owner in _OWNERS:
        owner.card_deleted(con, task_id)


def budget_scope(con: sqlite3.Connection, task_id: str) -> set[str] | None:
    for owner in _OWNERS:
        found = owner.budget_scope(con, task_id)
        if found is not None:
            return found
    return None


def sender_scope(con: sqlite3.Connection, task_id: str | None,
                 session_id: str | None) -> tuple[CardOwner | None, str | None, str | None]:
    for owner in _OWNERS:
        scope, unit = owner.sender_scope(con, task_id, session_id)
        if scope is not None:
            return owner, scope, unit
    return None, None, None


def owns_window() -> bool:
    return any(owner.owns_window() for owner in _OWNERS)


def token_cap() -> int:
    """The cap every owner scope runs under: the tightest an owner sets (0: none)."""
    caps = [cap for cap in (owner.token_cap() for owner in _OWNERS) if cap > 0]
    return min(caps, default=0)


def beast_at() -> float:
    return min((owner.beast_at() for owner in _OWNERS), default=1.0)


def read_only_tools() -> frozenset[str]:
    return frozenset().union(*(owner.read_only_tools() for owner in _OWNERS))


def owns_card_process(argv: list[str]) -> bool:
    return any(owner.owns_card_process(argv) for owner in _OWNERS)


def frozen_refusal(target: str) -> str | None:
    return next((reason for owner in _OWNERS if (reason := owner.frozen_refusal(target))), None)
