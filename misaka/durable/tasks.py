"""Translated from pi's ``packages/durable/src/tasks.ts``."""

from __future__ import annotations

from typing import Any


def defineTask(definition: dict[str, Any]) -> dict[str, Any]:
    """Define an executable task. Register it in the registry so a Harness can run tasks of its kind. A task is
    ``{"definition": definition}``."""
    return {"definition": definition}


__all__ = ["defineTask"]
