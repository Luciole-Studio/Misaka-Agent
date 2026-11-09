"""Translated from pi's ``coding-agent/src/extensions/mcp/cli.lazy.ts``."""

from __future__ import annotations

import importlib
from types import ModuleType


def loadMcpCommand() -> ModuleType:
    """Loads ``misaka mcp`` and the MCP runtime only when the command runs (see ``cli.py``)."""
    return importlib.import_module("misaka.extensions.mcp.cli")


__all__ = ["loadMcpCommand"]
