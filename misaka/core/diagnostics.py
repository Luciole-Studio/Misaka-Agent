"""Resource diagnostics shared across coding-agent core loaders."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(slots=True)
class ResourceCollision:
    resourceType: Literal["extension", "skill", "prompt", "theme"]
    name: str
    winnerPath: str
    loserPath: str
    winnerSource: str | None = None
    loserSource: str | None = None


@dataclass(slots=True)
class ResourceDiagnostic:
    type: Literal["warning", "error", "collision"]
    message: str
    path: str | None = None
    collision: ResourceCollision | None = None


__all__ = ["ResourceCollision", "ResourceDiagnostic"]
