"""Bundled capability policy.

Roles grant the maximum authority an identity may have.  Session kinds only
remove authority; they never grant it.  The composition root consumes the
result, so individual entry points do not maintain extension allow-lists.
"""

from __future__ import annotations

from typing import Literal

from misaka.config import profiles

type SessionKind = Literal["foreground", "dm", "card", "child", "one-shot", "beast", "bare"]

ALLY = "ally"
ASK_USER = "ask-user"
DELEGATE = "delegate"
DOCUMENTS = "documents"
LCM = "lcm"
MCP = "mcp"
MESSAGES = "messages"
MOA = "moa"
NETWORK_CONTROL = "network.control"
OBSERVE = "observe"
RESEARCH = "research"
ROSTER = "roster"
SKILLS = "skills"
TODO = "todo"

_SHARED = {ASK_USER, DOCUMENTS, LCM, MCP, MESSAGES, MOA, OBSERVE, ROSTER, SKILLS, TODO}

# Two authority classes are enough today.  Adding a role means changing this
# policy, not every CLI/worker/child entry point.
ROLE_CAPABILITIES = {
    "last-order": frozenset(_SHARED | {ALLY, NETWORK_CONTROL, RESEARCH}),
    "worker": frozenset(_SHARED | {DELEGATE}),
}

_ALL = frozenset().union(*ROLE_CAPABILITIES.values())

SESSION_CEILINGS: dict[SessionKind, frozenset[str]] = {
    "foreground": _ALL,
    "dm": _ALL - {ASK_USER},
    "card": frozenset({DOCUMENTS, LCM, MCP, MESSAGES, DELEGATE, SKILLS, TODO}),
    "child": frozenset({DOCUMENTS, MCP, MESSAGES, DELEGATE}),
    "one-shot": frozenset({MESSAGES, DELEGATE}),
    # Beast keeps only facilities that can affect a headless run.  Slash
    # commands and skill tools would be filtered by -t, so loading them wastes
    # work without changing the model's effective tool set.
    "beast": frozenset({LCM, MESSAGES, DELEGATE, TODO}),
    "bare": frozenset(),
}


def role_class(profile_dir: str) -> str:
    return "last-order" if profiles.is_last_order(profile_dir) else "worker"


def resolve(profile_dir: str, session_kind: SessionKind) -> frozenset[str]:
    """Return role authority intersected with the session's hard ceiling."""

    if session_kind == "beast" and role_class(profile_dir) == "last-order":
        return frozenset()
    return ROLE_CAPABILITIES[role_class(profile_dir)] & SESSION_CEILINGS[session_kind]


__all__ = [
    "ALLY", "ASK_USER", "DELEGATE", "DOCUMENTS", "LCM", "MCP", "MESSAGES", "MOA",
    "NETWORK_CONTROL", "OBSERVE", "RESEARCH", "ROSTER", "SKILLS", "TODO",
    "ROLE_CAPABILITIES", "SESSION_CEILINGS", "SessionKind", "resolve", "role_class",
]
