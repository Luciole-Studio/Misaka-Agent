"""Translated from pi's ``core/tools/renderers/index.ts`` (v1.1.0).

Built-in tool renderers, without the tools themselves.

A presentation displays tool calls and results; it does not execute them and does not need their
typebox parameter schemas. Importing this instead of `core/tools/index.ts` keeps ~17 MB of module
graph out of a process that only renders.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from misaka.core.extensions.types import ToolRenderers
from misaka.utils.values import read_field

from .bash import createShellRenderers
from .edit import editRenderers
from .find import findRenderers
from .grep import grepRenderers
from .ls import lsRenderers
from .read import readRenderers
from .write import writeRenderers

__all__ = [
    "ToolRenderers",
    "createAllToolRenderers",
    "createShellRenderers",
    "editRenderers",
    "findRenderers",
    "grepRenderers",
    "lsRenderers",
    "readRenderers",
    "withBuiltInRenderers",
    "writeRenderers",
]


def createAllToolRenderers() -> dict[str, ToolRenderers]:
    """Renderers for every built-in tool, keyed by tool name."""
    return {
        "read": readRenderers,
        "bash": createShellRenderers("$"),
        "powershell": createShellRenderers("PS>"),
        "edit": editRenderers,
        "write": writeRenderers,
        "grep": grepRenderers,
        "find": findRenderers,
        "ls": lsRenderers,
    }


def withBuiltInRenderers(toolName: str, definition: Any | None) -> Any | None:
    """Merge built-in renderers into a tool definition that does not supply its own.

    `ToolExecutionComponent` used to do this lookup itself, which forced every presentation to import
    the tool implementations. Callers do it now, so a process that renders can import renderers alone.

    MISAKA fork: a definition is a ``ToolDefinition`` dataclass or a ``ToolRenderers`` dict; ``{ ...definition }`` is
    ``dataclasses.replace`` for the former.
    """
    builtIn = createAllToolRenderers().get(toolName)
    if not definition:
        return builtIn
    if not builtIn:
        return definition
    renderCall = read_field(definition, "renderCall")
    renderResult = read_field(definition, "renderResult")
    overrides = {
        "renderCall": renderCall if renderCall is not None else builtIn["renderCall"],
        "renderResult": renderResult if renderResult is not None else builtIn["renderResult"],
    }
    if dataclasses.is_dataclass(definition) and not isinstance(definition, type):
        return dataclasses.replace(definition, **overrides)
    return {**definition, **overrides}
