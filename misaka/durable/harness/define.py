"""Identity helpers that type extensions, tools, sections, hooks and wrappers.

Translated from pi's ``packages/durable/src/harness/define.ts``. An extension, a tool, a section, a hook registration
and a wrapper are plain ``dict`` objects with the upstream keys.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def defineExtension(extension: dict[str, Any]) -> dict[str, Any]:
    """Identity function that types an extension."""
    return extension


def defineTool(tool: dict[str, Any]) -> dict[str, Any]:
    """Identity function that types a tool: ``args`` from ``parameters``, details from what it reports."""
    return tool


def section(key: str, render: Callable[..., Any], options: dict[str, Any] | None = None) -> dict[str, Any]:
    """A prompt section; tagged unless ``tag`` is false."""
    if options is None or options.get("tag") is None:
        return {"key": key, "render": render}
    return {"key": key, "render": render, "tag": options["tag"]}


def hook(task: dict[str, Any], handlers: dict[str, Any]) -> dict[str, Any]:
    """Hook handlers for tasks with ``task``'s name."""
    return {"task": task["definition"]["name"], "handlers": handlers}


def wrapTool(tool: dict[str, Any], wrapper: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """Wrap the tool named like ``tool`` wherever the wrapping extension is selected."""
    return {"tool": tool["name"], "wrap": wrapper}


def wrapSection(key: str, wrapper: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """Wrap the section ``key`` wherever the wrapping extension is selected."""
    return {"section": key, "wrap": wrapper}


__all__ = ["defineExtension", "defineTool", "hook", "section", "wrapSection", "wrapTool"]
