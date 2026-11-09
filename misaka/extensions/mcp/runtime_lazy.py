"""Translated from pi's ``coding-agent/src/extensions/mcp/runtime.lazy.ts``."""

from __future__ import annotations

import importlib
from types import ModuleType


async def loadMcpRuntime() -> ModuleType:
    """Loads the MCP client, transports, and OAuth sign-in on first use (see ``runtime.py``)."""
    return importlib.import_module("misaka.extensions.mcp.runtime")


__all__ = ["loadMcpRuntime"]
