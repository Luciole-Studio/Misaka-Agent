"""Session-context types shared by the core session layer.

The rest of pi's harness type surface (Result/Ok/Err, execution environments, session
storages, a second copy of the extension events) had no consumers in MISAKA and was
removed; ``misaka.core`` is the only session runtime.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from misaka.agent.types import AgentMessage


class SessionModelInfo(TypedDict):
    provider: str
    modelId: str


@dataclass(slots=True)
class SessionContext:
    messages: list[AgentMessage]
    thinkingLevel: str
    model: SessionModelInfo | None = None
