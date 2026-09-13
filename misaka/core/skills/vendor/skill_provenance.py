# Hermes f03ed94a34f47ebca57e4a1b0a890bc2aeb5e140 / tools/skill_provenance.py; see PROVENANCE.json and LICENSE.

"""Skill write-origin provenance: a ContextVar separating background-review skill writes from foreground
user-directed writes (the curator only curates skills the self-improvement review fork created; skills a user
asked for belong to the user). run_agent.py binds the origin before each tool loop, mirroring
AIAgent._memory_write_origin: ``token = set_current_write_origin(...)`` / ``reset_current_write_origin(token)``."""

import contextvars

_write_origin: contextvars.ContextVar[str] = contextvars.ContextVar("skill_write_origin", default="foreground")
BACKGROUND_REVIEW = "background_review"  # sentinel used by run_agent._spawn_background_review


def set_current_write_origin(origin: str) -> contextvars.Token[str]:
    return _write_origin.set(origin or "foreground")


def reset_current_write_origin(token: contextvars.Token[str]) -> None:
    _write_origin.reset(token)


def get_current_write_origin() -> str:
    """"foreground" for any regular agent (CLI, gateway, cron, subagent); "background_review" for the review fork."""
    return _write_origin.get()


def is_background_review() -> bool:
    return get_current_write_origin() == BACKGROUND_REVIEW
