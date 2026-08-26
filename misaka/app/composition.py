"""Session assembly: describe the session, then let :mod:`misaka.extensions` pick its extensions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from misaka.extensions import discover

type SessionKind = Literal["foreground", "dm", "card", "child", "beast", "bare"]


@dataclass(frozen=True, slots=True)
class SessionSpec:
    profile_dir: str
    role: str
    workspace: str
    kind: SessionKind
    sender: str | None = None
    mcp_role: str | None = None
    receive_messages: bool = False
    task_id: str | None = None
    tool_ceiling: tuple[str, ...] | None = None
    # Where this session's skills come from, as ``(layer, root)`` pairs. None = the role's
    # three layers (project, role, shared); a card passes its read-only sandbox instead.
    skill_roots: tuple[tuple[str, str], ...] | None = None


def build_extensions(spec: SessionSpec) -> list[dict]:
    return discover(spec)


__all__ = ["SessionKind", "SessionSpec", "build_extensions"]
