"""The ``codemode`` tool as an extension. Sessions load it as a built-in extension
(``builtin:codemode``); SDK users add ``createCodemodeExtension()`` to their extension factories.

Translated from pi's ``coding-agent/src/extensions/codemode/index.ts``.

``codemode`` is registered inactive. Activate it with ``--tools``, the ``defaultTools`` setting, or
``setActiveTools()``; the MCP extension activates it when MCP tools are only reachable from scripts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

from misaka.core.extensions.types import ExtensionFactory
from misaka.core.settings_defaults import SETTINGS_DEFAULTS
from misaka.core.settings_manager import CodemodeMode
from misaka.core.wiring import KINDS
from misaka.utils.js import isNumber
from misaka.utils.values import read_field

from .tool import (
    CodemodeToolOptions,
    createCodemodeToolDefinition,
)

EXTENSION_NAME = "codemode"
# MISAKA fork: bundled-extension metadata for misaka's loader.
# `builtin:codemode`, as upstream bundles it. Replaceable: an extension that registers `codemode` takes
# over instead of running alongside it.
BUILTIN = True
REPLACEABLE = True
SESSION_KINDS = KINDS
# MISAKA fork: Sisters only. Last Order coordinates and does not batch work through scripts; her MCP tools
# are declared to her directly (the MCP glue makes her servers `direct`).
ROLES = {"sisters"}


@dataclass(slots=True)
class CodemodeExtensionOptions:
    # Overrides the `codemode.mode` setting.
    mode: CodemodeMode | None = None
    # Overrides the `codemode.inlineBudget` setting.
    inlineBudget: float | None = None
    # Expose the model catalog and classifiers to scripts as `models`. Default: `True`.
    models: bool | None = None


def readMode(pi: Any) -> CodemodeMode:
    return (
        "only"
        if read_field(read_field(pi.getSettings(), "codemode"), "mode") == "only"
        else SETTINGS_DEFAULTS["codemode"]["mode"]
    )


def readInlineBudget(pi: Any) -> float | None:
    budget = read_field(read_field(pi.getSettings(), "codemode"), "inlineBudget")
    return budget if isNumber(budget) and math.isfinite(budget) and budget >= 0 else None


def createCodemodeExtension(options: CodemodeExtensionOptions | None = None) -> ExtensionFactory:
    options = options or CodemodeExtensionOptions()

    def factory(pi: Any) -> None:
        def getToolNamespace(name: str) -> Any:
            return next((tool.namespace for tool in pi.getAllTools() if tool.name == name), None)

        definition = createCodemodeToolDefinition(CodemodeToolOptions(
            appendEntry=lambda customType, data: pi.appendEntry(customType, data),
            models=options.models if options.models is not None else True,
            getToolNamespace=getToolNamespace,
            getToolGuidelines=lambda: {tool.name: tool.promptGuidelines or [] for tool in pi.getAllTools()},
            getMode=lambda: options.mode if options.mode is not None else readMode(pi),
            getInlineBudget=lambda: options.inlineBudget if options.inlineBudget is not None else readInlineBudget(pi),
        ))
        pi.registerTool(replace(definition, defaultActive=False))

    return factory


register = createCodemodeExtension()


def activate(_spec: Any):
    return register


__all__ = [
    "BUILTIN",
    "EXTENSION_NAME",
    "REPLACEABLE",
    "ROLES",
    "SESSION_KINDS",
    "CodemodeExtensionOptions",
    "activate",
    "createCodemodeExtension",
    "register",
]
